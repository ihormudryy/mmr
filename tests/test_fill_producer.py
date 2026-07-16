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
    db = DuckDBConnection.get_instance(str(tmp_path / "fills.duckdb"))
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


def fake_trade(client_order_id=5, perm_id=100, order_ref="mmr:grp1"):
    order = SimpleNamespace(
        orderId=client_order_id,
        permId=perm_id,
        parentId=0,
        orderRef=order_ref,
        account="DU123",
        action="BUY",
        orderType="MKT",
        totalQuantity=10.0,
        lmtPrice=0.0,
        auxPrice=0.0,
        tif="DAY",
    )
    status = SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0)
    contract = SimpleNamespace(
        conId=265598, symbol="AAPL", secType="STK", exchange="NASDAQ", currency="USD"
    )
    return SimpleNamespace(order=order, orderStatus=status, contract=contract)


def fake_fill(exec_id="0001.abc", perm_id=100, client_order_id=5, conid=265598,
              side="BOT", shares=10.0, price=185.0):
    execution = SimpleNamespace(
        execId=exec_id,
        acctNumber="DU123",
        permId=perm_id,
        orderId=client_order_id,
        side=side,
        shares=shares,
        price=price,
        time=UTC_NOW,
    )
    contract = SimpleNamespace(
        conId=conid, symbol="AAPL", secType="STK", exchange="NASDAQ", currency="USD"
    )
    return SimpleNamespace(execution=execution, contract=contract)


def fake_commission(commission=1.25, currency="USD", realized_pnl=None):
    return SimpleNamespace(
        commission=commission,
        currency=currency,
        realizedPNL=(realized_pnl if realized_pnl is not None else 1.7976931348623157e308),
    )


def test_exec_details_persist_once_per_account_and_exec_id(env):
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.drain_once()
    events = _events(env.db)
    assert [event["event_type"] for event in events] == ["fill.received"]
    assert events[0]["entity_id"] == "DU123:0001.abc"
    assert events[0]["payload"]["side"] == "BUY"
    assert events[0]["payload"]["quantity"] == 10.0


def test_commission_report_revises_the_same_fill(env):
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.on_commission_report(None, fake_fill(), fake_commission(commission=1.25))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [(event["event_type"], event["entity_revision"]) for event in events] == [
        ("fill.received", 1),
        ("fill.updated", 2),
    ]
    assert {event["entity_id"] for event in events} == {"DU123:0001.abc"}
    assert events[1]["payload"]["commission"] == 1.25


def test_duplicate_commission_report_is_a_noop(env):
    env.ingest.on_exec_details(None, fake_fill())
    env.ingest.on_commission_report(None, fake_fill(), fake_commission())
    env.ingest.on_commission_report(None, fake_fill(), fake_commission())
    env.ingest.drain_once()
    assert len(_events(env.db)) == 2


def test_commission_before_exec_details_creates_then_revises(env):
    env.ingest.on_commission_report(None, fake_fill(), fake_commission())
    env.ingest.drain_once()
    events = _events(env.db)
    assert [event["event_type"] for event in events] == ["fill.received", "fill.updated"]
    assert events[-1]["payload"]["commission"] == 1.25


def test_fill_before_order_resolves_on_order_arrival(env):
    env.ingest.on_exec_details(None, fake_fill(perm_id=100))
    env.ingest.drain_once()
    assert _events(env.db)[-1]["payload"]["order_entity_id"] is None

    env.ingest.on_open_order(fake_trade(client_order_id=5, perm_id=100))
    env.ingest.drain_once()
    fill_events = [event for event in _events(env.db) if event["entity_type"] == "fill"]
    assert [(event["event_type"], event["entity_revision"]) for event in fill_events] == [
        ("fill.received", 1),
        ("fill.updated", 2),
    ]
    assert fill_events[-1]["payload"]["order_entity_id"] == "grp1:entry"
