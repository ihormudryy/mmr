"""[M1-F2] durable risk and reconciliation producer contracts."""
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.risk_producer import (
    ReconciliationProducer,
    RiskDecisionImmutable,
    RiskProducer,
)


UTC_NOW = dt.datetime(2026, 7, 16, 13, 0, tzinfo=dt.timezone.utc)


class FakeTimer:
    instances: list = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.cancelled = interval, fn, False
        type(self).instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def env(tmp_path):
    FakeTimer.instances = []
    db = DuckDBConnection.get_instance(str(tmp_path / "risk.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    projection = {"gross_exposure_pct": 42.0}
    producer = RiskProducer(
        db=db,
        journal=journal,
        account_id="DU123",
        compute_projection=lambda: dict(projection),
        timer_factory=FakeTimer,
        clock=lambda: UTC_NOW,
    )
    producer.migrate(migrator)
    return SimpleNamespace(db=db, journal=journal, producer=producer, projection=projection)


def _events(db):
    rows = db.execute(
        "SELECT event_type, entity_id, entity_revision, payload "
        "FROM domain_event_journal ORDER BY source_cursor",
        fetch="all",
    )
    return [
        {
            "event_type": row[0],
            "entity_id": row[1],
            "entity_revision": row[2],
            "payload": json.loads(row[3]) if row[3] else None,
        }
        for row in rows
    ]


def test_projection_is_debounced_to_at_most_100ms(env):
    env.producer.mark_projection_dirty()
    env.producer.mark_projection_dirty()

    assert len(FakeTimer.instances) == 1
    assert FakeTimer.instances[0].interval <= 0.1
    assert _events(env.db) == []
    FakeTimer.instances[0].fn()

    events = _events(env.db)
    assert [event["entity_id"] for event in events] == ["projection:DU123"]
    assert events[0]["event_type"] == "risk.updated"


def test_unchanged_projection_is_not_rejournaled(env):
    env.producer.mark_projection_dirty()
    FakeTimer.instances[-1].fn()
    env.producer.mark_projection_dirty()
    FakeTimer.instances[-1].fn()

    assert len(_events(env.db)) == 1


def test_namespaces_have_independent_revision_streams(env):
    env.producer.publish_policy("default", {"max_position_pct": 10.0})
    env.producer.publish_decision("cmd-1", {"result": "pass"})
    env.producer.mark_projection_dirty()
    FakeTimer.instances[-1].fn()
    env.producer.publish_policy("default", {"max_position_pct": 12.0})

    by_entity = {}
    for event in _events(env.db):
        by_entity.setdefault(event["entity_id"], []).append(event["entity_revision"])
    assert by_entity == {
        "policy:default": [1, 2],
        "decision:cmd-1": [1],
        "projection:DU123": [1],
    }


def test_decision_is_write_once(env):
    env.producer.publish_decision("cmd-1", {"result": "pass"})

    assert env.producer.publish_decision("cmd-1", {"result": "pass"}) is None
    with pytest.raises(RiskDecisionImmutable):
        env.producer.publish_decision("cmd-1", {"result": "fail"})


def test_reconciliation_run_is_journaled(env):
    recon = ReconciliationProducer(db=env.db, journal=env.journal)
    recon.migrate(SchemaMigrator(env.db))

    recon.publish_run(
        run_id="run-1",
        trigger="startup",
        source_cursor=17,
        discrepancies=[{"kind": "orphan_order", "order_id": 9}],
        resolutions=[],
        started_at=UTC_NOW,
        completed_at=UTC_NOW,
    )

    event = _events(env.db)[-1]
    assert event["event_type"] == "reconciliation.updated"
    assert event["payload"]["discrepancies"][0]["kind"] == "orphan_order"
