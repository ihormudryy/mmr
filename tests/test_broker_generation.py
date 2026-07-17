"""[M1-F2] broker snapshot generation staging and readiness barrier."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.duckdb_store import DuckDBConnection
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BROKER_SYNC_SOURCES, BrokerIngest, GenerationIncomplete

NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "generation.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ingest = BrokerIngest(db, journal, store, "DU123", "paper", session_epoch="s1", clock=lambda: NOW)
    return SimpleNamespace(db=db, ingest=ingest)


def _position(conid=265598, qty=10.0):
    return SimpleNamespace(
        account="DU123",
        contract=SimpleNamespace(conId=conid, symbol="AAPL", secType="STK", exchange="NASDAQ", currency="USD"),
        position=qty,
        avgCost=180.0,
    )


def test_generation_stages_callbacks_without_changing_live_journal(env):
    generation = env.ingest.begin_generation()
    env.ingest.on_position(_position())
    assert env.ingest.drain_once() == 1

    assert env.db.execute("SELECT COUNT(*) FROM domain_event_journal", fetch="one") == (0,)
    assert env.db.execute(
        "SELECT generation_id, record_kind FROM broker_sync_staging", fetch="one"
    ) == (generation, "PositionObservation")
    assert env.ingest.is_ready is False


def test_incomplete_generation_cannot_promote(env):
    env.ingest.begin_generation()
    with pytest.raises(GenerationIncomplete, match="positions"):
        env.ingest.promote_generation()


def test_complete_generation_promotes_staged_position_atomically(env):
    generation = env.ingest.begin_generation()
    env.ingest.on_position(_position())
    env.ingest.drain_once()
    for source in BROKER_SYNC_SOURCES:
        env.ingest.mark_source_complete(source)

    cursor = env.ingest.promote_generation()

    assert cursor == 1
    assert env.db.execute("SELECT COUNT(*) FROM broker_positions", fetch="one") == (1,)
    assert env.db.execute("SELECT COUNT(*) FROM domain_event_journal", fetch="one") == (1,)
    assert env.db.execute("SELECT COUNT(*) FROM broker_sync_staging", fetch="one") == (0,)
    assert env.db.execute(
        "SELECT status, promoted_cursor FROM broker_sync_generations WHERE generation_id = ?",
        [generation],
        fetch="one",
    ) == ("promoted", cursor)
    assert env.ingest.is_ready is True


def test_position_absent_from_a_complete_later_generation_is_tombstoned(env):
    env.ingest.begin_generation()
    env.ingest.on_position(_position(conid=265598, qty=10.0))
    env.ingest.on_position(_position(conid=272093, qty=5.0))
    env.ingest.drain_once()
    for source in BROKER_SYNC_SOURCES:
        env.ingest.mark_source_complete(source)
    env.ingest.promote_generation()

    env.ingest.begin_generation()
    env.ingest.on_position(_position(conid=265598, qty=10.0))
    env.ingest.drain_once()
    for source in BROKER_SYNC_SOURCES:
        env.ingest.mark_source_complete(source)
    env.ingest.promote_generation()

    deletes = env.db.execute(
        "SELECT entity_id FROM domain_event_journal WHERE operation = 'delete'", fetch="all"
    )
    assert deletes == [("DU123:272093",)]


def test_abandon_discards_staging_and_leaves_barrier_unready(env):
    env.ingest.begin_generation()
    env.ingest.on_position(_position())
    env.ingest.drain_once()

    env.ingest.abandon_generation("ib disconnected")

    assert env.db.execute("SELECT COUNT(*) FROM broker_sync_staging", fetch="one") == (0,)
    assert env.db.execute(
        "SELECT status, abandon_reason FROM broker_sync_generations", fetch="one"
    ) == ("abandoned", "ib disconnected")
    assert env.ingest.is_ready is False


def test_abandon_without_an_active_generation_is_idempotent(env):
    env.ingest.abandon_generation("late disconnect")

    assert env.ingest.is_ready is False


@pytest.mark.asyncio
async def test_run_broker_sync_marks_all_sources_and_promotes(env):
    class FakeIB:
        def accountValues(self, _account_id):
            # ib.accountValues() is a SYNC snapshot of the already-downloaded
            # account subscription (connectAsync fires reqAccountUpdatesAsync).
            # run_broker_sync reads this instead of re-subscribing (which hangs).
            return [SimpleNamespace(account="DU123", tag="NetLiquidation",
                                    currency="USD", value="100000")]

        async def reqPositionsAsync(self):
            return [_position()]

        async def reqAllOpenOrdersAsync(self):
            return []

        async def reqCompletedOrdersAsync(self, *, apiOnly):
            assert apiOnly is True
            return []

        async def reqExecutionsAsync(self):
            return []

    assert await env.ingest.run_broker_sync(SimpleNamespace(ib=FakeIB())) is True
    assert env.ingest.is_ready is True
    assert env.db.execute("SELECT COUNT(*) FROM broker_positions", fetch="one") == (1,)
    # The account source now stages from the cached accountValues() snapshot.
    assert env.db.execute("SELECT COUNT(*) FROM broker_account_state", fetch="one") == (1,)
