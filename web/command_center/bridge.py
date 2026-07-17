"""Journal bridge: the dashboard's only consumer of domain updates (spec §5.1, §11).

Runs on a dedicated thread. Never touches DuckDB, ZMQ raw-object RPC, or dill:
its only inputs are the typed query socket (fenced snapshot + quote baseline)
and the typed long-poll feed socket. State mutation crosses into the ASGI loop
exclusively via loop.call_soon_threadsafe.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from trader.domain.events import ReadDomainEventsResult, SnapshotWithCursor

logger = logging.getLogger("web.command_center.bridge")


class BridgeLifecycle(str, Enum):
    STARTING = "starting"
    SYNCHRONIZING = "synchronizing"
    LIVE = "live"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


def _safe_error(exc: BaseException) -> str:
    """Class + truncated message; never interpolates secrets or payloads."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


@dataclass
class _SourceHealth:
    state: str = "unknown"
    last_success: Optional[float] = None
    last_error: Optional[str] = None
    reconnects: int = 0
    # Set on the first failure since the last success; cleared on any
    # success. Lets a cold/never-connected outage (last_success still None)
    # still age into DISCONNECTED instead of hanging in DEGRADED forever.
    first_failure_at: Optional[float] = None


class DashboardEventBridge:
    def __init__(self, query_client, feed_client, state, fanout, loop, *,
                 poll_limit: int = 500, wait_ms: int = 10_000,
                 backoff_min: float = 0.5, backoff_max: float = 30.0,
                 disconnected_after: float = 60.0,
                 rng=random.random, monotonic=time.monotonic):
        self._query = query_client
        self._feed = feed_client
        self._state = state
        self._fanout = fanout
        self._loop = loop
        self._poll_limit = poll_limit
        self._wait_ms = wait_ms
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._disconnected_after = disconnected_after
        self._rng = rng
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cursor: Optional[int] = None
        self._reconnects = 0
        self.lifecycle = BridgeLifecycle.STARTING
        self._sources: dict[str, _SourceHealth] = {
            "journal": _SourceHealth(), "snapshot": _SourceHealth()}

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="cc-event-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            # A long-poll can legitimately block inside the feed client for
            # up to wait_ms before it next checks _stop. Join at least that
            # long (plus a margin), regardless of the caller's timeout, so
            # stop() never returns with the thread still alive -- a lingering
            # thread could later fire a callback onto a loop the caller has
            # since torn down.
            min_join = self._wait_ms / 1000.0 + 1.0
            self._thread.join(timeout=max(timeout, min_join))

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _schedule(self, callback) -> None:
        """Cross onto the ASGI loop, guarded against a closed/None loop.

        A callback scheduled by a lingering bridge thread (e.g. after stop()
        gives up on an unusually slow join) must never raise past this call
        uncontrolled -- it should surface as a normal, catchable failure.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("bridge: cannot schedule onto a closed event loop")
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError as exc:
            raise RuntimeError(f"bridge: event loop rejected callback: {exc}") from exc

    def _run(self) -> None:
        backoff = self._backoff_min
        while not self._stop.is_set():
            phase = "snapshot"
            try:
                self.lifecycle = BridgeLifecycle.SYNCHRONIZING
                self._resync_baseline()
                backoff = self._backoff_min
                self.lifecycle = BridgeLifecycle.LIVE
                phase = "journal"
                self._tail()
            except Exception as exc:  # noqa: BLE001 — every failure degrades, retries
                if self._stop.is_set():
                    break
                self._record_error(phase, exc)
                self._reconnects += 1
                delay = min(backoff, self._backoff_max) * (0.5 + self._rng())
                logger.warning("bridge degraded (%s); retrying in %.2fs",
                               _safe_error(exc), delay)
                self._stop.wait(delay)
                backoff = min(backoff * 2, self._backoff_max)

    # -- fenced baseline --------------------------------------------------------
    def _resync_baseline(self) -> None:
        baseline: SnapshotWithCursor = self._query.call(
            "snapshot_with_cursor", {}, SnapshotWithCursor)
        quotes = self._query.call("get_quotes_snapshot", {}, dict)
        stream_id = uuid.uuid4().hex  # every resync rotates the stream
        installed = threading.Event()

        def _install() -> None:
            # Existing clients must resnapshot: their sequence space is gone.
            self._fanout.broadcast_resync()
            self._state.install_baseline(baseline, stream_id)
            self._state.apply_quotes(dict(quotes.get("quotes") or {}))
            installed.set()

        self._schedule(_install)
        if not installed.wait(timeout=10):
            raise TimeoutError("reducer loop did not install baseline within 10s")
        self._cursor = baseline.source_cursor
        self._record_success("snapshot")

    # -- long-poll tail ---------------------------------------------------------
    def _tail(self) -> None:
        while not self._stop.is_set():
            result: ReadDomainEventsResult = self._feed.call(
                "read_domain_events",
                {"after_cursor": self._cursor, "limit": self._poll_limit,
                 "wait_ms": self._wait_ms},
                ReadDomainEventsResult)
            self._record_success("journal")
            if not result.events:
                continue  # ten-second empty heartbeat
            events = tuple(result.events)
            applied = threading.Event()
            error_holder: list[BaseException] = []
            self._schedule(self._make_apply_callback(events, applied, error_holder))
            applied_ok = applied.wait(timeout=10)  # natural backpressure: one batch in flight
            if not applied_ok:
                raise TimeoutError("reducer loop did not apply tail batch within 10s")
            if error_holder:
                raise error_holder[0]
            # Only advance past a batch confirmed applied on the loop --
            # otherwise the next read_domain_events(after_cursor=...) would
            # silently skip these events forever (a read-model gap while the
            # bridge still reports LIVE).
            self._cursor = events[-1].source_cursor

    def _make_apply_callback(self, events, applied, error_holder):
        """Build the loop-side apply callback, binding this iteration's batch
        by parameter rather than by closure-over-loop-variable.

        ``_tail`` reassigns its local ``events``/``applied`` every iteration
        of its while loop; a nested closure that merely references those
        names would resolve them by cell at call time, not at definition
        time. If a wait ever timed out while a callback was still queued on
        the loop, a later closure would alias whichever iteration happened to
        be current when it finally ran and corrupt the wrong batch. Passing
        them as arguments here gives each callback its own bound values.
        """
        def _apply() -> None:
            try:
                for event in events:
                    envelope = self._state.apply(event)
                    if envelope is not None:  # stale revisions ignored idempotently
                        self._fanout.publish(envelope)
            except Exception as exc:  # noqa: BLE001 — captured; re-raised on the bridge thread
                error_holder.append(exc)
            finally:
                applied.set()
        return _apply

    # -- health -------------------------------------------------------------------
    def _record_success(self, source: str) -> None:
        health = self._sources[source]
        health.state = "ok"
        health.last_success = self._monotonic()
        health.last_error = None
        health.first_failure_at = None

    def _record_error(self, source: str, exc: BaseException) -> None:
        health = self._sources[source]
        health.state = "error"
        health.last_error = _safe_error(exc)
        health.reconnects += 1
        now = self._monotonic()
        if health.last_success is not None:
            outage_seconds = now - health.last_success
        else:
            # Never had a success (cold/never-connected outage): age from
            # the first observed failure instead of hanging in DEGRADED
            # forever because there's no "last success" to measure from.
            if health.first_failure_at is None:
                health.first_failure_at = now
            outage_seconds = now - health.first_failure_at
        if outage_seconds > self._disconnected_after:
            self.lifecycle = BridgeLifecycle.DISCONNECTED
        else:
            self.lifecycle = BridgeLifecycle.DEGRADED

    def health(self) -> dict:
        now = self._monotonic()
        sources = {}
        for name, h in self._sources.items():
            age = None if h.last_success is None else round(now - h.last_success, 3)
            sources[name] = {"state": h.state, "last_success_age_seconds": age,
                             "last_error": h.last_error, "reconnects": h.reconnects}
        return {"lifecycle": self.lifecycle.value, "reconnects": self._reconnects,
                "cursor": self._cursor, "sources": sources}
