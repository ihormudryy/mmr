"""Loop-owned dashboard read model (spec §5.2, §6, §7).

Only reducer callbacks scheduled on the ASGI loop mutate this state, so a
synchronous read (snapshot_view / replay_after) cannot observe a partially
applied event. It is a UI read model, not a second source of truth.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections import OrderedDict, deque
from typing import Optional

from trader.domain.events import DomainEvent, SnapshotWithCursor

logger = logging.getLogger("web.command_center.state")

SCHEMA_VERSION = 1
REPLAY_RING_MAX_EVENTS = 10_000
REPLAY_RING_MAX_AGE_SECONDS = 300.0
TERMINAL_CAP = 500
TERMINAL_TTL_SECONDS = 24 * 3600
CLEANUP_INTERVAL_SECONDS = 60.0

ACTIVE_PROPOSAL_STATUSES = {"PENDING", "APPROVED"}
TERMINAL_ORDER_STATUSES = {
    "FILLED",
    "CANCELLED",
    "CANCELLED_AFTER_PARTIAL",
    "REJECTED",
    "INACTIVE",
}


def _utc_iso(epoch_seconds: float) -> str:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc).isoformat()


class _TerminalStore:
    """Insertion-ordered terminal records: cap 500, TTL 24 h."""

    def __init__(self, cap: int = TERMINAL_CAP, ttl: float = TERMINAL_TTL_SECONDS):
        self._cap = cap
        self._ttl = ttl
        self._rows: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def put(self, entity_id: str, row: dict, now: float) -> None:
        self._rows.pop(entity_id, None)
        self._rows[entity_id] = (now, row)
        while len(self._rows) > self._cap:
            self._rows.popitem(last=False)

    def remove(self, entity_id: str) -> None:
        self._rows.pop(entity_id, None)

    def cleanup(self, now: float) -> None:
        for key in [key for key, (then, _) in self._rows.items() if now - then > self._ttl]:
            del self._rows[key]

    def values(self) -> list[dict]:
        return [row for _, row in self._rows.values()]


class DashboardState:
    def __init__(self, *, clock=time.time, monotonic=time.monotonic):
        self._clock = clock
        self._monotonic = monotonic
        self.stream_id: str = uuid.uuid4().hex
        self.sequence: int = 0
        self.has_baseline: bool = False
        self.last_event_at: Optional[str] = None
        self.last_transport_lag_ms: Optional[float] = None
        self.quotes: dict[str, dict] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._ring: deque[tuple[int, float, dict]] = deque()
        self._last_cleanup: float = monotonic()
        self._reset_collections()

    def _reset_collections(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.positions: dict[str, dict] = {}
        self.proposals_active: dict[str, dict] = {}
        self.orders_active: dict[str, dict] = {}
        self.strategies: dict[str, dict] = {}
        self.risk: dict[str, dict] = {}
        self.reconciliation: dict[str, dict] = {}
        self.trading_control: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.proposals_terminal = _TerminalStore()
        self.orders_terminal = _TerminalStore()
        self.fills = _TerminalStore()

    def install_baseline(self, snapshot: SnapshotWithCursor, stream_id: str) -> None:
        for entity_type, rows in snapshot.entities.items():
            for row in rows:
                if "entity_id" not in row or "entity_revision" not in row:
                    raise ValueError(
                        f"snapshot row for {entity_type!r} lacks entity_id/"
                        "entity_revision — refusing an unfenced baseline"
                    )
        self.stream_id = stream_id
        self.sequence = 0
        self._ring.clear()
        self._revisions.clear()
        self._reset_collections()
        now = self._monotonic()
        for entity_type, rows in snapshot.entities.items():
            for row in rows:
                entity_id = str(row["entity_id"])
                self._place(entity_type, entity_id, dict(row), now)
                self._revisions[(entity_type, entity_id)] = int(row["entity_revision"])
        self.has_baseline = True

    def apply(self, event: DomainEvent) -> Optional[dict]:
        key = (event.entity_type, event.entity_id)
        if event.entity_revision <= self._revisions.get(key, 0):
            return None
        self._revisions[key] = event.entity_revision
        now = self._monotonic()
        if event.operation == "delete":
            self._remove(event.entity_type, event.entity_id)
        else:
            row = dict(event.payload or {})
            row["entity_id"] = event.entity_id
            row["entity_revision"] = event.entity_revision
            self._place(event.entity_type, event.entity_id, row, now)
        envelope = self._envelope(event)
        self._ring.append((envelope["sequence"], now, envelope))
        self._prune_ring(now)
        self.last_event_at = envelope["source_timestamp"]
        self.maybe_cleanup(now)
        return envelope

    def apply_quotes(self, batch: dict[str, dict]) -> None:
        """Latest-value only. Quotes never enter the ring or advance sequence."""
        self.quotes.update(batch)

    def _envelope(self, event: DomainEvent) -> dict:
        self.sequence += 1
        received = self._clock()
        # Source-timestamp -> reducer-applied lag for the newest event, used
        # by the command-center health surface (spec §12). Clamped at 0 so a
        # skewed/slightly-ahead source clock never reports a negative lag.
        self.last_transport_lag_ms = max(
            0.0, received * 1000.0 - event.source_timestamp.timestamp() * 1000.0)
        return {
            "schema_version": SCHEMA_VERSION,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "event_id": event.event_id,
            "source_cursor": event.source_cursor,
            "entity_revision": event.entity_revision,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "operation": event.operation,
            "account_id": event.account_id,
            "source": event.source,
            "source_timestamp": event.source_timestamp.isoformat(),
            "received_timestamp": _utc_iso(received),
            "correlation_id": event.correlation_id,
            "payload": event.payload,
        }

    def _place(self, entity_type: str, entity_id: str, row: dict, now: float) -> None:
        status = str(row.get("status", "")).upper()
        if entity_type == "proposal":
            if status in ACTIVE_PROPOSAL_STATUSES:
                self.proposals_active[entity_id] = row
                self.proposals_terminal.remove(entity_id)
            else:
                self.proposals_active.pop(entity_id, None)
                self.proposals_terminal.put(entity_id, row, now)
        elif entity_type == "order":
            if status in TERMINAL_ORDER_STATUSES:
                self.orders_active.pop(entity_id, None)
                self.orders_terminal.put(entity_id, row, now)
            else:
                self.orders_active[entity_id] = row
                self.orders_terminal.remove(entity_id)
        elif entity_type == "fill":
            self.fills.put(entity_id, row, now)
        elif entity_type == "account":
            self.accounts[entity_id] = row
        elif entity_type == "position":
            self.positions[entity_id] = row
        elif entity_type == "strategy":
            self.strategies[entity_id] = row
        elif entity_type == "risk":
            self.risk[entity_id] = row
        elif entity_type == "reconciliation":
            self.reconciliation[entity_id] = row
        elif entity_type == "trading_control":
            self.trading_control[entity_id] = row
        elif entity_type == "command":
            self.commands[entity_id] = row
        else:
            logger.warning("unknown entity_type %r ignored", entity_type)

    def _remove(self, entity_type: str, entity_id: str) -> None:
        for collection in (
            self.accounts,
            self.positions,
            self.proposals_active,
            self.orders_active,
            self.strategies,
            self.risk,
            self.reconciliation,
            self.trading_control,
            self.commands,
        ):
            collection.pop(entity_id, None)
        if entity_type == "proposal":
            self.proposals_terminal.remove(entity_id)
        elif entity_type == "order":
            self.orders_terminal.remove(entity_id)
        elif entity_type == "fill":
            self.fills.remove(entity_id)
        self.quotes.pop(entity_id, None)

    def replay_after(self, stream_id: str, sequence: int) -> Optional[list[dict]]:
        if stream_id != self.stream_id:
            return None
        if sequence >= self.sequence:
            return []
        if not self._ring or self._ring[0][0] > sequence + 1:
            return None
        return [envelope for ring_sequence, _, envelope in self._ring if ring_sequence > sequence]

    def snapshot_view(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "generated_at": _utc_iso(self._clock()),
            "has_baseline": self.has_baseline,
            "last_event_at": self.last_event_at,
            "accounts": list(self.accounts.values()),
            "positions": list(self.positions.values()),
            "quotes": dict(self.quotes),
            "proposals": {
                "active": list(self.proposals_active.values()),
                "terminal": self.proposals_terminal.values(),
            },
            "orders": {
                "active": list(self.orders_active.values()),
                "terminal": self.orders_terminal.values(),
            },
            "fills": self.fills.values(),
            "strategies": list(self.strategies.values()),
            "risk": dict(self.risk),
            "reconciliation": list(self.reconciliation.values()),
            "trading_control": list(self.trading_control.values()),
            "commands": list(self.commands.values()),
        }

    def _prune_ring(self, now: float) -> None:
        while len(self._ring) > REPLAY_RING_MAX_EVENTS:
            self._ring.popleft()
        while self._ring and now - self._ring[0][1] > REPLAY_RING_MAX_AGE_SECONDS:
            self._ring.popleft()

    def maybe_cleanup(self, now: Optional[float] = None) -> None:
        now = self._monotonic() if now is None else now
        if now - self._last_cleanup < CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup = now
        for store in (self.proposals_terminal, self.orders_terminal, self.fills):
            store.cleanup(now)
        self._prune_ring(now)
