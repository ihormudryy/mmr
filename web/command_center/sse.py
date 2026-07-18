"""SSE fan-out registry and per-client bounds (spec §7).

Every method runs on the ASGI loop only; the reducer (``DashboardState``,
driven by ``DashboardEventBridge``) schedules ``publish``/``publish_quotes``
calls via ``call_soon_threadsafe``, so ``register()`` is atomic with respect
to event application -- the replay cutover can neither miss nor duplicate an
event: a client registered between two applied events either sees the older
event only via replay (its cursor already covers it) or only via its live
FIFO (registered before the event was applied), never both and never
neither.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from web.command_center.state import DashboardState

logger = logging.getLogger("web.command_center.sse")

# Bound on how many un-delivered domain-event envelopes a single client's
# FIFO may hold before it is treated as "too slow": the client is dropped
# into a forced resync instead of allowed to grow the queue unbounded (which
# would eventually OOM a client that never reads, e.g. a backgrounded tab).
FIFO_LIMIT = 1000
QUOTE_MAP_LIMIT = 500


class SseClient:
    __slots__ = ("fifo", "quote_map", "wake", "resync", "closed")

    def __init__(self):
        self.fifo: deque[dict] = deque()
        self.quote_map: dict[str, dict] = {}
        self.wake = asyncio.Event()
        self.resync = False
        self.closed = False


def _parse_event_id(value: str) -> Optional[tuple[str, int]]:
    """Parse a Last-Event-ID / ``?after=`` value of the form ``stream:seq``.

    Returns ``None`` on anything unparsable so the caller can distinguish
    "malformed cursor" from a legitimate stale/aged one -- both currently
    surface the same way (no replay), but keeping the parse failure distinct
    avoids conflating the two if that ever needs to change.
    """
    try:
        stream_id, seq = value.rsplit(":", 1)
        return stream_id, int(seq)
    except (ValueError, AttributeError):
        return None


class SseFanout:
    """Registry of connected SSE clients plus the replay handshake.

    Constructed once per ``CommandCenter`` (one fan-out per reducer). Every
    method here is called from the ASGI loop only -- ``register`` reads
    ``DashboardState`` synchronously (no lock needed, single-threaded
    cooperative scheduling), and ``publish``/``publish_quotes`` are always
    invoked via ``call_soon_threadsafe`` from the bridge/quote-plane threads,
    never called directly off-loop.
    """

    def __init__(self, state: DashboardState):
        self._state = state
        self._clients: set[SseClient] = set()

    def register(
        self, last_event_id: Optional[str]
    ) -> tuple[SseClient, Optional[list[dict]], dict[str, dict]]:
        """Register a new client and compute its replay + quote baseline.

        Returns ``(client, replay, quote_baseline)``. ``replay`` is ``None``
        when the supplied cursor can't be honored (unparsable, stale stream,
        or aged out of the ring) -- the caller must send a
        ``resync_required`` frame and stop, never guess a partial replay.
        An empty list means "you are already caught up". The client is
        added to the registry regardless of the cursor outcome, so any event
        published from this point forward (including one applied
        concurrently with this call, since everything is on one loop) lands
        in its live FIFO with no gap and no duplicate against the replay.
        """
        client = SseClient()
        replay: Optional[list[dict]] = []
        if last_event_id:
            parsed = _parse_event_id(last_event_id)
            replay = (
                None if parsed is None
                else self._state.replay_after(parsed[0], parsed[1])
            )
        self._clients.add(client)
        return client, replay, dict(self._state.quotes)

    def unregister(self, client: SseClient) -> None:
        client.closed = True
        self._clients.discard(client)

    def client_count(self) -> int:
        return len(self._clients)

    def max_fifo_depth(self) -> int:
        """Deepest client replay FIFO right now (bound is `FIFO_LIMIT`). A
        pure read, no side effects -- exposed on `/api/cc-health` as
        `client_fifo_depth_max` so the soak runner can sample it for the
        previously fail-closed `max_client_fifo_depth` COMPAT threshold."""
        return max((len(c.fifo) for c in self._clients), default=0)

    def publish(self, envelope: dict) -> None:
        """Fan a journaled domain-event envelope out to every live client.

        A client already flagged ``resync`` is skipped entirely (it has
        already been told to resync and must not accumulate more state
        until it reconnects). A client at capacity is dropped into resync
        right here -- its FIFO and quote_map are cleared so a subsequent
        read never mixes pre- and post-overflow state -- but this NEVER
        blocks or slows delivery to any other client: the loop below is a
        plain iteration with no per-client I/O or backpressure.
        """
        if self._state.consume_retention_eviction():
            # An eviction is an implicit delete from the snapshot, but the
            # journal has only the triggering upsert. Send the established
            # resync control frame rather than leave an uninterrupted browser
            # retaining a row the bounded read model has discarded.
            self.broadcast_resync()
            return
        for client in list(self._clients):
            if client.resync:
                continue
            if len(client.fifo) >= FIFO_LIMIT:
                # A slow tab never blocks source consumption: drop its state,
                # force resynchronization, keep everyone else flowing.
                client.fifo.clear()
                client.quote_map.clear()
                client.resync = True
            else:
                client.fifo.append(envelope)
            client.wake.set()

    def publish_quotes(self, batch: dict[str, dict]) -> None:
        """Conflate a quote batch into the reducer and every live client.

        Quotes are latest-value-only and never consume FIFO capacity or
        advance the replay sequence (mirrors ``DashboardState.apply_quotes``)
        -- a client backlogged on domain events still gets fresh quotes, and
        a flood of quote updates can never itself trigger a forced resync.
        """
        self._state.apply_quotes(batch)
        if self._state.consume_retention_eviction():
            # quote.updated is merge-only in the browser. A bounded quote-map
            # eviction therefore needs the snapshot path, which replaces the
            # browser's quote map rather than retaining the stale key.
            self.broadcast_resync()
            return
        for client in self._clients:
            if client.resync:
                continue
            for instrument_id, quote in batch.items():
                # Reinsert updates at the tail so cap eviction drops the least
                # recently updated quote, not an actively changing one.
                client.quote_map.pop(instrument_id, None)
                client.quote_map[instrument_id] = quote
            while len(client.quote_map) > QUOTE_MAP_LIMIT:
                oldest = next(iter(client.quote_map))
                client.quote_map.pop(oldest)
            client.wake.set()

    def broadcast_resync(self) -> None:
        """Force every connected client to resync (e.g. bridge re-baseline).

        Used when the stream identity itself changes underneath existing
        clients (a new fenced snapshot rotates ``stream_id``) -- their
        sequence space is gone, so replay is not just aged but meaningless.
        """
        for client in list(self._clients):
            client.fifo.clear()
            client.quote_map.clear()
            client.resync = True
            client.wake.set()
