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
    group: str = "og-1",
    action: str = "BUY",
) -> BrokerOrderRow:
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=CONID, symbol="AAPL",
        order_group_id=group, leg=leg, is_external=is_external, action=action,
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
    """Records starts; ``joined_root`` makes the next account start join that root."""
    def __init__(self):
        self.starts: list[tuple] = []
        self.kwargs: list[dict] = []
        self.receipts: dict[str, Any] = {}
        self.rescans = 0
        self.joined_root: Optional[str] = None
        self.other_flat: Any = None

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        self.starts.append((account_id, cause_command_id, deadline))
        self.kwargs.append(kwargs)
        root = self.joined_root or cause_command_id
        self.receipts.setdefault(root, SimpleNamespace(
            account_id=account_id, cause_command_id=root, state="REQUESTED",
            deadline=deadline, generation_id=None, detail="started",
        ))
        return self.receipts[root]

    def rescan(self):
        self.rescans += 1
        return self.other_flat or next(iter(self.receipts.values()), None)

    def receipt_for(self, root_id):
        return self.receipts.get(root_id)

    def mark_flat(self, generation_id: int = 2, root_id: Optional[str] = None):
        if not self.receipts:
            return
        root = root_id or next(iter(self.receipts))
        current = self.receipts[root]
        self.receipts[root] = SimpleNamespace(
            account_id=current.account_id, cause_command_id=root, state="FLAT",
            deadline=current.deadline, generation_id=generation_id, detail="flat",
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


def test_session_cancel_entries_keeps_protective_children(tmp_path):
    """R16 / #27: stops, targets, a close's reduce and external orders are not cancelled."""
    working = (
        _order("og-1:entry", leg="entry"),
        _order("og-2:stop", leg="stop", group="og-2", action="SELL"),
        _order("og-2:take_profit", leg="take_profit", group="og-2", action="SELL"),
        _order("ext-1", leg="entry", is_external=True, group=None),
        _order("flat-1-liquidation-reduce-265598:entry", leg="entry",
               group="flat-1-liquidation-reduce-265598", action="SELL"),
    )
    broker = FakeBroker([_snapshot(1, positions=[_position()], working=working)])
    controller, _b, cancel, _l, breaker, _t, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 35)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert [c[1] for c in cancel.calls] == ["og-1:entry"]
    assert breaker.signals == []


def test_protection_bigger_than_the_position_after_the_cancel_closes_the_conid(tmp_path):
    """D16: the entry filled 4 of 10 and its rest was cancelled; a stop for 10 would reverse the position."""
    working = (_order("og-1:stop", leg="stop", total=10.0, action="SELL"),)
    broker = FakeBroker([_snapshot(1, positions=[_position(4.0)], working=working)])
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 36)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    from trader.automation.session_controller import SessionController
    root = SessionController.cancel_command_id(ACCOUNT, SESSION_DATE)
    expected = {"command_id": f"{root}-protect-{CONID}", "conid": CONID, "quantity": Decimal("4.0"), "side": "BUY"}
    assert time_exit.exits and all(e == expected for e in time_exit.exits)   # one root id: the close joins itself


def test_protection_that_matches_the_position_is_left_to_the_flatten(tmp_path):
    working = (_order("og-1:stop", leg="stop", total=4.0, action="SELL"),)
    broker = FakeBroker([_snapshot(1, positions=[_position(4.0)], working=working)])
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 36)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert time_exit.exits == []


def test_an_entry_that_cannot_be_cancelled_does_not_stop_the_others():
    from trader.automation.session_controller import SessionCancelAdapter
    from trader.trading.liquidation_service import DispatchRefused

    sent = []

    def cancel(order, child):
        if order.order_entity_id == "gone":
            raise DispatchRefused("CANCEL_UNRESOLVED", "no live order")
        sent.append(order.order_entity_id)
    SessionCancelAdapter(SimpleNamespace(cancel=cancel)).cancel_working_entries(
        root_command_id="session-cancel-x", orders=(_order("gone"), _order("og-2:entry")))
    assert sent == ["og-2:entry"]


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


class _RootGoesFailedSafe(FakeLiquidation):
    """rescan() returns None, as the real service does once its only root is FAILED_SAFE."""
    def rescan(self):
        self.rescans += 1
        return None


def test_missed_flat_deadline_records_incident_when_rescan_finds_no_advanceable_root(tmp_path):
    broker = FakeBroker([_snapshot(1, positions=[_position()])])
    controller, _b, _c, liquidation, breaker, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker, liquidation=_RootGoesFailedSafe(),
    )
    clock[0] = _utc(15, 45)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert liquidation.starts
    assert state.state in ("FLATTENING", "VERIFYING_FLAT")   # Task 11 polls the root it got back

    clock[0] = _utc(15, 55)
    state = controller.run_due(clock[0])
    assert state.state == "INCIDENT"
    assert any(s.kind == "MISSED_FLAT_DEADLINE" for s in breaker.signals)


class _LockAlwaysBusy(FakeLiquidation):
    """start() raises LiquidationBusy, as the real service does when the lock stays held."""
    def start(self, account_id, cause_command_id, deadline):
        from trader.trading.liquidation_service import LiquidationBusy

        self.starts.append((account_id, cause_command_id, deadline))
        raise LiquidationBusy("liquidation lock busy")


def test_busy_liquidation_lock_still_reaches_incident_after_flat_deadline(tmp_path):
    broker = FakeBroker([_snapshot(1, positions=[_position()])])
    controller, _b, _c, liquidation, breaker, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker, liquidation=_LockAlwaysBusy(),
    )
    clock[0] = _utc(15, 45)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert liquidation.starts
    assert state.state == "FLATTENING"
    assert state.flatten_issued is True

    clock[0] = _utc(15, 55)
    state = controller.run_due(clock[0])
    assert state.state == "INCIDENT"
    assert any(s.kind == "MISSED_FLAT_DEADLINE" for s in breaker.signals)


def test_recover_past_flat_deadline_with_busy_lock_records_incident(tmp_path):
    broker = FakeBroker([_snapshot(1, positions=[_position()])])
    controller, _b, _c, liquidation, breaker, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker, liquidation=_LockAlwaysBusy(),
    )
    clock[0] = _utc(15, 55)
    state = controller.recover(clock[0])
    assert liquidation.starts
    assert state.state == "INCIDENT"
    assert any(s.kind == "MISSED_FLAT_DEADLINE" for s in breaker.signals)


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
    marker = "_maybe_start_session_recovery(trader, loop, liquidation_worker, stopping)"
    assert marker in text
    run_marker = "logging.debug('starting trader run() loop')\n        trader.run()"
    assert run_marker in text
    assert text.index(marker) < text.index(run_marker)
# ---------------------------------------------------------------------------
# SP1 plan 1: time exits close through the scoped liquidation (Tasks 1, 11)
# ---------------------------------------------------------------------------

class _SimBroker:
    """A broker with one long position and its working protective stop.

    It is the snapshot port and the dispatch port at once; every capture is a
    newer, complete broker generation.
    """
    def __init__(self, quantity: float = 10.0):
        self.generation = 0
        self.quantity = quantity
        self.stop_working = True
        self.rows: dict[str, list] = {}
        self.calls: list[tuple] = []

    def _stop_row(self, status: str = "Submitted") -> BrokerOrderRow:
        return BrokerOrderRow(
            order_entity_id="og-1:stop", account_id=ACCOUNT, conid=CONID, symbol="AAPL",
            order_group_id="og-1", leg="stop", is_external=False, action="SELL", order_type="STP",
            total_quantity=10.0, filled_quantity=0.0, avg_fill_price=None, limit_price=None,
            stop_price=150.0, tif="DAY", status=status, deleted=False, revision=1,
            source_timestamp=_utc(11, 0),
        )

    def capture(self, account_id):
        self.generation += 1
        positions = [_position(self.quantity)] if self.quantity else []
        working = [self._stop_row()] if self.stop_working else []
        return _snapshot(self.generation, positions, working)

    def cancel(self, order, child_id):
        self.calls.append(("cancel", order.order_entity_id))
        self.stop_working = False

    def reduce(self, position, side, quantity, child_id):
        self.calls.append(("reduce", side, float(quantity)))
        self.quantity -= float(quantity) if side == "SELL" else -float(quantity)
        self.rows[child_id] = [SimpleNamespace(status="Filled", filled_quantity=float(quantity),
                                               total_quantity=float(quantity))]

    def find_orders(self, account_id, child_id):
        return self.rows.get(child_id, [])

    def get_order(self, order_entity_id):
        return None if self.stop_working else self._stop_row("Cancelled")

    def executed_quantities(self, account_id, order_entity_ids):
        return {}

    def unbound_execution_since(self, account_id, conid, generation_id):
        return False

    def enumeration_complete(self):
        return True

    def newest_generation(self):
        return self.generation


class _SimProtection:
    """Saga stand-in that writes its hand-over into the broker's call log, so the order is visible."""
    def __init__(self, calls):
        self.calls = calls

    def handover(self, *, account_id, conid, close_root_id, cancels, generation, now):
        from trader.trading.liquidation_service import HandoverInfo
        self.calls.append(("handover", tuple(c.order_entity_id for c in cancels)))
        return HandoverInfo(150.0, None)

    def handover_account(self, **_kwargs):
        raise AssertionError("a time exit is a conid-scoped close")

    def expect_reprotect(self, **_kwargs):
        raise AssertionError("a time exit never re-protects")

    def release_after_partial(self, **_kwargs):
        raise AssertionError("a time exit never re-protects")

    def close_after_full(self, *, close_root_id, now):
        self.calls.append(("close_after_full", close_root_id))


def _real_liquidation(tmp_path, broker, protection=None):
    from trader.trading.exit_owner import ExitOwnerRegistry
    from trader.trading.liquidation_service import (
        LiquidationRunStore, LiquidationService, apply_liquidation_migration,
    )
    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
    apply_liquidation_migration(SchemaMigrator(db))
    return LiquidationService(broker, broker, store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db),
                              now=lambda: _utc(15, 0), protection=protection)


def test_time_exit_leaves_no_live_stop_after_the_position_is_closed(tmp_path):
    """Spec 5.1: a time exit hands protection over, cancels the stop, then closes from broker truth."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    broker = _SimBroker()
    service = _real_liquidation(tmp_path, broker, protection=_SimProtection(broker.calls))
    adapter = SessionTimeExitAdapter(service, account_id=ACCOUNT, now=lambda: _utc(15, 0))
    adapter.request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    for _ in range(4):
        service.rescan()
    assert broker.calls == [("handover", ("og-1:stop",)), ("cancel", "og-1:stop"), ("reduce", "SELL", 10.0),
                            ("close_after_full", "exit-1")]
    assert broker.quantity == 0.0
    assert broker.stop_working is False, "a live stop on a closed position can open a short"
    assert service.receipt_for("exit-1").state == "CLOSED"


def test_time_exit_adapter_starts_a_full_conid_close_without_a_quantity():
    """#27: a quantity would make a join refusable; the adapter never passes one."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    liquidation = FakeLiquidation()
    SessionTimeExitAdapter(liquidation, account_id=ACCOUNT, now=lambda: _utc(15, 0), deadline_seconds=120.0) \
        .request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    assert liquidation.starts == [(ACCOUNT, "exit-1", _utc(15, 2))]
    assert liquidation.kwargs == [{"scope": "conid", "conid": CONID}]


def test_time_exit_adapter_tolerates_exit_in_progress():
    from trader.automation.session_controller import SessionTimeExitAdapter
    from trader.trading.exit_owner import ExitInProgress

    class _Refusing:
        def start(self, *a, **k):
            raise ExitInProgress("other-root")

    SessionTimeExitAdapter(_Refusing(), account_id=ACCOUNT, now=lambda: _utc(15, 0)).request_exit(
        command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")


def test_poll_flat_ignores_another_roots_flat_receipt(tmp_path):
    controller, broker, _c, liquidation, _br, _t, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(15, 46)
    controller.recover(clock[0])
    liquidation.other_flat = SimpleNamespace(cause_command_id="someone-else", state="FLAT", generation_id=9)
    state = controller.run_due(_utc(15, 47))
    assert state.state in ("FLATTENING", "VERIFYING_FLAT")
    liquidation.mark_flat(generation_id=3, root_id=state.flatten_command_id)
    state = controller.run_due(_utc(15, 48))
    assert (state.state, state.flat_generation) == ("FLAT", 3)


def test_session_flatten_joining_an_existing_account_flatten_polls_the_returned_root(tmp_path):
    """R10 / #27: the session persists the root it got back, also across a restart."""
    liquidation = FakeLiquidation()
    liquidation.joined_root = "kill-1"
    controller, _b, _c, _l, _br, _t, _j, db, clock = _build_controller(tmp_path, liquidation=liquidation)
    clock[0] = _utc(15, 46)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert state.flatten_command_id == "kill-1"
    from trader.automation.session_controller import SessionController
    restarted = SessionController(
        journal=DomainJournal(db), db=db, calendar=XNYSCalendarPolicy(), broker=FakeBroker(),
        cancel=FakeCancel(), liquidation=liquidation, breaker=FakeBreaker(),
        time_exit=FakeTimeExitDispatch(), account_id=ACCOUNT, now=lambda: _utc(15, 47))
    restarted.recover(_utc(15, 47))
    liquidation.mark_flat(generation_id=4, root_id="kill-1")
    state = restarted.run_due(_utc(15, 48))
    assert (state.state, state.flatten_command_id, state.flat_generation) == ("FLAT", "kill-1", 4)


def test_time_exit_adapter_lets_a_refusal_reach_the_caller():
    """Fail loudly: only ExitInProgress (never possible without a quantity) is swallowed."""
    from trader.automation.session_controller import SessionTimeExitAdapter
    from trader.trading.liquidation_service import LiquidationRefused

    class _Refusing:
        def start(self, *a, **k):
            raise LiquidationRefused("NO_POSITION")

    with pytest.raises(LiquidationRefused):
        SessionTimeExitAdapter(_Refusing(), account_id=ACCOUNT, now=lambda: _utc(15, 0)).request_exit(
            command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")


def test_session_flatten_without_a_root_to_poll_fails_loudly(tmp_path):
    """R10: no silent fallback to the session's own cause; the flatten is retried on the next tick."""
    liquidation = FakeLiquidation()
    liquidation.start = lambda *a, **k: None
    controller, _b, _c, _l, _br, _t, _j, _db, clock = _build_controller(tmp_path, liquidation=liquidation)
    clock[0] = _utc(15, 46)
    with pytest.raises(RuntimeError, match="no root to poll"):
        controller.recover(clock[0])


def test_session_flatten_whose_root_ends_failed_safe_is_an_incident_at_once(tmp_path):
    """Ruling: a FAILED_SAFE root (joined or own) is not polled until the flat deadline."""
    controller, _b, _c, liquidation, breaker, _t, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(15, 46)
    state = controller.recover(clock[0])
    root = state.flatten_command_id
    liquidation.receipts[root] = SimpleNamespace(cause_command_id=root, state="FAILED_SAFE", generation_id=5,
                                                 detail="deadline elapsed")
    state = controller.run_due(_utc(15, 47))
    assert state.state == "INCIDENT" and "FAILED_SAFE" in state.incident
    assert [s.kind for s in breaker.signals] == ["LIQUIDATION_FAILED"]
