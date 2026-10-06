import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerFillRow, BrokerPositionRow, BrokerStateStore
from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.snapshot_service import DomainSnapshotService

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "broker.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    return SimpleNamespace(db=db, journal=journal, store=store)


def _position(quantity=10.0, revision=1, deleted=False):
    return BrokerPositionRow(
        account_id="DU123",
        conid=265598,
        symbol="AAPL",
        sec_type="STK",
        exchange="NASDAQ",
        currency="USD",
        quantity=quantity,
        average_cost=180.0,
        market_price=185.0,
        market_value=1850.0,
        unrealized_pnl=50.0,
        realized_pnl=0.0,
        daily_pnl=12.0,
        deleted=deleted,
        revision=revision,
        source_timestamp=UTC_NOW,
    )


def test_migration_is_idempotent_and_creates_all_tables(env):
    env.store.migrate(SchemaMigrator(env.db))
    tables = {
        row[0]
        for row in env.db.execute(
            "SELECT table_name FROM information_schema.tables", fetch="all"
        )
    }
    assert {
        "broker_account_state",
        "broker_positions",
        "broker_orders",
        "broker_order_aliases",
        "broker_fills",
        "broker_sync_generations",
        "broker_sync_staging",
    } <= tables


def test_position_upsert_replaces_by_account_and_conid(env):
    def _tx(conn):
        env.store.upsert_position_in_tx(conn, _position(quantity=10.0, revision=1))
        env.store.upsert_position_in_tx(conn, _position(quantity=25.0, revision=2))
        return env.store.get_position_in_tx(conn, "DU123", 265598)

    row = env.db.transaction(_tx)
    assert row.quantity == 25.0
    assert row.revision == 2
    count = env.db.execute("SELECT COUNT(*) FROM broker_positions", fetch="one")[0]
    assert count == 1


def test_fill_identity_is_unique_per_account_and_exec_id(env):
    fill = BrokerFillRow(
        account_id="DU123",
        exec_id="0001.abc",
        order_entity_id=None,
        perm_id=777,
        client_order_id=5,
        session_epoch="s1",
        conid=265598,
        side="BUY",
        quantity=10.0,
        price=185.0,
        commission=None,
        commission_currency=None,
        realized_pnl=None,
        fill_time=UTC_NOW,
        revision=1,
        source_timestamp=UTC_NOW,
    )

    def _tx(conn):
        env.store.upsert_fill_in_tx(conn, fill)
        env.store.upsert_fill_in_tx(conn, replace(fill, commission=1.5, revision=2))
        return env.store.get_fill_in_tx(conn, "DU123", "0001.abc")

    row = env.db.transaction(_tx)
    assert row.commission == 1.5 and row.revision == 2
    assert env.db.execute("SELECT COUNT(*) FROM broker_fills", fetch="one")[0] == 1


def test_alias_binding_is_idempotent_and_scoped(env):
    def _tx(conn):
        env.store.bind_alias_in_tx(
            conn, "perm_id", "777", "DU123", "", "grp1:entry", UTC_NOW
        )
        env.store.bind_alias_in_tx(
            conn, "perm_id", "777", "DU123", "", "grp1:entry", UTC_NOW
        )
        env.store.bind_alias_in_tx(
            conn, "client_order_id", "5", "DU123", "s1", "grp1:entry", UTC_NOW
        )
        env.store.bind_alias_in_tx(
            conn, "client_order_id", "5", "DU123", "s2", "ext:other", UTC_NOW
        )
        return (
            env.store.find_order_by_alias_in_tx(conn, "perm_id", "777", "DU123", ""),
            env.store.find_order_by_alias_in_tx(
                conn, "client_order_id", "5", "DU123", "s1"
            ),
            env.store.find_order_by_alias_in_tx(
                conn, "client_order_id", "5", "DU123", "s2"
            ),
        )

    by_perm, by_cid_s1, by_cid_s2 = env.db.transaction(_tx)
    assert by_perm == "grp1:entry"
    assert by_cid_s1 == "grp1:entry"
    assert by_cid_s2 == "ext:other"


def test_find_perm_id_for_order_reverse_lookup(env):
    # The cancel adapter resolves order_entity_id -> stable perm_id through this
    # reverse lookup. It must return the perm_id alias (as int), never the
    # session-scoped client_order_id, and None when no perm_id has been observed.
    def _tx(conn):
        env.store.bind_alias_in_tx(
            conn, "perm_id", "987654321", "DU123", "", "og-cmd1:entry", UTC_NOW)
        env.store.bind_alias_in_tx(
            conn, "client_order_id", "3", "DU123", "s1", "og-cmd1:entry", UTC_NOW)
        return (
            env.store.find_perm_id_for_order_in_tx(conn, "og-cmd1:entry"),
            env.store.find_perm_id_for_order_in_tx(conn, "og-cmd1:stop"),
        )

    found, missing = env.db.transaction(_tx)
    assert found == 987654321 and isinstance(found, int)
    assert missing is None


def test_tombstoned_position_is_excluded_from_active(env):
    def _tx(conn):
        env.store.upsert_position_in_tx(conn, _position(revision=1))
        env.store.tombstone_position_in_tx(
            conn, "DU123", 265598, revision=2, source_timestamp=UTC_NOW
        )
        return env.store.select_active_positions_in_tx(conn)

    assert env.db.transaction(_tx) == []


def test_generation_lifecycle_rows(env):
    def _open(conn):
        return env.store.open_generation_in_tx(conn, ("account", "positions"), UTC_NOW)

    generation_id = env.db.transaction(_open)

    def _rest(conn):
        env.store.stage_in_tx(
            conn,
            generation_id,
            1,
            "positions",
            "position",
            "position:DU123:265598",
            json.dumps({"k": "v"}),
        )
        env.store.mark_source_complete_in_tx(conn, generation_id, "account")
        env.store.mark_generation_promoted_in_tx(
            conn, generation_id, cursor=17, completed_at=UTC_NOW
        )
        env.store.purge_staging_in_tx(conn, generation_id)
        return (
            env.store.latest_promoted_generation_in_tx(conn),
            env.store.staged_rows_in_tx(conn, generation_id),
        )

    latest, staged = env.db.transaction(_rest)
    assert latest == generation_id
    assert staged == []


def test_snapshot_adapter_exposes_only_active_broker_rows_with_fenced_identity(env):
    position = _position(revision=0)
    mutation = DomainMutation(
        event_type="position.updated",
        entity_type="position",
        entity_id="DU123:265598",
        operation="upsert",
        account_id="DU123",
        source="test",
        source_timestamp=UTC_NOW,
        correlation_id=None,
        payload=position.to_payload(),
    )
    event = env.journal.mutate(
        env.journal.connect(),
        mutation,
        lambda conn, revision: env.store.upsert_position_in_tx(
            conn, replace(position, revision=revision)
        ),
        event_id="position-1",
    )

    service = DomainSnapshotService(env.journal)
    from trader.data.broker_state import broker_materialized_adapters

    for adapter in broker_materialized_adapters(env.store):
        service.register_adapter(adapter)
    snapshot = service.snapshot_with_cursor()

    assert snapshot.source_cursor == event.source_cursor
    assert snapshot.entities["position"] == [{
        **replace(position, revision=event.entity_revision).to_payload(),
    }]


def _group_order(order_entity_id, group, deleted=False, status="Cancelled"):
    from trader.data.broker_state import BrokerOrderRow

    return BrokerOrderRow(
        order_entity_id=order_entity_id, account_id="DU123", conid=265598, symbol="AAPL",
        order_group_id=group, leg="entry", is_external=False, action="BUY", order_type="LMT",
        total_quantity=10.0, filled_quantity=0.0, avg_fill_price=None, limit_price=185.0,
        stop_price=None, tif="DAY", status=status, deleted=deleted, revision=1,
        source_timestamp=UTC_NOW,
    )


def _group_fill(order_entity_id):
    return BrokerFillRow(
        account_id="DU123", exec_id="0001.xyz", order_entity_id=order_entity_id, perm_id=1,
        client_order_id=5, session_epoch="s1", conid=265598, side="BUY", quantity=10.0,
        price=185.0, commission=None, commission_currency=None, realized_pnl=None,
        fill_time=UTC_NOW, revision=1, source_timestamp=UTC_NOW,
    )


def _has_trace(env, group):
    return env.db.transaction(
        lambda conn: env.store.group_has_broker_trace_in_tx(conn, "DU123", group)
    )


def test_group_trace_is_absent_without_orders_or_executions(env):
    assert _has_trace(env, "og-1") is False


def test_group_trace_counts_terminal_and_deleted_orders(env):
    env.db.transaction(lambda conn: env.store.upsert_order_in_tx(
        conn, _group_order("o1", "og-1", deleted=True)))

    assert _has_trace(env, "og-1") is True
    assert _has_trace(env, "og-2") is False


def test_group_trace_counts_an_execution_bound_to_the_group(env):
    def seed(conn):
        env.store.upsert_order_in_tx(conn, _group_order("o1", "og-1"))
        env.store.upsert_fill_in_tx(conn, _group_fill("o1"))

    env.db.transaction(seed)

    assert _has_trace(env, "og-1") is True


def test_group_trace_is_never_a_false_no_while_an_execution_is_unbound(env):
    env.db.transaction(lambda conn: env.store.upsert_fill_in_tx(conn, _group_fill(None)))

    assert _has_trace(env, "og-1") is True
