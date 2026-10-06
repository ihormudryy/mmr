import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BrokerIngest
from trader.trading.order_correlation import (
    classify_leg, decode_order_ref, encode_order_ref, legacy_reduce_prefix, liquidation_child_id,
    liquidation_child_kind, matches_legacy_reduce, reprotect_oca_group,
)

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "orders.duckdb"))
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


def fake_trade(
    client_order_id=5,
    perm_id=0,
    parent_id=0,
    order_ref="mmr:grp1",
    order_type="MKT",
    status="Submitted",
    filled=0.0,
    conid=265598,
    account="DU123",
    quantity=10.0,
):
    order = SimpleNamespace(
        orderId=client_order_id,
        permId=perm_id,
        parentId=parent_id,
        orderRef=order_ref,
        account=account,
        action="BUY",
        orderType=order_type,
        totalQuantity=quantity,
        lmtPrice=0.0,
        auxPrice=0.0,
        tif="DAY",
    )
    order_status = SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0)
    contract = SimpleNamespace(
        conId=conid, symbol="AAPL", secType="STK", exchange="NASDAQ", currency="USD"
    )
    return SimpleNamespace(order=order, orderStatus=order_status, contract=contract)


def test_order_ref_round_trip():
    assert encode_order_ref("grp1") == "mmr:grp1"
    assert decode_order_ref("mmr:grp1") == "grp1"
    assert decode_order_ref("manual TWS ref") is None
    assert decode_order_ref(None) is None
    assert decode_order_ref("") is None


def test_classify_leg():
    assert classify_leg("MKT", 0, 5) == "entry"
    assert classify_leg("LMT", 5, 6) == "take_profit"
    assert classify_leg("STP", 5, 7) == "stop"
    assert classify_leg("TRAIL", 5, 7) == "stop"
    assert classify_leg("MOC", 5, 9) == "child-9"


def test_first_observation_mints_immutable_group_leg_entity(env):
    env.ingest.on_open_order(fake_trade())
    env.ingest.drain_once()
    events = _events(env.db)
    assert events[0]["entity_type"] == "order"
    assert events[0]["entity_id"] == "grp1:entry"
    assert events[0]["event_type"] == "order.updated"
    assert events[0]["payload"]["status"] == "Submitted"


def test_late_perm_id_never_rekeys_or_forks_the_revision_stream(env):
    env.ingest.on_open_order(fake_trade(perm_id=0))
    env.ingest.on_order_status(fake_trade(perm_id=999888, status="Filled", filled=10.0))
    env.ingest.drain_once()
    events = _events(env.db)
    assert [event["entity_id"] for event in events] == ["grp1:entry", "grp1:entry"]
    assert [event["entity_revision"] for event in events] == [1, 2]
    alias = env.db.execute(
        "SELECT order_entity_id FROM broker_order_aliases "
        "WHERE alias_type = 'perm_id' AND alias_value = '999888'",
        fetch="one",
    )
    assert alias == ("grp1:entry",)


def test_bracket_legs_get_distinct_immutable_entities(env):
    env.ingest.on_open_order(fake_trade(client_order_id=5, order_type="MKT", parent_id=0))
    env.ingest.on_open_order(fake_trade(client_order_id=6, order_type="LMT", parent_id=5))
    env.ingest.on_open_order(fake_trade(client_order_id=7, order_type="STP", parent_id=5))
    env.ingest.drain_once()
    assert {event["entity_id"] for event in _events(env.db)} == {
        "grp1:entry",
        "grp1:take_profit",
        "grp1:stop",
    }


def test_external_order_gets_persisted_local_id(env):
    env.ingest.on_open_order(fake_trade(order_ref="manual TWS ref", perm_id=42))
    env.ingest.drain_once()
    events = _events(env.db)
    assert events[0]["entity_id"].startswith("ext:")
    assert events[0]["payload"]["is_external"] is True


def test_client_order_id_reuse_after_restart_creates_new_entity(env):
    env.ingest.on_open_order(fake_trade(client_order_id=5, order_ref="ref-a", perm_id=100))
    env.ingest.drain_once()
    restarted = BrokerIngest(
        db=env.db,
        journal=env.journal,
        store=env.store,
        account_id="DU123",
        account_mode="paper",
        session_epoch="s2",
        clock=lambda: UTC_NOW,
    )
    restarted.on_open_order(fake_trade(client_order_id=5, order_ref="ref-b", perm_id=200))
    restarted.drain_once()
    assert len({event["entity_id"] for event in _events(env.db)}) == 2


def test_restart_resolves_perm_id_to_same_entity(env):
    env.ingest.on_open_order(fake_trade(client_order_id=5, perm_id=100))
    env.ingest.drain_once()
    restarted = BrokerIngest(
        db=env.db,
        journal=env.journal,
        store=env.store,
        account_id="DU123",
        account_mode="paper",
        session_epoch="s2",
        clock=lambda: UTC_NOW,
    )
    restarted.on_order_status(fake_trade(client_order_id=91, perm_id=100, status="Filled", filled=10.0))
    restarted.drain_once()
    assert [event["entity_id"] for event in _events(env.db)] == ["grp1:entry", "grp1:entry"]


def test_failed_order_write_rolls_back_aliases_with_the_journal_event(env, monkeypatch):
    def fail_write(*_args, **_kwargs):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(env.store, "upsert_order_in_tx", fail_write)
    env.ingest.on_open_order(fake_trade(perm_id=777))
    with pytest.raises(RuntimeError, match="disk write failed"):
        env.ingest.drain_once()

    assert _events(env.db) == []
    assert env.db.execute(
        "SELECT COUNT(*) FROM broker_order_aliases", fetch="one"
    ) == (0,)


def test_liquidation_children_classify_by_group_not_parent():
    assert classify_leg("STP", 0, 7, "p-1-reprotect-stop-265598-1") == "stop"
    assert classify_leg("LMT", 0, 8, "p-1-reprotect-target-265598-1") == "take_profit"
    assert classify_leg("MKT", 0, 9, "p-1-reduce-265598-2") == "exit"


def test_a_pre_sp1_liquidation_reduce_ref_is_an_exit():
    """Refs written before SP1 ({root}-liquidation-reduce-{conid}) are exits, not entries."""
    assert liquidation_child_kind("flat-1-liquidation-reduce-265598") == "reduce"
    assert classify_leg("MKT", 0, 9, "flat-1-liquidation-reduce-265598") == "exit"


def test_a_legacy_reduce_prefix_matches_every_conid_of_its_run_only():
    """Ruling 42: {run}-liquidation-reduce-{conid}, any conid; never another run or a new child."""
    prefix = legacy_reduce_prefix("flat-1")
    assert matches_legacy_reduce("flat-1-liquidation-reduce-265598", prefix)
    assert matches_legacy_reduce("flat-1-liquidation-reduce-1", prefix)
    assert not matches_legacy_reduce("flat-10-liquidation-reduce-1", prefix)
    assert not matches_legacy_reduce("flat-1-liquidation-reduce-1-2", prefix)
    assert not matches_legacy_reduce("flat-1-liquidation-reduce-", prefix)
    assert not matches_legacy_reduce(None, prefix)


def test_other_groups_keep_the_parent_rule():
    assert classify_leg("STP", 0, 7, "og-cmd1") == "entry"
    assert classify_leg("STP", 5, 7, "og-cmd1") == "stop"
    assert classify_leg("LMT", 5, 7) == "take_profit"


def test_liquidation_child_ids_are_deterministic_and_colon_free():
    assert liquidation_child_id("root-1", "reprotect-stop", 265598, 1) == "root-1-reprotect-stop-265598-1"
    assert reprotect_oca_group("root-1", 265598, 2) == "root-1-reprotect-265598-2"
    assert liquidation_child_kind("root-1-reduce-265598-3") == "reduce"
    assert liquidation_child_kind("og-root-1") is None
    for bad in (("root:1", "reduce", 1, 1), ("root-1", "entry", 1, 1), ("root-1", "reduce", 1, 0)):
        with pytest.raises(ValueError):
            liquidation_child_id(*bad)
