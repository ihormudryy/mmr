"""Tests for DomainSnapshotService ([M1-F1] Task 4) -- the fenced
materialized snapshot every long-poll baseline (Task 5) and dashboard
initial-state load (M1-R) installs as its starting point.

FROZEN CONTRACTS under test:
- `MaterializedAdapter` protocol: `entity_type`, `select_active(conn)`,
  `checkpoint(conn)` ([M1-F2]'s `broker_materialized_adapters` targets these
  exact names).
- `DomainSnapshotService.snapshot_with_cursor() -> SnapshotWithCursor`
  (`source_cursor`, `broker_generation`, `entities`, per `trader/domain/events.py`).

Broker-generation gate (BLOCKER-2, binding): `[M1-F1]` does not promote
broker generations ([M1-F2] does), so `snapshot_with_cursor` always returns
`broker_generation=0` here and never raises `SnapshotNotReady` -- the class
is defined for `[M1-F2]` to activate later. The fence test below therefore
uses a non-broker `proposal` entity (needs no generation), matching the
brief's pre-flight resolution.
"""
import datetime as dt

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.materialized_state import GenericEntityAdapter, MaterializedAdapter
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.snapshot_service import DomainSnapshotService, SnapshotNotReady

UTC_NOW = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)


class _Writer:
    """Test harness: writes `proposal` entities straight through
    `DomainJournal.mutate`. No dedicated materialized table of its own --
    `write_materialized` is a documented no-op because Task 3's own generic
    `domain_materialized_entities` ledger (read by `GenericEntityAdapter`)
    is all a bare `proposal` entity needs.
    """

    def __init__(self, journal: DomainJournal):
        self.journal = journal

    def proposal(self, quantity: int, event_id: str):
        conn = self.journal.connect()
        mutation = DomainMutation(
            event_type="proposal.updated",
            entity_type="proposal",
            entity_id="1",
            operation="upsert",
            account_id=None,
            source="test",
            source_timestamp=UTC_NOW,
            correlation_id=None,
            payload={"quantity": quantity},
        )
        return self.journal.mutate(conn, mutation, lambda _conn, _rev: None, event_id=event_id)

    def delete_proposal(self, event_id: str):
        conn = self.journal.connect()
        mutation = DomainMutation(
            event_type="proposal.updated",
            entity_type="proposal",
            entity_id="1",
            operation="delete",
            account_id=None,
            source="test",
            source_timestamp=UTC_NOW,
            correlation_id=None,
            payload=None,
        )
        return self.journal.mutate(conn, mutation, lambda _conn, _rev: None, event_id=event_id)


@pytest.fixture
def journal(tmp_duckdb_path):
    db = DuckDBConnection.get_instance(tmp_duckdb_path)
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    return j


@pytest.fixture
def writer(journal):
    return _Writer(journal)


@pytest.fixture
def snapshot_service(journal):
    service = DomainSnapshotService(journal)
    service.register_adapter(GenericEntityAdapter("proposal"))
    return service


# --------------------------------------------------------------------- #
# Brief's verbatim interleaving fence test
# --------------------------------------------------------------------- #

def test_snapshot_cursor_covers_exact_returned_revisions(snapshot_service, writer):
    first = writer.proposal(quantity=10, event_id="p1")
    snapshot = snapshot_service.snapshot_with_cursor(
        on_read_started=lambda: writer.proposal(quantity=20, event_id="p2")
    )
    proposal = snapshot.entities["proposal"][0]
    assert proposal["quantity"] == 10
    # Relative/observed, not an absolute journal-derived value (RA-4:
    # cursors are monotonic but SPARSE -- source_cursor burns values on
    # rollback) -- mirrors the convention in test_domain_retention.py /
    # test_domain_journal.py.
    assert snapshot.source_cursor == first.source_cursor
    assert snapshot.broker_generation == 0  # gate dormant in F1; F2 activates it


# --------------------------------------------------------------------- #
# Additional coverage
# --------------------------------------------------------------------- #

def test_the_independent_writer_genuinely_commits_during_the_open_read(snapshot_service, writer, journal):
    # Stronger than the brief's minimum: prove the injected writer's commit
    # actually landed durably (revision 2 in the ledger) -- otherwise the
    # fence test above could pass trivially because the writer never ran at
    # all, which would prove nothing about MVCC fencing.
    writer.proposal(quantity=10, event_id="p1")
    snapshot_service.snapshot_with_cursor(
        on_read_started=lambda: writer.proposal(quantity=20, event_id="p2")
    )
    entity = journal.get_entity("proposal", "1")
    assert entity["entity_revision"] == 2
    assert entity["payload"] == {"quantity": 20}


def test_a_fresh_read_after_the_writer_commits_sees_the_new_revision(snapshot_service, writer):
    # Confirms the earlier snapshot's staleness was a property of THAT one
    # transaction, not a broken/cached read path -- a brand new
    # snapshot_with_cursor() call issued after the writer's commit sees the
    # new revision.
    writer.proposal(quantity=10, event_id="p1")
    writer.proposal(quantity=20, event_id="p2")
    snapshot = snapshot_service.snapshot_with_cursor()
    proposal = snapshot.entities["proposal"][0]
    assert proposal["quantity"] == 20
    assert proposal["entity_revision"] == 2


def test_entity_rows_retain_entity_id_and_entity_revision(snapshot_service, writer):
    writer.proposal(quantity=10, event_id="p1")
    snapshot = snapshot_service.snapshot_with_cursor()
    proposal = snapshot.entities["proposal"][0]
    assert proposal["entity_id"] == "1"
    assert proposal["entity_revision"] == 1


def test_empty_journal_returns_cursor_zero_and_empty_entities(snapshot_service):
    snapshot = snapshot_service.snapshot_with_cursor()
    assert snapshot.source_cursor == 0
    assert snapshot.broker_generation == 0
    assert snapshot.entities == {"proposal": []}


def test_broker_generation_defaults_to_zero_when_checkpoints_table_is_empty(snapshot_service, writer):
    # domain_snapshot_checkpoints is created by Task 3's migrate() but
    # nothing in [M1-F1] ever writes a row to it (Task 6/[M1-F2] do).
    writer.proposal(quantity=10, event_id="p1")
    snapshot = snapshot_service.snapshot_with_cursor()
    assert snapshot.broker_generation == 0


def test_snapshot_not_ready_is_defined_but_never_raised_in_f1(snapshot_service, writer):
    # BLOCKER-2: the class exists now for [M1-F2] to raise once it wires the
    # broker-generation reader, but nothing reachable in [M1-F1] triggers it.
    assert issubclass(SnapshotNotReady, RuntimeError)
    writer.proposal(quantity=10, event_id="p1")
    snapshot = snapshot_service.snapshot_with_cursor()  # must not raise
    assert snapshot.broker_generation == 0


def test_registered_broker_generation_reader_gates_until_promotion(snapshot_service):
    snapshot_service.register_broker_generation_reader(lambda _conn: None)

    with pytest.raises(SnapshotNotReady, match="no complete broker-sync generation"):
        snapshot_service.snapshot_with_cursor()


def test_registered_broker_generation_reader_is_returned_in_snapshot(snapshot_service):
    snapshot_service.register_broker_generation_reader(lambda _conn: 17)

    assert snapshot_service.snapshot_with_cursor().broker_generation == 17


def test_materialized_adapter_protocol_matches_generic_entity_adapter():
    adapter = GenericEntityAdapter("proposal")
    assert isinstance(adapter, MaterializedAdapter)
    assert adapter.entity_type == "proposal"


def test_deleted_proposal_is_excluded_from_active_snapshot(snapshot_service, writer):
    writer.proposal(quantity=10, event_id="p1")
    writer.delete_proposal(event_id="p2")

    snapshot = snapshot_service.snapshot_with_cursor()
    assert snapshot.entities["proposal"] == []


def test_multiple_registered_adapters_each_get_their_own_entity_type_key(journal, writer):
    service = DomainSnapshotService(journal)
    service.register_adapter(GenericEntityAdapter("proposal"))
    service.register_adapter(GenericEntityAdapter("other_entity"))
    writer.proposal(quantity=10, event_id="p1")

    snapshot = service.snapshot_with_cursor()
    assert list(snapshot.entities["proposal"][0].keys()).__contains__("quantity")
    assert snapshot.entities["other_entity"] == []
