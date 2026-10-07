"""Shared builders for the ai_paper tests (Plan 3 Tasks 6-10)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Optional

import exchange_calendars as xcals
import pandas as pd

from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.trading.proposal_command_service import ExecutableQuote

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)      # Friday 11:00 ET, mid-session
ACCOUNT = "DU123"
CONID = 265598
OTHER = 272093


def pos(conid: int = CONID, quantity: float = 10.0, value: Optional[float] = None) -> BrokerPositionRow:
    value = quantity * 100.0 if value is None else value
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol=str(conid), sec_type="STK", exchange="SMART",
        currency="USD", quantity=quantity, average_cost=100.0, market_price=100.0,
        market_value=value, unrealized_pnl=0.0, realized_pnl=0.0, daily_pnl=0.0,
        deleted=False, revision=1, source_timestamp=NOW)


def order(*, conid: int = CONID, leg: Optional[str] = "entry", is_external: bool = False,
          total: float = 10.0, filled: float = 0.0, action: str = "BUY", group: Optional[str] = None,
          limit: Optional[float] = 100.0, entity: Optional[str] = None, status: str = "Submitted",
          deleted: bool = False) -> BrokerOrderRow:
    group = group if group is not None else f"og-{conid}-{leg}"
    return BrokerOrderRow(
        order_entity_id=entity or f"{group}:{leg}", account_id=ACCOUNT, conid=conid, symbol=str(conid),
        order_group_id=group, leg=leg, is_external=is_external, action=action, order_type="LMT",
        total_quantity=total, filled_quantity=filled, avg_fill_price=None, limit_price=limit,
        stop_price=None, tif="DAY", status=status, deleted=deleted, revision=1, source_timestamp=NOW)


def snapshot(*, positions=(), working=(), net_liquidation=1_000_000.0, daily_pnl=0.0, account=ACCOUNT,
             mode="paper", generation=5, cursor=9) -> BrokerRiskSnapshot:
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=cursor, promoted_at=NOW, account_id=account,
        account_mode=mode, net_liquidation=net_liquidation, daily_pnl=daily_pnl,
        positions=tuple(positions), working_orders=tuple(working))


def quote(*, ask=100.0, bid=99.95, age=0.0, feed="live", side="BUY", conid=CONID, ask_size=10_000.0,
          session_state="continuous") -> ExecutableQuote:
    return ExecutableQuote(
        conid=conid, side=side, price=ask, market_timestamp=NOW - dt.timedelta(seconds=age),
        feed_type=feed, session_state=session_state, bid=bid, ask=ask, bid_size=1_000.0, ask_size=ask_size)


def secdef(symbol="AAPL", primary="NASDAQ", sec_type="STK", *, exchange="SMART", conid=CONID):
    return SimpleNamespace(symbol=symbol, primaryExchange=primary, secType=sec_type, exchange=exchange,
                           conId=conid)


class FakeUniverse:
    """``resolve_symbol(conid)`` like UniverseAccessor: a list of definitions."""

    def __init__(self, rows_by_conid):
        self.rows_by_conid = rows_by_conid

    def resolve_symbol(self, symbol, *args, **kwargs):
        rows = self.rows_by_conid.get(symbol, [])
        return list(rows) if isinstance(rows, (list, tuple)) else [rows]


class SnapshotSequence:
    """Returns the snapshots in order and then repeats the last one."""

    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def capture(self, account_id):
        self.calls += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


def daily_frame(now=NOW, volume=1_000_000.0):
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(now.date() - dt.timedelta(days=90), now.date())
    closed = [day for day in sessions if calendar.session_close(day) < pd.Timestamp(now)][-20:]
    return pd.DataFrame(
        {"close": [100.0] * 20, "volume": [volume] * 20, "bar_size": "1 day", "what_to_show": 1},
        index=pd.DatetimeIndex(closed).tz_localize("America/New_York").rename("date"),
    )


def make_journal(path: str):
    from trader.data.domain_journal import DomainJournal
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.promotion.canary_risk import apply_canary_risk_migration
    db = DuckDBConnection.get_instance(path)
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_canary_risk_migration(migrator)
    return journal


def make_history(path: str):
    from trader.data.data_access import TickStorage
    from trader.objects import BarSize
    storage = TickStorage(path)
    for conid in (CONID, OTHER):
        storage.get_tickdata(BarSize.Days1).write(conid, daily_frame())
    return storage


SESSION = SimpleNamespace(anchor=1_000_000.0)
GOOD_MARGIN = {"initMarginAfter": 5_000.0, "equityWithLoanAfter": 995_000.0}


class Quotes:
    def __init__(self, value=None):
        self.value = quote() if value is None else value

    def executable_quote(self, conid, *, side):
        return self.value


class Margin:
    def __init__(self, response=GOOD_MARGIN, error=None):
        self.response, self.error = response, error

    def what_if_margin(self, conid, side, quantity):
        if self.error is not None:
            raise self.error
        return self.response


def prepare(parts, **changes):
    """SP1's own ENTER sizing (``AiPaperEvidence.prepare_entry``) on the ``parts`` fixture."""
    from trader.automation.ai_paper_evidence import AiPaperEvidence
    from trader.automation.risk_limits import PAPER_LIMITS
    args = dict(conid=CONID, stop_price=98.0, requested_quantity=None, limits=PAPER_LIMITS,
                session=SESSION, notional=1e9, experiment_id="exp1")
    args.update(changes)
    return AiPaperEvidence(**parts).prepare_entry(**args)
