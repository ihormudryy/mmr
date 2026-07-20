"""P3 Task 6 — trader-owned session deadlines and deterministic time exits.

Absolute UTC deadlines come from ``XNYSCalendarPolicy``. Strategy timers are
advisory only: completed-bar ``on_bar`` enforces max-hold / close-by /
artifact close-by. Session ``run_due`` cancels entries, flattens via P1
liquidation, and trips the breaker on a missed flat deadline.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from trader.automation.calendar_policy import SessionSchedule, XNYSCalendarPolicy
from trader.automation.models import TimeExitPolicy
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.research.canonical import sha256_digest
from trader.trading.circuit_breaker import BreakerSignal

SESSION_CONTROLLER_MIGRATION_VERSION = 31
SESSION_CONTROLLER_MIGRATION_NAME = "p3_automation_session_state"

ET = ZoneInfo("America/New_York")

SESSION_STATES = frozenset({
    "CLOSED",
    "OPEN",
    "ENTRY_CUTOFF",
    "CANCELLING",
    "FLATTENING",
    "VERIFYING_FLAT",
    "FLAT",
    "INCIDENT",
})

_TERMINAL = frozenset({"FLAT", "INCIDENT"})


def apply_session_controller_migration(migrator: SchemaMigrator) -> bool:
    """Journal migration 31: durable automation session schedule + deadlines."""
    return migrator.apply(
        SESSION_CONTROLLER_MIGRATION_VERSION,
        SESSION_CONTROLLER_MIGRATION_NAME,
        (
            """CREATE TABLE IF NOT EXISTS automation_session_state (
                account_id VARCHAR NOT NULL,
                session_date DATE NOT NULL,
                calendar_name VARCHAR NOT NULL,
                calendar_version VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                open_utc TIMESTAMPTZ,
                close_utc TIMESTAMPTZ,
                entry_cutoff_utc TIMESTAMPTZ,
                cancel_entries_utc TIMESTAMPTZ,
                flatten_start_utc TIMESTAMPTZ,
                flat_deadline_utc TIMESTAMPTZ,
                entry_cutoff_reached BOOLEAN NOT NULL,
                flatten_command_id VARCHAR,
                flat_generation BIGINT,
                incident VARCHAR,
                cancel_issued BOOLEAN NOT NULL DEFAULT FALSE,
                flatten_issued BOOLEAN NOT NULL DEFAULT FALSE,
                time_exits_json VARCHAR NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (account_id, session_date)
            )""",
        ),
    )


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return _as_utc(value)
    text = str(value).replace("Z", "+00:00")
    return _as_utc(dt.datetime.fromisoformat(text))


def _parse_date(value: Any) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.datetime):
        return value.date()
    return dt.date.fromisoformat(str(value))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackedPosition:
    conid: int
    quantity: Decimal
    side: str  # BUY = long, SELL = short
    entry_command_id: str
    bars_held: int
    time_exit: TimeExitPolicy
    artifact_close_by: Optional[dt.datetime] = None


@dataclass(frozen=True)
class TimeExitAction:
    command_id: str
    conid: int
    quantity: Decimal
    side: str
    reason: str
    entry_command_id: str


@dataclass(frozen=True)
class SessionControllerState:
    account_id: str
    session_date: dt.date
    calendar_name: str
    calendar_version: str
    state: str
    open_utc: Optional[dt.datetime]
    close_utc: Optional[dt.datetime]
    entry_cutoff_utc: Optional[dt.datetime]
    cancel_entries_utc: Optional[dt.datetime]
    flatten_start_utc: Optional[dt.datetime]
    flat_deadline_utc: Optional[dt.datetime]
    entry_cutoff_reached: bool
    flatten_command_id: Optional[str]
    flat_generation: Optional[int]
    incident: Optional[str]
    cancel_issued: bool = False
    flatten_issued: bool = False
    time_exits: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

class BrokerSnapshotPort(Protocol):
    def capture(self, account_id: str) -> Any: ...


class CancelPort(Protocol):
    def cancel_working_entries(
        self, *, root_command_id: str, orders: Sequence[Any],
    ) -> list[str]: ...


class LiquidationPort(Protocol):
    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime) -> Any: ...
    def rescan(self) -> Any: ...


class BreakerPort(Protocol):
    def record(self, signal: BreakerSignal) -> Any: ...


class TimeExitPort(Protocol):
    def request_exit(
        self, *, command_id: str, conid: int, quantity: Decimal, side: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SessionStateStore:
    def __init__(self, db: Any):
        self._db = db

    def load(self, account_id: str, session_date: dt.date) -> Optional[SessionControllerState]:
        row = self._db.execute(
            "SELECT account_id, session_date, calendar_name, calendar_version, state, "
            "open_utc, close_utc, entry_cutoff_utc, cancel_entries_utc, "
            "flatten_start_utc, flat_deadline_utc, entry_cutoff_reached, "
            "flatten_command_id, flat_generation, incident, cancel_issued, "
            "flatten_issued, time_exits_json, updated_at "
            "FROM automation_session_state WHERE account_id=? AND session_date=?",
            [account_id, session_date],
            fetch="one",
        )
        if row is None:
            return None
        exits = tuple(json.loads(row[17] or "{}").get("issued", []))
        return SessionControllerState(
            account_id=row[0],
            session_date=_parse_date(row[1]),
            calendar_name=row[2],
            calendar_version=row[3],
            state=row[4],
            open_utc=_parse_ts(row[5]),
            close_utc=_parse_ts(row[6]),
            entry_cutoff_utc=_parse_ts(row[7]),
            cancel_entries_utc=_parse_ts(row[8]),
            flatten_start_utc=_parse_ts(row[9]),
            flat_deadline_utc=_parse_ts(row[10]),
            entry_cutoff_reached=bool(row[11]),
            flatten_command_id=row[12],
            flat_generation=int(row[13]) if row[13] is not None else None,
            incident=row[14],
            cancel_issued=bool(row[15]),
            flatten_issued=bool(row[16]),
            time_exits=exits,
        )

    def save(self, state: SessionControllerState, now: dt.datetime) -> None:
        payload = json.dumps({"issued": list(state.time_exits)})

        def write(conn):
            conn.execute(
                "DELETE FROM automation_session_state WHERE account_id=? AND session_date=?",
                [state.account_id, state.session_date],
            )
            conn.execute(
                "INSERT INTO automation_session_state VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                ")",
                [
                    state.account_id,
                    state.session_date,
                    state.calendar_name,
                    state.calendar_version,
                    state.state,
                    state.open_utc,
                    state.close_utc,
                    state.entry_cutoff_utc,
                    state.cancel_entries_utc,
                    state.flatten_start_utc,
                    state.flat_deadline_utc,
                    state.entry_cutoff_reached,
                    state.flatten_command_id,
                    state.flat_generation,
                    state.incident,
                    state.cancel_issued,
                    state.flatten_issued,
                    payload,
                    now,
                ],
            )

        self._db.transaction(write)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class SessionCancelAdapter:
    """Cancel working orders with deterministic child command ids."""

    def __init__(self, dispatch: Any | None = None):
        self._dispatch = dispatch

    def cancel_working_entries(
        self, *, root_command_id: str, orders: Sequence[Any],
    ) -> list[str]:
        child_ids: list[str] = []
        for index, order in enumerate(orders):
            child = f"{root_command_id}-{index}"
            child_ids.append(child)
            if self._dispatch is not None:
                cancel = getattr(self._dispatch, "cancel", None)
                if cancel is not None:
                    cancel(order, child)
        return child_ids


class SessionTimeExitAdapter:
    """Reduce-only time exit via liquidation dispatch when available."""

    def __init__(self, dispatch: Any | None = None):
        self._dispatch = dispatch

    def request_exit(
        self, *, command_id: str, conid: int, quantity: Decimal, side: str,
    ) -> None:
        if self._dispatch is None:
            return
        exit_side = "SELL" if side == "BUY" else "BUY"
        position = SimpleNamespace(conid=conid, quantity=float(quantity))
        self._dispatch.reduce(position, exit_side, float(quantity), command_id)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class SessionController:
    """Trader-owned session scheduler: recover / on_bar / run_due."""

    def __init__(
        self,
        *,
        journal: Any,
        db: Any,
        calendar: XNYSCalendarPolicy,
        broker: BrokerSnapshotPort,
        cancel: CancelPort,
        liquidation: LiquidationPort,
        breaker: BreakerPort,
        time_exit: TimeExitPort,
        account_id: str,
        now: Callable[[], dt.datetime],
    ):
        self._journal = journal
        self._calendar = calendar
        self._broker = broker
        self._cancel = cancel
        self._liquidation = liquidation
        self._breaker = breaker
        self._time_exit = time_exit
        self._account_id = account_id
        self._now = now
        self._store = SessionStateStore(db)
        self._state: Optional[SessionControllerState] = None

    # -- deterministic IDs -------------------------------------------------

    @staticmethod
    def cancel_command_id(account_id: str, session_date: dt.date) -> str:
        digest = sha256_digest(
            "session-cancel",
            {"account_id": account_id, "session_date": session_date.isoformat()},
        )
        return f"session-cancel-{digest}"

    @staticmethod
    def flatten_command_id(account_id: str, session_date: dt.date) -> str:
        digest = sha256_digest(
            "session-flatten",
            {"account_id": account_id, "session_date": session_date.isoformat()},
        )
        return f"session-flatten-{digest}"

    @staticmethod
    def time_exit_command_id(
        entry_command_id: str, reason: str, session_date: dt.date,
    ) -> str:
        digest = sha256_digest(
            "session-time-exit",
            {
                "entry_command_id": entry_command_id,
                "reason": reason,
                "session_date": session_date.isoformat(),
            },
        )
        return f"session-time-exit-{digest}"

    # -- public API --------------------------------------------------------

    def recover(self, now: dt.datetime) -> SessionControllerState:
        """Load durable state (or open today's session) and catch up deadlines."""
        now_utc = _as_utc(now)
        schedule = self._calendar.resolve(now_utc)
        if schedule is None:
            session_date = now_utc.astimezone(ET).date()
            existing = self._store.load(self._account_id, session_date)
            if existing is not None:
                self._state = existing
                return existing
            state = self._closed_state(session_date)
            self._persist(state, now_utc)
            return state

        existing = self._store.load(self._account_id, schedule.session_date)
        if existing is not None and existing.state == "INCIDENT":
            # Never self-reset an incident across recover.
            self._state = existing
            return existing

        if existing is None:
            state = self._from_schedule(schedule, state="OPEN")
        else:
            # Rebind schedule fields from current calendar; preserve sticky flags.
            state = SessionControllerState(
                account_id=self._account_id,
                session_date=schedule.session_date,
                calendar_name=schedule.calendar_name,
                calendar_version=schedule.calendar_version,
                state=existing.state if existing.state != "CLOSED" else "OPEN",
                open_utc=schedule.open_utc,
                close_utc=schedule.close_utc,
                entry_cutoff_utc=schedule.entry_cutoff_utc,
                cancel_entries_utc=schedule.cancel_entries_utc,
                flatten_start_utc=schedule.flatten_start_utc,
                flat_deadline_utc=schedule.flat_deadline_utc,
                entry_cutoff_reached=existing.entry_cutoff_reached,
                flatten_command_id=existing.flatten_command_id,
                flat_generation=existing.flat_generation,
                incident=existing.incident,
                cancel_issued=existing.cancel_issued,
                flatten_issued=existing.flatten_issued,
                time_exits=existing.time_exits,
            )
        self._state = state
        self._persist(state, now_utc)
        return self.run_due(now_utc)

    def on_bar(
        self,
        now: dt.datetime,
        *,
        completed_bar_timestamp: Optional[dt.datetime],
        positions: Sequence[TrackedPosition] = (),
    ) -> list[TimeExitAction]:
        """Enforce completed-bar time exits. Incomplete bars are ignored."""
        if completed_bar_timestamp is None:
            return []
        now_utc = _as_utc(now)
        bar_ts = _as_utc(completed_bar_timestamp)
        state = self._require_state(now_utc)
        if state.state in _TERMINAL:
            return []

        actions: list[TimeExitAction] = []
        issued = set(state.time_exits)
        for pos in positions:
            reason = self._exit_reason(pos, bar_ts)
            if reason is None:
                continue
            command_id = self.time_exit_command_id(
                pos.entry_command_id, reason, state.session_date,
            )
            if command_id in issued:
                continue
            exit_side = "SELL" if pos.side == "BUY" else "BUY"
            self._time_exit.request_exit(
                command_id=command_id,
                conid=pos.conid,
                quantity=pos.quantity,
                side=pos.side,
            )
            actions.append(TimeExitAction(
                command_id=command_id,
                conid=pos.conid,
                quantity=pos.quantity,
                side=exit_side,
                reason=reason,
                entry_command_id=pos.entry_command_id,
            ))
            issued.add(command_id)

        if actions:
            state = self._evolve(state, time_exits=tuple(sorted(issued)))
            self._persist(state, now_utc)
        return actions

    def run_due(self, now: dt.datetime) -> SessionControllerState:
        """Advance absolute session deadlines; catch up if scheduling was delayed."""
        now_utc = _as_utc(now)
        state = self._require_state(now_utc)
        if state.state in _TERMINAL:
            return state
        if state.close_utc is None:
            return state

        # 1) Entry cutoff
        if (
            state.entry_cutoff_utc is not None
            and now_utc >= state.entry_cutoff_utc
            and not state.entry_cutoff_reached
        ):
            next_state = "ENTRY_CUTOFF" if state.state == "OPEN" else state.state
            state = self._evolve(
                state,
                entry_cutoff_reached=True,
                state=next_state,
            )
            self._persist(state, now_utc)

        # 2) Cancel working entries
        if (
            state.cancel_entries_utc is not None
            and now_utc >= state.cancel_entries_utc
            and not state.cancel_issued
        ):
            state = self._issue_cancel(state, now_utc)

        # 3) Flatten via P1 liquidation
        if (
            state.flatten_start_utc is not None
            and now_utc >= state.flatten_start_utc
            and not state.flatten_issued
        ):
            state = self._issue_flatten(state, now_utc)

        # 4) Poll liquidation / broker flat confirmation
        if state.flatten_issued and state.state not in _TERMINAL:
            state = self._poll_flat(state, now_utc)

        # 5) Missed flat deadline → incident + breaker (never self-resets)
        if (
            state.state not in _TERMINAL
            and state.flat_deadline_utc is not None
            and now_utc >= state.flat_deadline_utc
        ):
            state = self._missed_flat(state, now_utc)

        return state

    def entries_allowed(self, now: dt.datetime) -> bool:
        now_utc = _as_utc(now)
        state = self._state
        if state is None:
            schedule = self._calendar.resolve(now_utc)
            if schedule is None:
                return False
            return self._calendar.allows_new_entry(now_utc, schedule)
        if state.entry_cutoff_reached:
            return False
        if state.state in _TERMINAL | {
            "CANCELLING", "FLATTENING", "VERIFYING_FLAT", "ENTRY_CUTOFF", "CLOSED",
        }:
            return False
        if state.state != "OPEN":
            return False
        if None in (
            state.open_utc, state.close_utc, state.entry_cutoff_utc,
            state.cancel_entries_utc, state.flatten_start_utc, state.flat_deadline_utc,
        ):
            return False
        schedule = SessionSchedule(
            session_date=state.session_date,
            open_utc=state.open_utc,
            close_utc=state.close_utc,
            entry_cutoff_utc=state.entry_cutoff_utc,
            cancel_entries_utc=state.cancel_entries_utc,
            flatten_start_utc=state.flatten_start_utc,
            flat_deadline_utc=state.flat_deadline_utc,
            opening_stabilization_end_utc=state.open_utc,
            is_early_close=False,
            calendar_name=state.calendar_name,
            calendar_version=state.calendar_version,
        )
        return self._calendar.allows_new_entry(now_utc, schedule)

    # -- internals ---------------------------------------------------------

    def _require_state(self, now_utc: dt.datetime) -> SessionControllerState:
        if self._state is not None:
            return self._state
        return self.recover(now_utc)

    def _closed_state(self, session_date: dt.date) -> SessionControllerState:
        return SessionControllerState(
            account_id=self._account_id,
            session_date=session_date,
            calendar_name=self._calendar._calendar_name,
            calendar_version=self._calendar.calendar_version(),
            state="CLOSED",
            open_utc=None,
            close_utc=None,
            entry_cutoff_utc=None,
            cancel_entries_utc=None,
            flatten_start_utc=None,
            flat_deadline_utc=None,
            entry_cutoff_reached=False,
            flatten_command_id=None,
            flat_generation=None,
            incident=None,
        )

    def _from_schedule(
        self, schedule: SessionSchedule, *, state: str,
    ) -> SessionControllerState:
        return SessionControllerState(
            account_id=self._account_id,
            session_date=schedule.session_date,
            calendar_name=schedule.calendar_name,
            calendar_version=schedule.calendar_version,
            state=state,
            open_utc=schedule.open_utc,
            close_utc=schedule.close_utc,
            entry_cutoff_utc=schedule.entry_cutoff_utc,
            cancel_entries_utc=schedule.cancel_entries_utc,
            flatten_start_utc=schedule.flatten_start_utc,
            flat_deadline_utc=schedule.flat_deadline_utc,
            entry_cutoff_reached=False,
            flatten_command_id=None,
            flat_generation=None,
            incident=None,
        )

    @staticmethod
    def _evolve(current: SessionControllerState, **kwargs) -> SessionControllerState:
        data = {
            "account_id": current.account_id,
            "session_date": current.session_date,
            "calendar_name": current.calendar_name,
            "calendar_version": current.calendar_version,
            "state": current.state,
            "open_utc": current.open_utc,
            "close_utc": current.close_utc,
            "entry_cutoff_utc": current.entry_cutoff_utc,
            "cancel_entries_utc": current.cancel_entries_utc,
            "flatten_start_utc": current.flatten_start_utc,
            "flat_deadline_utc": current.flat_deadline_utc,
            "entry_cutoff_reached": current.entry_cutoff_reached,
            "flatten_command_id": current.flatten_command_id,
            "flat_generation": current.flat_generation,
            "incident": current.incident,
            "cancel_issued": current.cancel_issued,
            "flatten_issued": current.flatten_issued,
            "time_exits": current.time_exits,
        }
        data.update(kwargs)
        return SessionControllerState(**data)

    def _exit_reason(
        self, pos: TrackedPosition, bar_ts: dt.datetime,
    ) -> Optional[str]:
        # Artifact close-by takes precedence when present and due.
        if pos.artifact_close_by is not None and bar_ts >= _as_utc(pos.artifact_close_by):
            return "ARTIFACT_CLOSE_BY"
        if bar_ts >= _as_utc(pos.time_exit.close_by):
            return "CLOSE_BY"
        max_hold = pos.time_exit.max_hold_bars
        if max_hold is not None and pos.bars_held >= max_hold:
            return "MAX_HOLD_BARS"
        return None

    def _issue_cancel(
        self, state: SessionControllerState, now_utc: dt.datetime,
    ) -> SessionControllerState:
        root = self.cancel_command_id(self._account_id, state.session_date)
        try:
            snapshot = self._broker.capture(self._account_id)
            working = tuple(getattr(snapshot, "working_orders", ()) or ())
            if working:
                self._cancel.cancel_working_entries(
                    root_command_id=root, orders=working,
                )
        except Exception:
            # Ambiguous cancel still marks issued; flatten reconciles remainder.
            pass
        state = self._evolve(
            state,
            state="CANCELLING",
            cancel_issued=True,
            entry_cutoff_reached=True,
        )
        self._persist(state, now_utc)
        return state

    def _issue_flatten(
        self, state: SessionControllerState, now_utc: dt.datetime,
    ) -> SessionControllerState:
        cause = self.flatten_command_id(self._account_id, state.session_date)
        deadline = state.flat_deadline_utc or (now_utc + dt.timedelta(minutes=10))
        self._liquidation.start(self._account_id, cause, deadline)
        state = self._evolve(
            state,
            state="FLATTENING",
            flatten_command_id=cause,
            flatten_issued=True,
            cancel_issued=True,
            entry_cutoff_reached=True,
        )
        self._persist(state, now_utc)
        return state

    def _poll_flat(
        self, state: SessionControllerState, now_utc: dt.datetime,
    ) -> SessionControllerState:
        receipt = None
        try:
            receipt = self._liquidation.rescan()
        except Exception:
            receipt = None

        if receipt is not None and getattr(receipt, "state", None) == "FLAT":
            generation = getattr(receipt, "generation_id", None)
            state = self._evolve(
                state,
                state="FLAT",
                flat_generation=int(generation) if generation is not None else None,
            )
            self._persist(state, now_utc)
            return state

        # Only advance FLATTENING → VERIFYING_FLAT when liquidation has an
        # active non-flat receipt. A missing receipt (restart before the
        # liquidation service resumes) must not invent a state transition.
        if (
            receipt is not None
            and state.state == "FLATTENING"
            and getattr(receipt, "state", None) != "FLAT"
        ):
            state = self._evolve(state, state="VERIFYING_FLAT")
            self._persist(state, now_utc)
        return state

    def _missed_flat(
        self, state: SessionControllerState, now_utc: dt.datetime,
    ) -> SessionControllerState:
        detail = (
            f"session flat deadline {_iso(state.flat_deadline_utc)} "
            f"elapsed without broker-confirmed flat"
        )
        self._breaker.record(BreakerSignal(
            kind="MISSED_FLAT_DEADLINE",
            occurred_at=now_utc,
            detail=detail,
            key=state.flatten_command_id or self.flatten_command_id(
                self._account_id, state.session_date,
            ),
        ))
        state = self._evolve(state, state="INCIDENT", incident=detail)
        self._persist(state, now_utc)
        return state

    def _persist(self, state: SessionControllerState, now_utc: dt.datetime) -> None:
        self._state = state
        self._store.save(state, now_utc)
        if self._journal is None:
            return
        try:
            payload = {
                "state": state.state,
                "session_date": state.session_date.isoformat(),
                "entry_cutoff_reached": state.entry_cutoff_reached,
                "flatten_command_id": state.flatten_command_id,
                "flat_generation": state.flat_generation,
                "incident": state.incident,
                "calendar_version": state.calendar_version,
                "entry_cutoff_utc": _iso(state.entry_cutoff_utc),
                "cancel_entries_utc": _iso(state.cancel_entries_utc),
                "flatten_start_utc": _iso(state.flatten_start_utc),
                "flat_deadline_utc": _iso(state.flat_deadline_utc),
            }

            def write(_conn, _revision):
                return None

            self._journal.mutate(
                self._journal.connect(),
                DomainMutation(
                    event_type="automation_session.updated",
                    entity_type="automation_session",
                    entity_id=f"{state.account_id}:{state.session_date.isoformat()}",
                    operation="upsert",
                    account_id=state.account_id,
                    source="trader_service",
                    source_timestamp=now_utc,
                    correlation_id=state.flatten_command_id
                    or self.cancel_command_id(state.account_id, state.session_date),
                    payload=payload,
                ),
                write,
                event_id=(
                    f"automation_session:{state.account_id}:"
                    f"{state.session_date.isoformat()}:{state.state}:"
                    f"{int(now_utc.timestamp())}"
                ),
            )
        except Exception:
            # Durable row is authoritative; domain event is observability-only.
            pass
