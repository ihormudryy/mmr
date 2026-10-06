"""SP1 plan 1 Task 18: the broker evidence the safe close relies on."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BrokerIngest
from trader.trading.command_ports import TraderBrokerAuthority
from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)
ACCOUNT = "DU123"


class _Ingest:
    """``BrokerIngest.is_ready`` is a property, not a method."""
    def __init__(self, ready):
        self._ready = ready

    @property
    def is_ready(self):
        return self._ready


@pytest.mark.parametrize("ready", [True, False])
def test_enumeration_complete_reads_the_readiness_property(ready):
    trader = SimpleNamespace(broker_ingest=_Ingest(ready))
    assert TradingRuntimeOrderDispatch(trader).enumeration_complete() is ready


def test_enumeration_is_not_complete_without_an_ingest():
    assert TradingRuntimeOrderDispatch(SimpleNamespace()).enumeration_complete() is False


@pytest.mark.parametrize("ready", [True, False])
def test_broker_authority_readiness_reads_the_property(ready):
    trader = SimpleNamespace(broker_ingest=_Ingest(ready))
    authority = TraderBrokerAuthority(trader, run_coro=lambda c: None, resolve_contract=lambda conid: None)
    assert authority.is_ready() is ready


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "evidence.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ingest = BrokerIngest(db=db, journal=journal, store=store, account_id=ACCOUNT, account_mode="paper",
                          session_epoch="s1", clock=lambda: NOW)
    trader = SimpleNamespace(broker_state_store=store, domain_journal=journal, broker_ingest=ingest)
    return SimpleNamespace(db=db, journal=journal, store=store, ingest=ingest, migrator=migrator,
                           dispatch=TradingRuntimeOrderDispatch(trader))


def test_newest_generation_counts_a_generation_that_is_still_staging(env):
    def promoted_then_staging(conn):
        first = env.store.open_generation_in_tx(conn, ("account",), NOW)
        env.store.mark_generation_promoted_in_tx(conn, first, 1, NOW)
        return first, env.store.open_generation_in_tx(conn, ("account",), NOW)
    first, staging = env.db.transaction(promoted_then_staging)
    assert env.db.transaction(env.store.latest_promoted_generation_in_tx) == first
    assert env.dispatch.newest_generation() == staging > first


def test_newest_generation_fails_loudly_without_a_store():
    with pytest.raises(RuntimeError):
        TradingRuntimeOrderDispatch(SimpleNamespace()).newest_generation()


def test_order_rows_carry_the_oca_group_and_type(env):
    assert 38 in env.migrator.applied_versions()
    order = SimpleNamespace(orderId=7, permId=70, parentId=0, orderRef="mmr:p-1-reprotect-stop-265598-1",
                            account=ACCOUNT, action="SELL", orderType="STP", totalQuantity=6.0, lmtPrice=0.0,
                            auxPrice=95.0, tif="DAY", ocaGroup="p-1-reprotect-265598-1", ocaType=2)
    trade = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted", filled=0.0,
                                                                     avgFillPrice=0.0),
                            contract=SimpleNamespace(conId=265598, symbol="AAPL"))
    env.ingest.on_open_order(trade)
    env.ingest.drain_once()
    [row] = env.store.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type, row.leg) == ("p-1-reprotect-265598-1", 2, "stop")


def _stop_trade(oca_group, oca_type, status="Submitted"):
    order = SimpleNamespace(orderId=7, permId=70, parentId=0, orderRef="mmr:p-1-reprotect-stop-265598-1",
                            account=ACCOUNT, action="SELL", orderType="STP", totalQuantity=6.0, lmtPrice=0.0,
                            auxPrice=95.0, tif="DAY", ocaGroup=oca_group, ocaType=oca_type)
    return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=status, filled=0.0, avgFillPrice=0.0),
                           contract=SimpleNamespace(conId=265598, symbol="AAPL"))


def test_an_explicit_empty_oca_clears_the_stored_link(env):
    """#45: the broker saying "no OCA" ('' and 0) is not a missing field; it clears the link, also after a
    restart, so a close cannot take the leg as linked protection and end DONE."""
    from trader.trading.order_correlation import encode_order_ref

    env.ingest.on_open_order(_stop_trade("p-1-reprotect-265598-1", 2))
    env.ingest.drain_once()
    env.ingest.on_open_order(_stop_trade("", 0))
    env.ingest.drain_once()
    [row] = env.store.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type) == (None, None)
    restarted = BrokerStateStore(DuckDBConnection.get_instance(str(env.db.db_path)))
    [row] = restarted.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type) == (None, None)
    [found] = env.dispatch.find_by_order_ref(ACCOUNT, encode_order_ref("p-1-reprotect-stop-265598-1"))
    assert found.oca_type != 2                     # DONE's link check needs type 2 and the group


def test_an_observation_without_oca_fields_keeps_the_stored_link(env):
    env.ingest.on_open_order(_stop_trade("p-1-reprotect-265598-1", 2))
    env.ingest.drain_once()
    trade = _stop_trade("x", 0)
    del trade.order.ocaGroup, trade.order.ocaType
    env.ingest.on_open_order(trade)
    env.ingest.drain_once()
    [row] = env.store.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type) == ("p-1-reprotect-265598-1", 2)
