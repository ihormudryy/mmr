import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BrokerIngest

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "ingest.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ingest = BrokerIngest(
        db=db,
        journal=journal,
        store=store,
        account_id="DU123",
        account_mode="paper",
        session_epoch="s1",
        clock=lambda: UTC_NOW,
    )
    return SimpleNamespace(db=db, journal=journal, store=store, ingest=ingest)


def _events(db):
    rows = db.execute(
        "SELECT event_type, entity_type, entity_id, operation, entity_revision, payload "
        "FROM domain_event_journal ORDER BY source_cursor",
        fetch="all",
    )
    return [
        {
            "event_type": row[0],
            "entity_type": row[1],
            "entity_id": row[2],
            "operation": row[3],
            "entity_revision": row[4],
            "payload": json.loads(row[5]) if row[5] else None,
        }
        for row in rows
    ]


def fake_account_value(tag="NetLiquidation", value="50000.0", currency="USD"):
    return SimpleNamespace(account="DU123", tag=tag, value=value, currency=currency, modelCode="")


def fake_position(conid=265598, qty=10.0, avg=180.0):
    contract = SimpleNamespace(
        conId=conid, symbol="AAPL", secType="STK", exchange="NASDAQ", currency="USD"
    )
    return SimpleNamespace(account="DU123", contract=contract, position=qty, avgCost=avg)


def fake_portfolio_item(conid=265598, qty=10.0):
    contract = SimpleNamespace(
        conId=conid, symbol="AAPL", secType="STK", exchange="NASDAQ", currency="USD"
    )
    return SimpleNamespace(
        account="DU123",
        contract=contract,
        position=qty,
        marketPrice=185.0,
        marketValue=1850.0,
        averageCost=180.0,
        unrealizedPNL=50.0,
        realizedPNL=0.0,
    )


def test_callbacks_only_enqueue_until_drained(env):
    env.ingest.on_position(fake_position())
    assert _events(env.db) == []
    assert env.ingest.drain_once() == 1
    assert len(_events(env.db)) == 1


def test_account_value_journals_only_changed_fields(env):
    env.ingest.on_account_value(fake_account_value())
    env.ingest.drain_once()
    events = _events(env.db)
    assert [event["event_type"] for event in events] == ["account.updated"]
    assert events[0]["entity_id"] == "DU123"
    assert events[0]["payload"]["net_liquidation"] == 50000.0
    assert events[0]["payload"]["account_mode"] == "paper"

    env.ingest.on_account_value(fake_account_value())
    env.ingest.drain_once()
    assert len(_events(env.db)) == 1


def test_position_and_portfolio_callbacks_merge_into_one_entity(env):
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.on_portfolio_item(fake_portfolio_item(qty=10.0))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [event["entity_id"] for event in events] == ["DU123:265598", "DU123:265598"]
    assert [event["entity_revision"] for event in events] == [1, 2]
    assert events[-1]["payload"]["quantity"] == 10.0
    assert events[-1]["payload"]["market_value"] == 1850.0


def test_bare_position_does_not_erase_market_fields(env):
    env.ingest.on_portfolio_item(fake_portfolio_item(qty=10.0))
    env.ingest.drain_once()
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.drain_once()
    assert len(_events(env.db)) == 1


def test_zero_quantity_emits_tombstone_and_allows_resurrection(env):
    env.ingest.on_position(fake_position(qty=10.0))
    env.ingest.on_position(fake_position(qty=0.0))
    env.ingest.on_position(fake_position(qty=5.0))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [(event["operation"], event["entity_revision"]) for event in events] == [
        ("upsert", 1),
        ("delete", 2),
        ("upsert", 3),
    ]
    assert events[1]["payload"] is None


def test_zero_quantity_without_existing_row_is_ignored(env):
    env.ingest.on_position(fake_position(qty=0.0))
    env.ingest.drain_once()
    assert _events(env.db) == []


def test_callbacks_ignore_observations_for_an_unpinned_account(env):
    foreign = fake_position()
    foreign.account = "DU999"
    env.ingest.on_position(foreign)
    assert env.ingest.drain_once() == 0
    assert _events(env.db) == []
