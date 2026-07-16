"""Tests for DomainJournal ([M1-F1] Task 3) -- the linchpin atomic-append
primitive every downstream broker/command producer builds on.

FROZEN CONTRACTS under test:
- `DomainJournal(db)` constructor (no extra required args).
- `.migrate(migrator)` bootstraps schema_migrations, domain_event_journal,
  domain_snapshot_checkpoints, and the materialized-entity ledger
  (versions 1-3, inside this plan's owned range 1-9).
- `.mutate(conn, mutation, write_materialized) -> DomainEvent`, with
  `write_materialized(conn, entity_revision: int) -> None` invoked inside
  the same transaction as the journal insert.
- `.read_after(after_cursor, limit)` / `.get_entity(entity_type, entity_id)`
  test-support APIs.
- `EventIdentityConflict` on a same-event_id retry with different fields.

`source_cursor` is monotonic-but-SPARSE (nextval() burns values on
rollback) -- every assertion below on cursor values checks ORDERING or a
strict inequality, never an absolute integer.
"""
import datetime as dt
from dataclasses import replace

import pytest

from trader.data.domain_journal import DomainJournal, EventIdentityConflict
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation

UTC_NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)

POSITION_MUTATION = DomainMutation(
    event_type="position.updated",
    entity_type="position",
    entity_id="DU123:265598",
    operation="upsert",
    account_id="DU123",
    source="trader_service",
    source_timestamp=UTC_NOW,
    correlation_id=None,
    payload={"quantity": 10, "average_cost": 180.0},
)


class _DomainStore:
    """Thin test harness driving DomainJournal the way a real producer
    would: obtain a conn, supply a write_materialized callback that writes
    the caller's own domain-specific row (here, a scratch table standing
    in for a future rich `broker_positions`-style table), and optionally
    crash after that write to exercise the atomicity guarantee.
    """

    def __init__(self, journal: DomainJournal):
        self.journal = journal

    def apply(self, mutation: DomainMutation, event_id: str | None = None, crash_after: str | None = None):
        conn = self.journal.connect()

        def write_materialized(conn_, revision):
            conn_.execute(
                "INSERT INTO test_position_writes VALUES (?, ?)",
                [mutation.entity_id, revision],
            )
            if crash_after == "materialized":
                raise RuntimeError("crash after materialized write")

        return self.journal.mutate(conn, mutation, write_materialized, event_id=event_id)

    def get_entity(self, entity_type, entity_id):
        return self.journal.get_entity(entity_type, entity_id)


@pytest.fixture
def domain_store(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    # Scratch table representing a caller's own rich materialized write
    # (e.g. [M1-F2]'s broker_positions), created up front (outside any
    # mutate() transaction) so write_materialized can INSERT into it.
    db.execute("CREATE TABLE test_position_writes (entity_id VARCHAR, revision BIGINT)")
    return _DomainStore(journal)


# --------------------------------------------------------------------- #
# Brief's verbatim crash-point + idempotency tests
# --------------------------------------------------------------------- #

def test_materialized_write_and_event_are_atomic(domain_store):
    with pytest.raises(RuntimeError, match="after materialized"):
        domain_store.apply(
            POSITION_MUTATION,
            crash_after="materialized",
        )
    assert domain_store.get_entity("position", "DU123:265598") is None
    assert domain_store.journal.read_after(0, 100) == []


def test_same_event_id_is_idempotent(domain_store):
    first = domain_store.apply(POSITION_MUTATION, event_id="event-1")
    second = domain_store.apply(POSITION_MUTATION, event_id="event-1")
    assert first.source_cursor == second.source_cursor
    assert len(domain_store.journal.read_after(0, 100)) == 1


# --------------------------------------------------------------------- #
# Additional coverage
# --------------------------------------------------------------------- #

def test_crash_after_materialized_also_rolls_back_callers_own_write(domain_store, tmp_duckdb_path):
    # Stronger atomicity check than the brief's minimum: the caller's OWN
    # write_materialized side effect (the scratch table insert) must also
    # not survive -- proving the callback truly ran inside the same
    # transaction as the journal/ledger writes, not a separately-committed
    # step.
    with pytest.raises(RuntimeError, match="after materialized"):
        domain_store.apply(POSITION_MUTATION, crash_after="materialized")

    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    assert db.execute("SELECT * FROM test_position_writes", fetch="all") == []


def test_mutate_returns_a_domain_event_with_revision_one_on_first_write(domain_store):
    event = domain_store.apply(POSITION_MUTATION, event_id="e1")

    assert event.event_id == "e1"
    assert event.entity_revision == 1
    assert event.entity_type == "position"
    assert event.entity_id == "DU123:265598"
    assert event.operation == "upsert"
    assert event.payload == {"quantity": 10, "average_cost": 180.0}


def test_revision_increments_across_distinct_events_for_same_entity(domain_store):
    first = domain_store.apply(POSITION_MUTATION, event_id="e1")
    second_mutation = replace(POSITION_MUTATION, payload={"quantity": 20, "average_cost": 182.0})
    second = domain_store.apply(second_mutation, event_id="e2")

    assert first.entity_revision == 1
    assert second.entity_revision == 2
    assert second.source_cursor > first.source_cursor

    entity = domain_store.get_entity("position", "DU123:265598")
    assert entity["entity_revision"] == 2
    assert entity["payload"] == {"quantity": 20, "average_cost": 182.0}


def test_conflicting_event_id_raises_and_mutates_nothing(domain_store):
    domain_store.apply(POSITION_MUTATION, event_id="dup")
    conflicting = replace(POSITION_MUTATION, payload={"quantity": 999, "average_cost": 1.0})

    with pytest.raises(EventIdentityConflict):
        domain_store.apply(conflicting, event_id="dup")

    # Nothing from the conflicting attempt landed: still exactly one
    # journal row, and the ledger still reflects the original write.
    assert len(domain_store.journal.read_after(0, 100)) == 1
    entity = domain_store.get_entity("position", "DU123:265598")
    assert entity["entity_revision"] == 1
    assert entity["payload"] == {"quantity": 10, "average_cost": 180.0}


def test_delete_tombstones_entity_and_continues_revision_stream(domain_store):
    domain_store.apply(POSITION_MUTATION, event_id="e1")
    delete_mutation = replace(POSITION_MUTATION, operation="delete", payload=None)

    deleted_event = domain_store.apply(delete_mutation, event_id="e2")

    assert deleted_event.entity_revision == 2  # continues, does not reset to 1
    assert deleted_event.payload is None
    assert domain_store.get_entity("position", "DU123:265598") is None
    assert len(domain_store.journal.read_after(0, 100)) == 2


def test_read_after_orders_by_cursor_and_respects_limit(domain_store):
    for i in range(3):
        mutation = replace(
            POSITION_MUTATION,
            entity_id=f"DU123:{i}",
            payload={"quantity": i},
        )
        domain_store.apply(mutation, event_id=f"e{i}")

    all_events = domain_store.journal.read_after(0, 100)
    assert [e.entity_id for e in all_events] == ["DU123:0", "DU123:1", "DU123:2"]
    assert [e.source_cursor for e in all_events] == sorted(e.source_cursor for e in all_events)

    limited = domain_store.journal.read_after(0, 2)
    assert len(limited) == 2

    after_first = domain_store.journal.read_after(all_events[0].source_cursor, 100)
    assert [e.entity_id for e in after_first] == ["DU123:1", "DU123:2"]


def test_source_cursor_is_sparse_after_a_rolled_back_write(domain_store):
    # RA-4: nextval() is only consumed by the INSERT itself, so a crash
    # that aborts BEFORE the insert (crash_after="materialized") burns no
    # cursor value -- but a crash that happens after the insert has run
    # (still inside the same transaction, before COMMIT) does burn one.
    # Exercise that directly against the schema's own sequence semantics,
    # independent of mutate()'s specific crash-injection point, to prove
    # the schema genuinely tolerates gaps (never assert an absolute
    # cursor value elsewhere in this file).
    conn = domain_store.journal.connect()
    conn.execute("BEGIN TRANSACTION")
    conn.execute(
        "INSERT INTO domain_event_journal "
        "(event_id, entity_revision, event_type, entity_type, entity_id, "
        " operation, account_id, source, source_timestamp, received_timestamp, "
        " correlation_id, payload) VALUES "
        "('aborted', 1, 't', 'x', 'y', 'upsert', NULL, 's', ?, ?, NULL, NULL)",
        [UTC_NOW, UTC_NOW],
    )
    conn.execute("ROLLBACK")

    survivor = domain_store.apply(POSITION_MUTATION, event_id="e1")

    # The aborted insert's nextval() call was burned; the surviving row's
    # cursor is strictly greater than 1 (there is a gap), but we assert
    # only the relative property, never a specific integer.
    assert survivor.source_cursor > 1


def test_migrate_is_idempotent(domain_store, tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)

    # Re-running migrate() (e.g. on service restart) must not raise and
    # must not duplicate the ledger entries.
    domain_store.journal.migrate(migrator)

    versions = migrator.applied_versions()
    assert {1, 2, 3} <= versions


def test_migrate_creates_all_three_tables(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)

    journal.migrate(migrator)

    tables = {
        row[0]
        for row in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")
    }
    assert {
        "schema_migrations",
        "domain_event_journal",
        "domain_snapshot_checkpoints",
        "domain_materialized_entities",
    } <= tables
