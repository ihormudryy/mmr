"""Fakes and fixtures for the session ledger: real DuckDB, real calendar, fake broker/experiments/fx."""
import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import pytest

from tests.scoreboard.common import ACCOUNT, EXP_ID, NOW, UTC
from tests.scoreboard.fills import broker_store, put_fill
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.session_controller import (SessionControllerState, SessionStateStore,
                                                  apply_session_controller_migration)
from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.scoreboard.ports import ExperimentRecord, FxEvidence, NullAttributionLookup, SessionEnd
from trader.scoreboard.session_ledger import SessionLedger

CAL = XNYSCalendarPolicy()
D = dt.date(2026, 10, 6)          # Tuesday
D_NEXT = dt.date(2026, 10, 7)
START = dt.datetime(2026, 10, 6, 13, 35, tzinfo=UTC)
END = dt.datetime(2026, 10, 6, 19, 55, tzinfo=UTC)
END2 = dt.datetime(2026, 10, 7, 19, 55, tzinfo=UTC)
IN_SESSION = dt.datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
KILL_AT_SAME_ET_DATE = dt.datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
EXP = ExperimentRecord(EXP_ID, ACCOUNT, START, 100_000.0, "USD", 1.0, "ARMED", None)
END_FLAT = SessionEnd(ACCOUNT, D, "FLAT", END)


class FakeExperiments:
    def __init__(self, record=EXP):
        self.record = record

    def latest(self):
        return self.record

    def get(self, experiment_id):
        return self.record if self.record is not None and self.record.experiment_id == experiment_id else None


def position(quantity, market_value=1000.0, currency="USD", conid=265598):
    return BrokerPositionRow(account_id=ACCOUNT, conid=conid, symbol="AAPL", sec_type="STK", exchange="SMART",
                             currency=currency, quantity=float(quantity), average_cost=100.0, market_price=100.0,
                             market_value=market_value, unrealized_pnl=0.0, realized_pnl=0.0, daily_pnl=0.0,
                             deleted=False, revision=1, source_timestamp=NOW)


def snapshot(net_liquidation=100_250.0, positions=()):
    return BrokerRiskSnapshot(generation_id=1, source_cursor=1, promoted_at=NOW, account_id=ACCOUNT,
                              account_mode="paper", net_liquidation=net_liquidation, daily_pnl=0.0,
                              positions=tuple(positions), working_orders=())


def snap(gross=None, market_value="unset", currency="USD"):
    value = gross if market_value == "unset" else market_value
    return snapshot(positions=[position(10, value, currency)])


class FakeBroker:
    def __init__(self):
        self.net_liquidation = 100_250.0
        self.positions = []
        self.raises = None

    def capture(self, account_id):
        if self.raises is not None:
            raise self.raises
        return snapshot(self.net_liquidation, self.positions)


class FakeFx:
    def __init__(self):
        self.evidence_value = FxEvidence("USD", 1.0, "base_is_usd", END)
        self.raises = None

    def evidence(self):
        if self.raises is not None:
            raise self.raises
        return self.evidence_value


def controller_state(state, session_date=D):
    return SessionControllerState(
        account_id=ACCOUNT, session_date=session_date, calendar_name="XNYS", calendar_version="x", state=state,
        open_utc=None, close_utc=None, entry_cutoff_utc=None, cancel_entries_utc=None, flatten_start_utc=None,
        flat_deadline_utc=None, entry_cutoff_reached=True, flatten_command_id=None, flat_generation=None,
        incident=None if state != "INCIDENT" else "missed")


@dataclass
class World:
    db: Any
    store: Any
    broker_store: Any
    clock: list
    experiments: FakeExperiments = field(default_factory=FakeExperiments)
    broker: FakeBroker = field(default_factory=FakeBroker)
    fx: FakeFx = field(default_factory=FakeFx)
    written: list = field(default_factory=list)
    links: Any = field(default_factory=NullAttributionLookup)

    def ledger(self, on_row_written: Optional[Any] = "record"):
        callback = (lambda e, d: self.written.append((e, d))) if on_row_written == "record" else on_row_written
        return SessionLedger(store=self.store, db=self.db, experiments=self.experiments, broker=self.broker,
                             fx=self.fx, calendar=CAL, links=self.links, now=lambda: self.clock[0],
                             on_row_written=callback)

    def fill(self, exec_id, side, quantity, price, commission, when, **kw):
        put_fill(self.db, self.broker_store, ACCOUNT, exec_id, side, quantity, price, commission, when, **kw)

    def round_trip(self, day=D, buy=("e1", 10, 100, "1.00"), sell=("e2", 10, 101, "1.00")):
        base = dt.datetime.combine(day, dt.time(14, 0), tzinfo=UTC)
        for (exec_id, qty, px, comm), side, offset in ((buy, "BUY", 0), (sell, "SELL", 1)):
            self.fill(exec_id, side, qty, px, None if comm is None else float(comm),
                      base + dt.timedelta(hours=offset))

    def controller_row(self, state, session_date=D):
        SessionStateStore(self.db).save(controller_state(state, session_date), NOW)


@pytest.fixture
def world(db, migrator, store):
    apply_session_controller_migration(migrator)
    return World(db=db, store=store, broker_store=broker_store(db, migrator), clock=[IN_SESSION])


@pytest.fixture
def ledger(world):
    return world.ledger()


def with_experiment(world, **changes):
    world.experiments.record = replace(EXP, **changes)
