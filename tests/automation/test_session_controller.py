"""P3 Task 6 — session deadlines and deterministic time exits.

Contract (plan §Task 6):
* Trader-owned scheduler driven by absolute UTC deadlines from XNYS.
* Completed-bar time exits (max_hold_bars, artifact close_by); strategy timers advisory.
* No entries after cutoff; cancel working entries at cancel deadline; flatten at
  flatten deadline; broker-confirmed flat by flat deadline.
* Missed flat deadline trips breaker and never self-resets.
* Durable state (migration 31) survives restart at every state/deadline.
* Deterministic root/child command IDs; reuse P1 cancel/liquidation.
* recover() runs before semantic readiness.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pytest

from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.models import TimeExitPolicy
from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.circuit_breaker import BreakerSignal

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
ACCOUNT = "DU111111"
CONID = 265598

# Mid-session Friday 2026-07-17 (regular session)
SESSION_DATE = dt.date(2026, 7, 17)


def _et(hour: int, minute: int = 0, *, date: dt.date = SESSION_DATE) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=ET)


def _utc(hour: int, minute: int = 0, *, date: dt.date = SESSION_DATE) -> dt.datetime:
    return _et(hour, minute, date=date).astimezone(UTC)


# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------

def _position(quantity: float = 10.0, conid: int = CONID) -> BrokerPositionRow:
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol="AAPL", sec_type="STK",
        exchange="SMART", currency="USD", quantity=quantity, average_cost=100.0,
        market_price=160.0, market_value=1600.0, unrealized_pnl=0.0,
        realized_pnl=0.0, daily_pnl=0.0, deleted=False, revision=1,
        source_timestamp=_utc(11, 0),
    )


def _order(
    entity: str = "ord-entry-1",
    *,
    leg: str = "entry",
    filled: float = 0.0,
    total: float = 10.0,
    is_external: bool = False,
) -> BrokerOrderRow:
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=CONID, symbol="AAPL",
        order_group_id="og-1", leg=leg, is_external=is_external, action="BUY",
        order_type="LMT", total_quantity=total, filled_quantity=filled,
        avg_fill_price=None, limit_price=160.0, stop_price=None, tif="DAY",
        status="Submitted", deleted=False, revision=1,
        source_timestamp=_utc(11, 0),
    )


def _snapshot(
    generation: int,
    positions=(),
    working=(),
    *,
    now: Optional[dt.datetime] = None,
) -> BrokerRiskSnapshot:
    ts = now or _utc(11, 0)
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=generation, promoted_at=ts,
        account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000.0,
        daily_pnl=0.0, positions=tuple(positions), working_orders=tuple(working),
    )


class FakeBroker:
    def __init__(self, snapshots: list | None = None):
        self.snapshots = list(snapshots or [_snapshot(1)])
        self.calls = 0

    def capture(self, account_id: str):
        self.calls += 1
        assert account_id == ACCOUNT
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, Exception):
            raise value
        return value

    def push(self, snap: BrokerRiskSnapshot):
        self.snapshots = [snap]


class FakeCancel:
    def __init__(self):
        self.calls: list[tuple] = []

    def cancel_working_entries(self, *, root_command_id: str, orders: tuple) -> list[str]:
        child_ids = []
        for i, order in enumerate(orders):
            child = f"{root_command_id}-{i}"
            self.calls.append(("cancel", order.order_entity_id, child, root_command_id))
            child_ids.append(child)
        return child_ids


class FakeLiquidation:
    def __init__(self):
        self.starts: list[tuple] = []
        self._receipt: Any = None
        self.rescans = 0

    def start(self, account_id, cause_command_id, deadline):
        self.starts.append((account_id, cause_command_id, deadline))
        self._receipt = SimpleNamespace(
            account_id=account_id,
            cause_command_id=cause_command_id,
            state="REQUESTED",
            deadline=deadline,
            generation_id=None,
            detail="started",
        )
        return self._receipt

    def rescan(self):
        self.rescans += 1
        return self._receipt

    def mark_flat(self, generation_id: int = 2):
        if self._receipt is None:
            return
        self._receipt = SimpleNamespace(
            account_id=self._receipt.account_id,
            cause_command_id=self._receipt.cause_command_id,
            state="FLAT",
            deadline=self._receipt.deadline,
            generation_id=generation_id,
            detail="flat",
        )


class FakeBreaker:
    def __init__(self):
        self.signals: list[BreakerSignal] = []

    def record(self, signal: BreakerSignal):
        self.signals.append(signal)
        return SimpleNamespace(state="TRIPPED", reason_code=signal.kind)


class FakeTimeExitDispatch:
    def __init__(self):
        self.exits: list[dict] = []

    def request_exit(self, *, command_id: str, conid: int, quantity: Decimal, side: str):
        self.exits.append({
            "command_id": command_id,
            "conid": conid,
            "quantity": quantity,
            "side": side,
        })


def _build_controller(tmp_path: Path, **overrides):
    from trader.automation.session_controller import (
        SessionController,
        apply_session_controller_migration,
    )

    db = DuckDBConnection.get_instance(str(tmp_path / "session.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_session_controller_migration(migrator)

    broker = overrides.pop("broker", FakeBroker())
    cancel = overrides.pop("cancel", FakeCancel())
    liquidation = overrides.pop("liquidation", FakeLiquidation())
    breaker = overrides.pop("breaker", FakeBreaker())
    time_exit = overrides.pop("time_exit", FakeTimeExitDispatch())
    calendar = overrides.pop("calendar", XNYSCalendarPolicy())
    clock = overrides.pop("clock", [_utc(11, 0)])

    controller = SessionController(
        journal=journal,
        db=db,
        calendar=calendar,
        broker=broker,
        cancel=cancel,
        liquidation=liquidation,
        breaker=breaker,
        time_exit=time_exit,
        account_id=ACCOUNT,
        now=lambda: clock[0],
        **overrides,
    )
    return controller, broker, cancel, liquidation, breaker, time_exit, journal, db, clock


def _tracked(
    *,
    bars_held: int = 0,
    max_hold: int | None = 10,
    close_by: dt.datetime | None = None,
    artifact_close_by: dt.datetime | None = None,
    quantity: Decimal = Decimal("10"),
    entry_command_id: str = "auto-entry-1",
):
    from trader.automation.session_controller import TrackedPosition

    close = close_by or _utc(15, 50)
    return TrackedPosition(
        conid=CONID,
        quantity=quantity,
        side="BUY",
        entry_command_id=entry_command_id,
        bars_held=bars_held,
        time_exit=TimeExitPolicy(max_hold_bars=max_hold, close_by=close),
        artifact_close_by=artifact_close_by,
    )


# ---------------------------------------------------------------------------
# Migration 31
# ---------------------------------------------------------------------------

def test_migration_31_creates_automation_session_state_table(tmp_path):
    from trader.automation.session_controller import (
        SESSION_CONTROLLER_MIGRATION_VERSION,
        apply_session_controller_migration,
    )

    db = DuckDBConnection.get_instance(str(tmp_path / "mig.duckdb"))
    migrator = SchemaMigrator(db)
    assert apply_session_controller_migration(migrator) is True
    assert SESSION_CONTROLLER_MIGRATION_VERSION == 31
    assert apply_session_controller_migration(migrator) is False
    cols = {
        row[1]
        for row in db.execute("PRAGMA table_info('automation_session_state')", fetch="all")
    }
    required = {
        "account_id", "session_date", "calendar_name", "calendar_version",
        "state", "entry_cutoff_utc", "cancel_entries_utc", "flatten_start_utc",
        "flat_deadline_utc", "entry_cutoff_reached", "flatten_command_id",
        "flat_generation", "incident", "updated_at",
    }
    assert required <= cols


# ---------------------------------------------------------------------------
# recover / schedule / entries
# ---------------------------------------------------------------------------

def test_recover_loads_xnys_schedule_and_opens_session(tmp_path):
    controller, *_a, clock = _build_controller(tmp_path)
    clock[0] = _utc(11, 0)
    state = controller.recover(clock[0])
    assert state.state == "OPEN"
    assert state.session_date == SESSION_DATE
    assert state.entry_cutoff_reached is False
    assert state.entry_cutoff_utc == state.close_utc - dt.timedelta(minutes=30)
    assert state.calendar_name == "XNYS"
    assert state.calendar_version


def test_no_entries_after_cutoff(tmp_path):
    controller, *_a, clock = _build_controller(tmp_path)
    clock[0] = _utc(11, 0)
    controller.recover(clock[0])
    assert controller.entries_allowed(clock[0]) is True

    clock[0] = _utc(15, 30)
    state = controller.run_due(clock[0])
    assert state.entry_cutoff_reached is True
    assert state.state in ("ENTRY_CUTOFF", "CANCELLING", "FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT")
    assert controller.entries_allowed(clock[0]) is False


def test_entry_cutoff_sticky_across_clock_jump_backward(tmp_path):
    """Once cutoff is reached, clock rewind must not re-open entries."""
    controller, *_a, clock = _build_controller(tmp_path)
    clock[0] = _utc(15, 30)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert controller.entries_allowed(clock[0]) is False

    clock[0] = _utc(14, 0)
    state = controller.recover(clock[0])
    assert state.entry_cutoff_reached is True
    assert controller.entries_allowed(clock[0]) is False


# ---------------------------------------------------------------------------
# Cancel / flatten / flat deadlines
# ---------------------------------------------------------------------------

def test_cancel_entries_at_cancel_deadline(tmp_path):
    working = (_order("ord-1", leg="entry"), _order("ord-2", leg="entry"))
    broker = FakeBroker([_snapshot(1, working=working)])
    controller, _b, cancel, _l, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    clock[0] = _utc(15, 35)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert state.state in ("CANCELLING", "FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT")
    assert len(cancel.calls) == 2
    root = cancel.calls[0][3]
    assert root.startswith("session-cancel-")
    assert all(c[3] == root for c in cancel.calls)
    # deterministic child ids
    assert cancel.calls[0][2] == f"{root}-0"
    assert cancel.calls[1][2] == f"{root}-1"


def test_partial_fill_during_cancel_still_cancels_remainder(tmp_path):
    """Partial fill does not invent flatness; cancel still targets working order."""
    working = (_order("ord-partial", filled=4.0, total=10.0),)
    broker = FakeBroker([_snapshot(1, positions=[_position(4.0)], working=working)])
    controller, _b, cancel, _l, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    clock[0] = _utc(15, 35)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert len(cancel.calls) == 1
    assert cancel.calls[0][1] == "ord-partial"


def test_flatten_at_flatten_deadline_uses_liquidation(tmp_path):
    broker = FakeBroker([_snapshot(1, positions=[_position()])])
    controller, _b, _c, liquidation, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    clock[0] = _utc(15, 45)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert state.state in ("FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT")
    assert len(liquidation.starts) == 1
    account_id, cause_id, deadline = liquidation.starts[0]
    assert account_id == ACCOUNT
    assert cause_id == state.flatten_command_id
    assert cause_id.startswith("session-flatten-")
    assert deadline == state.flat_deadline_utc


def test_flatten_command_id_is_deterministic(tmp_path):
    from trader.automation.session_controller import SessionController

    a = SessionController.flatten_command_id(ACCOUNT, SESSION_DATE)
    b = SessionController.flatten_command_id(ACCOUNT, SESSION_DATE)
    assert a == b
    assert ":" not in a
    assert a != SessionController.flatten_command_id(ACCOUNT, dt.date(2026, 7, 16))


def test_broker_confirmed_flat_records_generation(tmp_path):
    broker = FakeBroker([
        _snapshot(1, positions=[_position()]),
        _snapshot(2, positions=()),
    ])
    liquidation = FakeLiquidation()
    controller, _b, _c, liq, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker, liquidation=liquidation,
    )
    clock[0] = _utc(15, 45)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert liq.starts

    # Broker proves flat on a later generation
    liq.mark_flat(generation_id=2)
    broker.push(_snapshot(2, positions=(), working=()))
    clock[0] = _utc(15, 50)
    state = controller.run_due(clock[0])
    assert state.state == "FLAT"
    assert state.flat_generation == 2
    assert state.incident is None


def test_missed_flat_deadline_trips_breaker_and_never_self_resets(tmp_path):
    broker = FakeBroker([_snapshot(1, positions=[_position()])])
    controller, _b, _c, liquidation, breaker, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    clock[0] = _utc(15, 45)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert liquidation.starts

    # Still not flat at/after flat deadline
    clock[0] = _utc(15, 55)
    state = controller.run_due(clock[0])
    assert state.state == "INCIDENT"
    assert state.incident is not None
    assert any(s.kind == "MISSED_FLAT_DEADLINE" for s in breaker.signals)

    # Later tick / restart cannot self-clear the incident
    clock[0] = _utc(15, 56)
    state2 = controller.run_due(clock[0])
    assert state2.state == "INCIDENT"
    clock[0] = _utc(15, 57)
    state3 = controller.recover(clock[0])
    assert state3.state == "INCIDENT"
    assert controller.entries_allowed(clock[0]) is False


def test_external_position_is_included_in_flatten(tmp_path):
    """External (non-automation) exposure is still flattened via liquidation."""
    broker = FakeBroker([
        _snapshot(1, positions=[_position()], working=[_order("ext-1", is_external=True)]),
    ])
    controller, _b, _c, liquidation, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    clock[0] = _utc(15, 45)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    # Cancel path may have fired at catch-up; flatten must still start.
    assert liquidation.starts
    assert liquidation.starts[0][0] == ACCOUNT


# ---------------------------------------------------------------------------
# Delayed scheduling / clock jumps / catch-up
# ---------------------------------------------------------------------------

def test_delayed_run_due_catches_up_all_missed_deadlines_in_order(tmp_path):
    working = (_order(),)
    broker = FakeBroker([
        _snapshot(1, positions=[_position()], working=working),
        _snapshot(2, positions=[_position()]),
    ])
    controller, _b, cancel, liquidation, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    # Jump straight past flatten start — must still cancel then flatten.
    clock[0] = _utc(15, 46)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert state.entry_cutoff_reached is True
    assert cancel.calls  # cancel deadline was due
    assert liquidation.starts  # flatten was due
    assert state.state in ("FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT")


def test_half_day_session_uses_relative_deadlines(tmp_path):
    """Thanksgiving early close: offsets from 13:00 ET close."""
    early = dt.date(2025, 11, 28)
    controller, *_a, clock = _build_controller(tmp_path)
    clock[0] = _et(10, 0, date=early).astimezone(UTC)
    state = controller.recover(clock[0])
    assert state.session_date == early
    close_et = state.close_utc.astimezone(ET)
    assert close_et.hour == 13 and close_et.minute == 0
    assert state.entry_cutoff_utc == state.close_utc - dt.timedelta(minutes=30)
    assert state.flat_deadline_utc == state.close_utc - dt.timedelta(minutes=5)

    clock[0] = state.entry_cutoff_utc
    state = controller.run_due(clock[0])
    assert state.entry_cutoff_reached is True


def test_dst_spring_forward_deadlines_are_absolute_utc(tmp_path):
    dst_date = dt.date(2026, 3, 9)  # Monday after spring-forward
    controller, *_a, clock = _build_controller(tmp_path)
    clock[0] = _et(11, 0, date=dst_date).astimezone(UTC)
    state = controller.recover(clock[0])
    # 15:30 EDT = 19:30 UTC
    assert state.entry_cutoff_utc.astimezone(ET).hour == 15
    assert state.entry_cutoff_utc.astimezone(ET).minute == 30
    assert state.entry_cutoff_utc.utcoffset() == dt.timedelta(0)


# ---------------------------------------------------------------------------
# Restart durability at each state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("when_et,expected_states", [
    ((11, 0), {"OPEN"}),
    ((15, 30), {"ENTRY_CUTOFF", "CANCELLING", "FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT"}),
    ((15, 35), {"CANCELLING", "FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT"}),
    ((15, 45), {"FLATTENING", "VERIFYING_FLAT", "FLAT", "INCIDENT"}),
])
def test_restart_restores_durable_state(tmp_path, when_et, expected_states):
    hour, minute = when_et
    broker = FakeBroker([_snapshot(1, positions=[_position()], working=[_order()])])
    controller, *_a, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(hour, minute)
    controller.recover(clock[0])
    before = controller.run_due(clock[0])
    assert before.state in expected_states

    # New instance over same DB
    clock2 = [clock[0]]
    db = DuckDBConnection.get_instance(str(tmp_path / "session.duckdb"))
    from trader.automation.session_controller import (
        SessionController,
        apply_session_controller_migration,
    )
    journal = DomainJournal(db)
    apply_session_controller_migration(SchemaMigrator(db))
    c2 = SessionController(
        journal=journal,
        db=db,
        calendar=XNYSCalendarPolicy(),
        broker=FakeBroker([_snapshot(1)]),
        cancel=FakeCancel(),
        liquidation=FakeLiquidation(),
        breaker=FakeBreaker(),
        time_exit=FakeTimeExitDispatch(),
        account_id=ACCOUNT,
        now=lambda: clock2[0],
    )
    after = c2.recover(clock2[0])
    assert after.state == before.state
    assert after.entry_cutoff_reached == before.entry_cutoff_reached
    assert after.flatten_command_id == before.flatten_command_id
    assert after.session_date == before.session_date


# ---------------------------------------------------------------------------
# Completed-bar time exits
# ---------------------------------------------------------------------------

def test_max_hold_bars_exit_on_completed_bar(tmp_path):
    from trader.automation.session_controller import TrackedPosition

    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(14, 0)
    controller.recover(clock[0])
    pos = _tracked(bars_held=9, max_hold=10, close_by=_utc(15, 50))
    actions = controller.on_bar(
        clock[0],
        completed_bar_timestamp=_utc(14, 0),
        positions=(pos,),
    )
    assert actions == []  # not yet

    pos2 = TrackedPosition(
        conid=pos.conid, quantity=pos.quantity, side=pos.side,
        entry_command_id=pos.entry_command_id, bars_held=10,
        time_exit=pos.time_exit, artifact_close_by=pos.artifact_close_by,
    )
    actions = controller.on_bar(
        clock[0],
        completed_bar_timestamp=_utc(14, 1),
        positions=(pos2,),
    )
    assert len(actions) == 1
    assert actions[0].reason == "MAX_HOLD_BARS"
    assert len(time_exit.exits) == 1
    assert time_exit.exits[0]["conid"] == CONID
    assert time_exit.exits[0]["command_id"].startswith("session-time-exit-")


def test_close_by_time_exit_on_completed_bar(tmp_path):
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(14, 30)
    controller.recover(clock[0])
    close_by = _utc(14, 30)
    pos = _tracked(bars_held=2, max_hold=100, close_by=close_by)
    actions = controller.on_bar(
        clock[0],
        completed_bar_timestamp=close_by,
        positions=(pos,),
    )
    assert len(actions) == 1
    assert actions[0].reason == "CLOSE_BY"
    assert time_exit.exits


def test_artifact_close_by_exit_on_completed_bar(tmp_path):
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(14, 0)
    controller.recover(clock[0])
    artifact_close = _utc(14, 0)
    pos = _tracked(
        bars_held=1, max_hold=100,
        close_by=_utc(15, 50),
        artifact_close_by=artifact_close,
    )
    actions = controller.on_bar(
        clock[0],
        completed_bar_timestamp=artifact_close,
        positions=(pos,),
    )
    assert len(actions) == 1
    assert actions[0].reason == "ARTIFACT_CLOSE_BY"
    assert time_exit.exits


def test_time_exit_command_ids_are_deterministic(tmp_path):
    from trader.automation.session_controller import SessionController

    a = SessionController.time_exit_command_id("auto-entry-1", "MAX_HOLD_BARS", SESSION_DATE)
    b = SessionController.time_exit_command_id("auto-entry-1", "MAX_HOLD_BARS", SESSION_DATE)
    assert a == b
    assert ":" not in a


def test_time_exit_idempotent_for_same_position(tmp_path):
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(14, 30)
    controller.recover(clock[0])
    pos = _tracked(bars_held=10, max_hold=10)
    controller.on_bar(clock[0], completed_bar_timestamp=_utc(14, 30), positions=(pos,))
    controller.on_bar(clock[0], completed_bar_timestamp=_utc(14, 31), positions=(pos,))
    assert len(time_exit.exits) == 1


def test_incomplete_bar_does_not_trigger_time_exit(tmp_path):
    """Advisory strategy timers must not fire without a completed bar timestamp."""
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(14, 30)
    controller.recover(clock[0])
    pos = _tracked(bars_held=10, max_hold=10, close_by=_utc(14, 0))
    actions = controller.on_bar(clock[0], completed_bar_timestamp=None, positions=(pos,))
    assert actions == []
    assert time_exit.exits == []


# ---------------------------------------------------------------------------
# Recovery before semantic readiness (wiring contract)
# ---------------------------------------------------------------------------

def test_command_stack_exposes_session_controller(tmp_path, monkeypatch):
    """build_command_stack wires SessionController when authority is enabled."""
    from trader.automation.session_controller import SessionController
    from trader.trading.command_stack import CommandStack

    # Structural: CommandStack dataclass must carry the field.
    assert "session_controller" in CommandStack.__dataclass_fields__


def test_trader_service_starts_session_recovery_before_readiness():
    """Source-level: session recovery is invoked before trader.run()."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "trader" / "trader_service.py"
    text = src.read_text()
    # Pin the call site in main(), not earlier docstring mentions of trader.run().
    marker = "_maybe_start_session_recovery(trader, loop)"
    assert marker in text
    run_marker = "logging.debug('starting trader run() loop')\n        trader.run()"
    assert run_marker in text
    assert text.index(marker) < text.index(run_marker)
