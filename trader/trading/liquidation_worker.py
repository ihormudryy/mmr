"""One serialized worker for every LiquidationService entry point (R12).

The service sends orders with ``run_coroutine_threadsafe(...).result()`` onto
the trader loop. That wait is safe only off the loop, so nothing may run the
service on the loop. ``trader_service`` already runs its recovery and session
ticks on a single worker thread (PR #42); this module makes that worker the
one every producer shares, through ``SerializedLiquidation``:

- trader_service's ticks run on the worker (``run_in_executor``); a call
  made there runs inline;
- a thread with no running loop (RPC handler) calls ``start`` and blocks;
- a coroutine on an event loop awaits ``run_async``; a blocking call there
  is refused;
- the broker ingest thread uses ``start_nowait``: it holds the ingest apply
  lock, and the worker's broker snapshot needs that lock, so it must not wait.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional


class LiquidationWorker(ThreadPoolExecutor):
    """The single liquidation thread. An executor, so ``loop.run_in_executor`` can use it."""

    def __init__(self, name: str = "liquidation-worker"):
        self._thread_id: Optional[int] = None
        super().__init__(max_workers=1, thread_name_prefix=name, initializer=self._remember_thread)

    def _remember_thread(self) -> None:
        self._thread_id = threading.get_ident()

    def in_worker(self) -> bool:
        return threading.get_ident() == self._thread_id

    def call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Run on the worker and wait. Inline when already on the worker."""
        if self.in_worker():
            return fn(*args, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.submit(fn, *args, **kwargs).result()
        raise RuntimeError("a blocking liquidation call on an event loop would deadlock; await run_async")

    async def run_async(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))


class SerializedLiquidation:
    """``LiquidationService`` behind the worker. Reads go straight to the journal."""

    def __init__(self, service, worker: LiquidationWorker, *, account_id: str,
                 now: Callable[[], dt.datetime], deadline_seconds: float = 300.0):
        self._service = service
        self._worker = worker
        self._account_id = account_id
        self._now = now
        self._deadline_seconds = deadline_seconds
        self._saga = None

    @property
    def worker(self) -> LiquidationWorker:
        return self._worker

    def attach_protection(self, saga) -> None:
        """The saga is both the protection port and the source of unhandled failures."""
        self._saga = saga
        self._service.attach_protection(saga)

    # -- entry points (serialized) ----------------------------------------------------

    def start(self, *args, **kwargs):
        return self._worker.call(self._service.start, *args, **kwargs)

    def rescan(self):
        """Start a flatten for every unhandled protective failure, then rescan every root.

        trader_service's recovery tick calls this on the worker, so the
        failures are picked up on every tick.
        """
        return self._worker.call(self._tick)

    tick = rescan

    async def tick_async(self):
        return await self._worker.run_async(self._tick)

    def upgrade_to_zero(self, root_id: str):
        return self._worker.call(self._service.upgrade_to_zero, root_id)

    def liquidate(self, cmd):
        return self._worker.call(self._service.liquidate, cmd)

    def start_nowait(self, account_id: str, cause_command_id: str, deadline: dt.datetime) -> Future:
        future = self._worker.submit(self._service.start, account_id, cause_command_id, deadline)
        future.add_done_callback(_log_failure)
        return future

    def nonblocking(self) -> "_NonBlockingStart":
        return _NonBlockingStart(self)

    async def run_async(self, fn: Callable[..., Any], *args, **kwargs):
        """Run another component (the session controller) on the same worker."""
        return await self._worker.run_async(fn, *args, **kwargs)

    def _tick(self):
        if self._saga is not None:
            for command_id in self._saga.unhandled_failures(self._account_id):
                self._flatten_failure(command_id)
        return self._service.rescan()

    def _flatten_failure(self, command_id: str) -> None:
        """One failed saga must not stop the others or the rescan (D8)."""
        try:
            if self._service.root_for(command_id) is None:
                self._service.start(self._account_id, command_id,
                                    self._now() + dt.timedelta(seconds=self._deadline_seconds))
        except Exception:
            logging.getLogger(__name__).exception(
                "could not start the flatten for protective failure %s", command_id)

    # -- reads (any thread) --------------------------------------------------------------

    def receipt_for(self, root_id: str):
        return self._service.receipt_for(root_id)

    def root_for(self, command_id: str):
        return self._service.root_for(command_id)

    def close_resolution(self, command_id: str):
        return self._service.close_resolution(command_id)


class _NonBlockingStart:
    """What the protective saga gets: ``start`` queues the flatten and returns at once."""

    def __init__(self, serialized: SerializedLiquidation):
        self._serialized = serialized

    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime) -> None:
        self._serialized.start_nowait(account_id, cause_command_id, deadline)


def _log_failure(future: Future) -> None:
    exc = future.exception()
    if exc is not None:
        logging.getLogger(__name__).error("queued liquidation start failed: %s", exc)
