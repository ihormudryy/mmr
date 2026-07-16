"""Conflated quote plane (spec §5.1, §6 quote.updated row, §7 bounds).

A dedicated SUB thread drains ticker PubSub, conflates to a latest-value map
per canonical instrument, and hands batches to the loop at 2-5 Hz. Quotes are
ephemeral: they never touch the journal, the replay ring, or client FIFOs.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

import zmq

logger = logging.getLogger("web.command_center.quotes")

DEFAULT_QUOTE_HZ = 4.0


def clamp_hz(hz: float) -> float:
    return min(5.0, max(2.0, float(hz)))


def _default_decode(payload: bytes):
    """Refuse to run unless the dashboard process has explicitly opted in.

    ``trader.messaging.clientserver.unpack`` already gates ``EXT_OBJECT``
    dill payloads behind ``MMR_DILL_STRICT`` internally, but the dashboard
    is a read-only surface that should never even attempt dill
    deserialization of untrusted-looking data -- so this seam re-checks the
    same env var independently before delegating, rather than trusting a
    single gate deep in a shared module.
    """
    if os.environ.get("MMR_DILL_STRICT") != "1":
        raise RuntimeError(
            "command center requires MMR_DILL_STRICT=1 — the dashboard never "
            "executes dill payloads (set it in the dashboard service environment)")
    from trader.messaging.clientserver import unpack
    return unpack(payload)


_CONID_KEYS = ("conId", "conid", "con_id", "instrument_id")
_TIME_KEYS = ("time", "market_time", "timestamp", "date")


def normalize_ticker(obj) -> Optional[dict]:
    """Normalize a PubSub ticker payload to the quote row of spec §5.4.

    Handles plain dicts and attribute-style objects. Anything without a
    resolvable canonical conId is dropped (never keyed by display symbol,
    never guessed).
    """
    get = obj.get if isinstance(obj, dict) else (
        lambda key, default=None: getattr(obj, key, default))
    try:
        con_id = next((get(k) for k in _CONID_KEYS if get(k) is not None), None)
        if con_id is None:
            return None
        market_time = next((get(k) for k in _TIME_KEYS if get(k) is not None), None)
        return {
            "instrument_id": str(int(con_id)),
            "bid": _num(get("bid")),
            "ask": _num(get("ask")),
            "last": _num(get("last")),
            "market_timestamp": str(market_time) if market_time is not None else None,
            "feed_type": get("feed") or get("feed_type"),
        }
    except (TypeError, ValueError, AttributeError):
        return None


def _num(value) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if number == number else None  # NaN -> None


class QuotePlane:
    """Conflated ticker PubSub subscriber, decoupled from the ASGI loop.

    Runs its own SUB socket on a dedicated background thread. Ingested
    tickers are conflated to a latest-value-per-instrument map and handed to
    ``deliver`` on the caller's loop via ``call_soon_threadsafe`` at most
    once per ``1/hz`` seconds -- never more often, regardless of ticker
    volume.
    """

    def __init__(self, address: str, port: int, loop,
                 deliver: Callable[[dict], None], *,
                 hz: float = DEFAULT_QUOTE_HZ, topic: str = "",
                 decode: Optional[Callable[[bytes], object]] = None):
        self._endpoint = f"{address}:{port}"
        self._loop = loop
        self._deliver = deliver
        self._interval = 1.0 / clamp_hz(hz)
        self._topic = topic
        self._decode = decode or _default_decode
        self._pending: dict[str, dict] = {}
        self._last_flush = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0

    # Testable units — the thread loop is just poll -> ingest -> flush_due.
    def ingest(self, obj) -> None:
        quote = normalize_ticker(obj)
        if quote is None:
            self.dropped += 1
            return
        self._pending[quote["instrument_id"]] = quote  # conflate: latest wins

    def flush_due(self, now: float) -> bool:
        if not self._pending or now - self._last_flush < self._interval:
            return False
        batch, self._pending = self._pending, {}
        self._last_flush = now
        return self._deliver_to_loop(batch)

    def _deliver_to_loop(self, batch: dict) -> bool:
        """Cross onto the ASGI loop, guarded against a closed/None loop.

        A lingering thread (e.g. after ``stop()`` gives up on an unusually
        slow join) must never raise past this call, and must never attempt
        to schedule a callback onto a loop that has since been torn down.
        """
        loop = self._loop
        is_closed = getattr(loop, "is_closed", None)
        if callable(is_closed) and is_closed():
            logger.warning(
                "quote plane: event loop is closed, dropping a batch of %d quotes",
                len(batch))
            return False
        try:
            loop.call_soon_threadsafe(self._deliver, batch)
        except RuntimeError:
            logger.warning("quote plane: event loop rejected batch (closed?)")
            return False
        return True

    # -- thread ---------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="cc-quote-plane", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.connect(self._endpoint)
        sock.setsockopt_string(zmq.SUBSCRIBE, self._topic)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                for _sock, _mask in poller.poll(timeout=50):
                    frames = sock.recv_multipart(zmq.NOBLOCK)
                    if len(frames) < 2:
                        continue
                    try:
                        self.ingest(self._decode(frames[1]))
                    except Exception:  # noqa: BLE001 — malformed payloads drop
                        self.dropped += 1
                self.flush_due(time.monotonic())
        finally:
            sock.close(0)
            ctx.term()
