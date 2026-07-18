from __future__ import annotations

import datetime as dt
import math
import threading

import duckdb
import pytest

from trader.data.broker_state import (
    BrokerAccountRow,
    BrokerOrderRow,
    BrokerPositionRow,
    BrokerRiskSnapshotError,
    BrokerStateStore,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_ports import TraderBrokerRiskSnapshotAuthority


UTC = dt.timezone.utc
T1 = dt.datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
T2 = dt.datetime(2026, 7, 18, 13, 1, tzinfo=UTC)
ACCOUNT = "DU123"
CONID = 265598


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    yield db, store
    journal.close()


def _account(*, mode="paper", net_liq=100_000.0, daily_pnl=-250.0, revision=1, ts=T1):
    return BrokerAccountRow(
        account_id=ACCOUNT,
        account_mode=mode,
        net_liquidation=net_liq,
        total_cash=50_000.0,
        buying_power=200_000.0,
        available_funds=150_000.0,
        maintenance_margin=5_000.0,
        balances={"DailyPnL:USD": str(daily_pnl)},
        revision=revision,
        source_timestamp=ts,
    )


def _position(*, quantity=10.0, value=2_100.0, daily_pnl=-25.0, revision=1, ts=T1):
    return BrokerPositionRow(
        account_id=ACCOUNT,
        conid=CONID,
        symbol="AAPL",
        sec_type="STK",
        exchange="NASDAQ",
        currency="USD",
        quantity=quantity,
        average_cost=200.0,
        market_price=210.0,
        market_value=value,
        unrealized_pnl=100.0,
        realized_pnl=0.0,
        daily_pnl=daily_pnl,
        deleted=False,
        revision=revision,
        source_timestamp=ts,
    )


def _order(*, entity="og-1:entry", quantity=5.0, revision=1, ts=T1):
    return BrokerOrderRow(
        order_entity_id=entity,
        account_id=ACCOUNT,
        conid=CONID,
        symbol="AAPL",
        order_group_id="og-1",
        leg="entry",
        is_external=False,
        action="BUY",
        order_type="LMT",
        total_quantity=quantity,
        filled_quantity=0.0,
        avg_fill_price=None,
        limit_price=209.0,
        stop_price=None,
        tif="DAY",
        status="Submitted",
        deleted=False,
        revision=revision,
        source_timestamp=ts,
    )


def _promote(db, store, *, account=None, position=None, order=None, when=T1):
    def write(conn):
        if account is not None:
            store.upsert_account_in_tx(conn, account)
        if position is not None:
            store.upsert_position_in_tx(conn, position)
        if order is not None:
            store.upsert_order_in_tx(conn, order)
        generation = store.open_generation_in_tx(
            conn, ("account", "positions", "open_orders"), when
        )
        store.mark_generation_promoted_in_tx(conn, generation, cursor=0, completed_at=when)
        return generation

    return db.transaction(write)


def test_latest_snapshot_is_one_transactional_generation(env):
    db, store = env
    first = _promote(
        db, store, account=_account(net_liq=90_000),
        position=_position(quantity=3, value=630), order=_order(quantity=2), when=T1,
    )
    second = _promote(
        db, store, account=_account(net_liq=110_000, daily_pnl=75, revision=2, ts=T2),
        position=_position(quantity=12, value=2_520, revision=2, ts=T2),
        order=_order(quantity=7, revision=2, ts=T2), when=T2,
    )

    snapshot = db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))

    assert second > first
    assert snapshot.generation_id == second
    assert snapshot.source_cursor == 0
    assert snapshot.promoted_at == T2
    assert snapshot.net_liquidation == 110_000
    assert snapshot.daily_pnl == 75
    assert snapshot.reducible_quantity(CONID) == 12
    assert snapshot.position_value(CONID) == 2_520
    assert snapshot.open_order_count == 1
    assert snapshot.working_orders[0].total_quantity == 7


def test_no_promoted_generation_fails_closed(env):
    db, store = env
    db.transaction(lambda conn: store.upsert_account_in_tx(conn, _account()))

    with pytest.raises(BrokerRiskSnapshotError) as error:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))

    assert error.value.code == "NO_PROMOTED_GENERATION"


def test_account_mismatch_fails_closed(env):
    db, store = env
    _promote(db, store, account=_account())

    with pytest.raises(BrokerRiskSnapshotError) as error:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, "DU999"))

    assert error.value.code == "ACCOUNT_MISMATCH"


def test_newer_staging_generation_blocks_use_of_old_promotion(env):
    db, store = env
    _promote(db, store, account=_account())
    db.transaction(
        lambda conn: store.open_generation_in_tx(
            conn, ("account", "positions", "open_orders"), T2
        )
    )

    with pytest.raises(BrokerRiskSnapshotError) as error:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))

    assert error.value.code == "GENERATION_STAGING"


def test_promoted_cursor_ahead_of_visible_journal_fails_closed(env):
    db, store = env
    generation = _promote(db, store, account=_account())
    db.execute(
        "UPDATE broker_sync_generations SET promoted_cursor = 99 WHERE generation_id = ?",
        [generation],
    )

    with pytest.raises(BrokerRiskSnapshotError) as error:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))

    assert error.value.code == "INVALID_GENERATION_CURSOR"


def test_active_position_without_daily_pnl_fails_closed(env):
    db, store = env
    account = _account()
    account = BrokerAccountRow(**{
        **account.__dict__, "balances": {},
    })
    _promote(db, store, account=account, position=_position(daily_pnl=None))

    with pytest.raises(BrokerRiskSnapshotError) as error:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))

    assert error.value.code == "DAILY_PNL_UNAVAILABLE"


@pytest.mark.parametrize(
    ("net_liq", "daily_pnl", "code"),
    [
        (math.nan, 0.0, "INVALID_NET_LIQUIDATION"),
        (math.inf, 0.0, "INVALID_NET_LIQUIDATION"),
        (100_000.0, math.nan, "INVALID_DAILY_PNL"),
        (100_000.0, math.inf, "INVALID_DAILY_PNL"),
    ],
)
def test_non_finite_account_risk_values_fail_closed(env, net_liq, daily_pnl, code):
    db, store = env
    _promote(db, store, account=_account(net_liq=net_liq, daily_pnl=daily_pnl))

    with pytest.raises(BrokerRiskSnapshotError) as error:
        db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))

    assert error.value.code == code


def test_authority_pins_account_mode_and_rejects_generation_regression(env):
    db, store = env
    first = _promote(db, store, account=_account(mode="paper"), when=T1)
    second = _promote(db, store, account=_account(mode="paper", revision=2, ts=T2), when=T2)
    authority = TraderBrokerRiskSnapshotAuthority(
        db=db, store=store, account_id=ACCOUNT, account_mode="paper"
    )
    assert authority.capture(ACCOUNT).generation_id == second

    db.execute("DELETE FROM broker_sync_generations WHERE generation_id = ?", [second])
    with pytest.raises(BrokerRiskSnapshotError) as regression:
        authority.capture(ACCOUNT)
    assert regression.value.code == "GENERATION_REGRESSION"

    live_authority = TraderBrokerRiskSnapshotAuthority(
        db=db, store=store, account_id=ACCOUNT, account_mode="live"
    )
    with pytest.raises(BrokerRiskSnapshotError) as mismatch:
        live_authority.capture(ACCOUNT)
    assert mismatch.value.code == "ACCOUNT_MODE_MISMATCH"
    assert first < second


def test_authority_rejects_snapshot_while_broker_transport_is_not_ready(env):
    db, store = env
    _promote(db, store, account=_account())
    authority = TraderBrokerRiskSnapshotAuthority(
        db=db, store=store, account_id=ACCOUNT, account_mode="paper",
        ready=lambda: False,
    )

    with pytest.raises(BrokerRiskSnapshotError) as error:
        authority.capture(ACCOUNT)

    assert error.value.code == "BROKER_UNAVAILABLE"


def test_open_read_transaction_cannot_mix_a_concurrent_generation_commit(env):
    db, store = env
    first = _promote(
        db, store, account=_account(net_liq=90_000),
        position=_position(quantity=3, value=630), order=_order(quantity=2), when=T1,
    )
    account_read = threading.Event()
    writer_done = threading.Event()
    original = store.select_active_positions_in_tx

    def interleaved_positions(conn):
        account_read.set()
        assert writer_done.wait(timeout=3)
        return original(conn)

    store.select_active_positions_in_tx = interleaved_positions

    def writer():
        assert account_read.wait(timeout=3)
        conn = duckdb.connect(db.db_path)
        conn.execute("BEGIN TRANSACTION")
        try:
            store.upsert_account_in_tx(
                conn, _account(net_liq=120_000, daily_pnl=80, revision=2, ts=T2)
            )
            store.upsert_position_in_tx(
                conn, _position(quantity=20, value=4_200, revision=2, ts=T2)
            )
            store.upsert_order_in_tx(conn, _order(quantity=9, revision=2, ts=T2))
            generation = store.open_generation_in_tx(
                conn, ("account", "positions", "open_orders"), T2
            )
            store.mark_generation_promoted_in_tx(conn, generation, cursor=0, completed_at=T2)
            conn.execute("COMMIT")
        finally:
            conn.close()
            writer_done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    snapshot = db.transaction(lambda conn: store.capture_risk_snapshot_in_tx(conn, ACCOUNT))
    thread.join(timeout=3)

    assert snapshot.generation_id == first
    assert snapshot.net_liquidation == 90_000
    assert snapshot.reducible_quantity(CONID) == 3
    assert snapshot.working_orders[0].total_quantity == 2
