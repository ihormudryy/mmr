"""P4 Task 1 — durable, append-only promotion evidence + derived windows.

Raw evidence lives in ``promotion_evidence_events``: every row is inserted
exactly once, keyed by an idempotent ``source_event_id`` the caller supplies
(the id of an authoritative P3 attribution/replay/breaker event, or a stable
id for an operator-issued correction) — a duplicate id is a no-op, never an
overwrite. ``promotion_evidence_windows`` is a derived per-strategy
projection: ``EvidenceStore.project`` always fully rebuilds it from raw
evidence via the pure ``project_evidence_window`` function (never patches it
in place), so it is safe to rebuild after a crash, a correction, or simply
on demand. Journal migrations 40-41; migration 42 (``strategy_promotion_state``)
lives in ``trader/promotion/stage.py``.

This module deliberately has no dependency on ``trader_service`` or live IB
connectivity — callers hand it plain event payloads and source event ids
derived from P3's attribution/replay/breaker evidence, never the live
objects themselves.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import strategy_entity_id

EVIDENCE_MIGRATION_40 = 40
EVIDENCE_MIGRATION_41 = 41
EVIDENCE_MIGRATION_VERSIONS = (EVIDENCE_MIGRATION_40, EVIDENCE_MIGRATION_41)

EVIDENCE_MIGRATION_40_NAME = "p4_promotion_evidence_events"
EVIDENCE_MIGRATION_41_NAME = "p4_promotion_evidence_windows"

# Evidence event kinds recognized by the pure projection below. Corrections
# carry a ``scope`` in ``CORRECTION_SCOPES``; the other kinds are raw facts
# about a strategy's paper/live evidence.
EVENT_KIND_SESSION = "session"
EVENT_KIND_ROUND_TRIP = "round_trip"
EVENT_KIND_INSTRUMENT = "instrument"
EVENT_KIND_CORRECTION = "correction"
EVENT_KIND_BREAKER_TRIP = "breaker_trip"
EVENT_KIND_COST_BREACH = "cost_breach"
EVENT_KIND_DRAWDOWN_BREACH = "drawdown_breach"

CORRECTION_SCOPES = frozenset({"code", "config", "allowlist", "risk", "data"})

# Every event_kind recognized by the pure projection above. An unrecognized
# kind (e.g. a typo like "braker_trip") must never be accepted and silently
# dropped from projection -- that would make safety evidence vanish without
# any error. Validated eagerly in ``EvidenceEvent.__post_init__`` so it can
# never even be constructed, let alone appended.
EVENT_KINDS = frozenset({
    EVENT_KIND_SESSION,
    EVENT_KIND_ROUND_TRIP,
    EVENT_KIND_INSTRUMENT,
    EVENT_KIND_CORRECTION,
    EVENT_KIND_BREAKER_TRIP,
    EVENT_KIND_COST_BREACH,
    EVENT_KIND_DRAWDOWN_BREACH,
})

# 30-day evidence inactivity floor (plan Global Constraint / Task 1 checklist).
STALE_AFTER = dt.timedelta(days=30)


def apply_evidence_migrations(migrator: SchemaMigrator) -> bool:
    """Apply journal migrations 40-41. Returns True if any newly applied."""
    applied = False
    applied |= migrator.apply(
        EVIDENCE_MIGRATION_40,
        EVIDENCE_MIGRATION_40_NAME,
        (
            """CREATE TABLE IF NOT EXISTS promotion_evidence_events (
                evidence_key VARCHAR PRIMARY KEY,
                strategy_id VARCHAR NOT NULL,
                event_kind VARCHAR NOT NULL,
                payload VARCHAR NOT NULL,
                source_timestamp TIMESTAMPTZ NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_promotion_evidence_events_strategy
                ON promotion_evidence_events(strategy_id)""",
            """CREATE INDEX IF NOT EXISTS idx_promotion_evidence_events_kind
                ON promotion_evidence_events(event_kind)""",
        ),
    )
    applied |= migrator.apply(
        EVIDENCE_MIGRATION_41,
        EVIDENCE_MIGRATION_41_NAME,
        (
            """CREATE TABLE IF NOT EXISTS promotion_evidence_windows (
                strategy_id VARCHAR PRIMARY KEY,
                payload VARCHAR NOT NULL,
                rebuilt_at TIMESTAMPTZ NOT NULL
            )""",
        ),
    )
    return applied


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    return _as_utc(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")))


@dataclass(frozen=True)
class EvidenceEvent:
    """One raw, authoritative evidence event for a strategy's promotion.

    ``source_event_id`` is the idempotency key — callers pass through the id
    of the authoritative P3 event they are recording evidence from (an
    attribution trade_id, a replay seal id, a breaker incident id, ...), or a
    stable id for an operator-issued correction. Appending the same
    ``source_event_id`` twice is a no-op; the first payload always wins.
    """
    source_event_id: str
    strategy_id: str
    event_kind: str
    payload: Mapping[str, Any]
    source_timestamp: dt.datetime

    def __post_init__(self) -> None:
        if not self.source_event_id:
            raise ValueError("source_event_id is required")
        if not self.strategy_id:
            raise ValueError("strategy_id is required")
        if not self.event_kind:
            raise ValueError("event_kind is required")
        if self.event_kind not in EVENT_KINDS:
            raise ValueError(
                f"event_kind must be one of {sorted(EVENT_KINDS)}, got {self.event_kind!r}"
            )
        if self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware")
        if self.event_kind == EVENT_KIND_CORRECTION:
            scope = self.payload.get("scope")
            if scope not in CORRECTION_SCOPES:
                raise ValueError(
                    f"correction scope must be one of {sorted(CORRECTION_SCOPES)}, "
                    f"got {scope!r}"
                )


@dataclass(frozen=True)
class EvidenceWindow:
    """Pure, rebuildable projection of a strategy's promotion evidence.

    ``window_reset_at`` is the timestamp of the most recent correction event
    (any scope) — every counter below (sessions/round trips/instruments/
    calendar days) counts only evidence at or after that boundary, per the
    plan's correction-impact rule (Task 2 defines the concrete gate-reset
    semantics; this is the generic primitive it builds on). ``corrections``
    always lists the FULL, never-reset correction history for audit.
    ``stale`` is true when there is no in-window evidence within
    ``STALE_AFTER`` (30 days) of ``as_of``.
    """
    strategy_id: str
    as_of: dt.datetime
    window_reset_at: Optional[dt.datetime]
    first_event_at: Optional[dt.datetime]
    last_event_at: Optional[dt.datetime]
    calendar_days: tuple[str, ...]
    session_ids: tuple[str, ...]
    round_trip_ids: tuple[str, ...]
    instrument_ids: tuple[str, ...]
    corrections: tuple[dict[str, Any], ...]
    breaker_trips: tuple[dict[str, Any], ...]
    cost_breaches: tuple[dict[str, Any], ...]
    drawdown_breaches: tuple[dict[str, Any], ...]
    stale: bool
    event_count: int

    @property
    def calendar_day_count(self) -> int:
        return len(self.calendar_days)

    @property
    def session_count(self) -> int:
        return len(self.session_ids)

    @property
    def round_trip_count(self) -> int:
        return len(self.round_trip_ids)

    @property
    def instrument_count(self) -> int:
        return len(self.instrument_ids)

    @property
    def is_clean(self) -> bool:
        """No unresolved safety evidence and not stale.

        Gate helpers (paper gate, live metrics, canary risk — Tasks 2/3/6)
        build their specific floors on top of this; Task 1 only computes
        the raw signal.
        """
        return (
            not self.stale
            and not self.breaker_trips
            and not self.cost_breaches
            and not self.drawdown_breaches
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "as_of": _iso(self.as_of),
            "window_reset_at": _iso(self.window_reset_at) if self.window_reset_at else None,
            "first_event_at": _iso(self.first_event_at) if self.first_event_at else None,
            "last_event_at": _iso(self.last_event_at) if self.last_event_at else None,
            "calendar_days": list(self.calendar_days),
            "session_ids": list(self.session_ids),
            "round_trip_ids": list(self.round_trip_ids),
            "instrument_ids": list(self.instrument_ids),
            "corrections": list(self.corrections),
            "breaker_trips": list(self.breaker_trips),
            "cost_breaches": list(self.cost_breaches),
            "drawdown_breaches": list(self.drawdown_breaches),
            "stale": self.stale,
            "event_count": self.event_count,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvidenceWindow":
        data = dict(payload)
        return cls(
            strategy_id=data["strategy_id"],
            as_of=_parse_ts(data["as_of"]),
            window_reset_at=_parse_ts(data["window_reset_at"]) if data.get("window_reset_at") else None,
            first_event_at=_parse_ts(data["first_event_at"]) if data.get("first_event_at") else None,
            last_event_at=_parse_ts(data["last_event_at"]) if data.get("last_event_at") else None,
            calendar_days=tuple(data.get("calendar_days") or ()),
            session_ids=tuple(data.get("session_ids") or ()),
            round_trip_ids=tuple(data.get("round_trip_ids") or ()),
            instrument_ids=tuple(data.get("instrument_ids") or ()),
            corrections=tuple(data.get("corrections") or ()),
            breaker_trips=tuple(data.get("breaker_trips") or ()),
            cost_breaches=tuple(data.get("cost_breaches") or ()),
            drawdown_breaches=tuple(data.get("drawdown_breaches") or ()),
            stale=bool(data["stale"]),
            event_count=int(data["event_count"]),
        )


def _latest_correction_ts(ordered_events: Sequence[Mapping[str, Any]]) -> Optional[dt.datetime]:
    latest: Optional[dt.datetime] = None
    for event in ordered_events:
        if event["event_kind"] == EVENT_KIND_CORRECTION:
            ts = _as_utc(event["source_timestamp"])
            if latest is None or ts > latest:
                latest = ts
    return latest


def project_evidence_window(
    strategy_id: str,
    events: Sequence[Mapping[str, Any]],
    as_of: dt.datetime,
) -> EvidenceWindow:
    """Pure rebuild of the derived evidence window from raw evidence rows.

    Deterministic regardless of append order: events are re-sorted by
    ``(source_timestamp, evidence_key)`` before folding, so out-of-order
    delivery (e.g. a correction recorded before the events it postdates, or
    vice versa) never changes the result. Takes no DB dependency — safe to
    unit test directly, and reused by ``EvidenceStore.project`` for the
    durable, atomically-persisted path.
    """
    as_of = _as_utc(as_of)
    ordered = sorted(
        events,
        key=lambda e: (_as_utc(e["source_timestamp"]), str(e.get("evidence_key") or "")),
    )

    corrections = tuple(
        {**dict(event["payload"]),
         "evidence_key": event.get("evidence_key"),
         "source_timestamp": _iso(_as_utc(event["source_timestamp"]))}
        for event in ordered
        if event["event_kind"] == EVENT_KIND_CORRECTION
    )
    window_reset_at = _latest_correction_ts(ordered)

    in_window = [
        event for event in ordered
        if window_reset_at is None or _as_utc(event["source_timestamp"]) >= window_reset_at
    ]

    session_ids = sorted({
        str(event["payload"]["session_id"])
        for event in in_window
        if event["event_kind"] == EVENT_KIND_SESSION and event["payload"].get("session_id")
    })
    round_trip_ids = sorted({
        str(event["payload"].get("round_trip_id") or event["payload"].get("trade_id"))
        for event in in_window
        if event["event_kind"] == EVENT_KIND_ROUND_TRIP
        and (event["payload"].get("round_trip_id") or event["payload"].get("trade_id"))
    })
    instrument_ids = sorted({
        str(
            event["payload"]["instrument_id"]
            if event["payload"].get("instrument_id") is not None
            else event["payload"]["conid"]
        )
        for event in in_window
        if event["event_kind"] in (EVENT_KIND_INSTRUMENT, EVENT_KIND_ROUND_TRIP)
        and (event["payload"].get("instrument_id") is not None or event["payload"].get("conid") is not None)
    })
    calendar_days = sorted({
        _as_utc(event["source_timestamp"]).date().isoformat()
        for event in in_window
        if event["event_kind"] == EVENT_KIND_SESSION
    })
    breaker_trips = tuple(
        dict(event["payload"]) for event in in_window if event["event_kind"] == EVENT_KIND_BREAKER_TRIP
    )
    cost_breaches = tuple(
        dict(event["payload"]) for event in in_window if event["event_kind"] == EVENT_KIND_COST_BREACH
    )
    drawdown_breaches = tuple(
        dict(event["payload"]) for event in in_window if event["event_kind"] == EVENT_KIND_DRAWDOWN_BREACH
    )

    first_event_at = _as_utc(ordered[0]["source_timestamp"]) if ordered else None
    last_event_at = _as_utc(ordered[-1]["source_timestamp"]) if ordered else None
    last_in_window_at = _as_utc(in_window[-1]["source_timestamp"]) if in_window else None
    stale = last_in_window_at is None or (as_of - last_in_window_at) > STALE_AFTER

    return EvidenceWindow(
        strategy_id=strategy_id,
        as_of=as_of,
        window_reset_at=window_reset_at,
        first_event_at=first_event_at,
        last_event_at=last_event_at,
        calendar_days=tuple(calendar_days),
        session_ids=tuple(session_ids),
        round_trip_ids=tuple(round_trip_ids),
        instrument_ids=tuple(instrument_ids),
        corrections=corrections,
        breaker_trips=breaker_trips,
        cost_breaches=cost_breaches,
        drawdown_breaches=drawdown_breaches,
        stale=stale,
        event_count=len(ordered),
    )


class EvidenceStore:
    """Append-only evidence ingestion + derived window projection.

    ``append`` and ``project`` each commit their DuckDB write and the
    corresponding ``DomainMutation`` atomically via ``DomainJournal.mutate``
    — the domain event is observability only (dashboard/audit), never the
    source of truth; raw evidence in ``promotion_evidence_events`` is.
    """

    def __init__(self, journal: Any, db: Any, now: Optional[Callable[[], dt.datetime]] = None):
        self.journal = journal
        self.db = db
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def append(self, event: EvidenceEvent) -> bool:
        """Append raw evidence. Idempotent by ``source_event_id``; returns
        False on a duplicate id (the existing raw row is never overwritten).
        """
        existing = self.db.execute(
            "SELECT 1 FROM promotion_evidence_events WHERE evidence_key = ?",
            [event.source_event_id],
            fetch="one",
        )
        if existing is not None:
            return False

        now = _as_utc(self._now())
        inserted = {"ok": False}

        def write(conn: Any, _revision: int) -> None:
            row = conn.execute(
                "SELECT 1 FROM promotion_evidence_events WHERE evidence_key = ?",
                [event.source_event_id],
            ).fetchone()
            if row is not None:
                return
            conn.execute(
                "INSERT INTO promotion_evidence_events "
                "(evidence_key, strategy_id, event_kind, payload, source_timestamp, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    event.source_event_id,
                    event.strategy_id,
                    event.event_kind,
                    json.dumps(dict(event.payload), sort_keys=True, default=str),
                    _as_utc(event.source_timestamp),
                    now,
                ],
            )
            inserted["ok"] = True

        mutation = DomainMutation(
            event_type="promotion.evidence_appended",
            entity_type="promotion_evidence",
            entity_id=event.source_event_id,
            operation="upsert",
            account_id=None,
            source="trader_service",
            source_timestamp=_as_utc(event.source_timestamp),
            correlation_id=event.strategy_id,
            payload={
                "source_event_id": event.source_event_id,
                "strategy_id": event.strategy_id,
                "event_kind": event.event_kind,
                "payload": dict(event.payload),
            },
        )
        # evidence_key as journal event_id: a retried append with identical
        # canonical fields (including identical payload) is a safe no-op.
        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"promo-ev:{event.source_event_id}",
        )
        return bool(inserted["ok"])

    def list_events(self, strategy_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT evidence_key, strategy_id, event_kind, payload, "
            "source_timestamp, recorded_at "
            "FROM promotion_evidence_events WHERE strategy_id = ? "
            "ORDER BY source_timestamp ASC, evidence_key ASC",
            [strategy_id],
            fetch="all",
        )
        out: list[dict[str, Any]] = []
        for row in rows or ():
            out.append({
                "evidence_key": row[0],
                "strategy_id": row[1],
                "event_kind": row[2],
                "payload": json.loads(row[3]),
                "source_timestamp": row[4],
                "recorded_at": row[5],
            })
        return out

    def project(self, strategy_id: str, as_of: Optional[dt.datetime] = None) -> EvidenceWindow:
        """Rebuild the derived window from raw evidence and persist it.

        The math (``project_evidence_window``) is a pure function of the raw
        rows; this method is the durable side effect (replace-in-place
        persistence + atomic domain event), so a crash between an ``append``
        and a ``project`` call loses nothing — the next ``project`` call
        recomputes identically from surviving raw evidence.
        """
        resolved_as_of = _as_utc(as_of) if as_of is not None else _as_utc(self._now())
        events = self.list_events(strategy_id)
        window = project_evidence_window(strategy_id, events, resolved_as_of)

        mutation = DomainMutation(
            event_type="promotion.evidence_window_rebuilt",
            entity_type="promotion_evidence_window",
            entity_id=strategy_entity_id(strategy_id),
            operation="upsert",
            account_id=None,
            source="trader_service",
            source_timestamp=resolved_as_of,
            correlation_id=strategy_id,
            payload=window.to_payload(),
        )

        def write(conn: Any, _revision: int) -> None:
            conn.execute(
                "DELETE FROM promotion_evidence_windows WHERE strategy_id = ?",
                [strategy_id],
            )
            conn.execute(
                "INSERT INTO promotion_evidence_windows (strategy_id, payload, rebuilt_at) "
                "VALUES (?, ?, ?)",
                [
                    strategy_id,
                    json.dumps(window.to_payload(), sort_keys=True, default=str),
                    resolved_as_of,
                ],
            )

        # Unique event id per rebuild (a dashboard-observability event only;
        # promotion_evidence_windows is the derived table of record).
        self.journal.mutate(
            self.journal.connect(),
            mutation,
            write,
            event_id=f"promo-window:{strategy_id}:{uuid.uuid4().hex}",
        )
        return window

    def load_window(self, strategy_id: str) -> Optional[EvidenceWindow]:
        row = self.db.execute(
            "SELECT payload FROM promotion_evidence_windows WHERE strategy_id = ?",
            [strategy_id],
            fetch="one",
        )
        if row is None:
            return None
        return EvidenceWindow.from_payload(json.loads(row[0]))
