"""[M1-F3] Task 1 — clean proposal-authority schema bootstrap.

Production is a fresh deployment: the proposal authority starts directly in
the trader-service-owned journal database.  There is deliberately no read,
write, attach, or freeze path for the retired legacy proposal schema.
"""
import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    PROPOSAL_AUTHORITY_MIGRATION_VERSION,
    PROPOSAL_COLUMNS,
    ProposalRecord,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator


@pytest.fixture
def authority(tmp_path):
    journal_path = tmp_path / "journal.duckdb"
    db = DuckDBConnection.get_instance(str(journal_path))
    return journal_path, db, SchemaMigrator(db)


def test_fresh_bootstrap_creates_guarded_journal_table_only(authority):
    journal_path, db, migrator = authority

    apply_proposal_authority_migration(migrator)

    names = [row[1] for row in db.execute(
        "PRAGMA table_info('trade_proposals')", fetch="all"
    )]
    assert names == list(PROPOSAL_COLUMNS)
    assert journal_path.exists()


def test_fresh_bootstrap_assigns_ids_and_safe_guard_defaults(authority):
    _, db, migrator = authority
    apply_proposal_authority_migration(migrator)

    row = db.execute(
        "INSERT INTO trade_proposals (symbol, action, created_at, updated_at) "
        "VALUES ('AAPL', 'BUY', ?, ?) "
        "RETURNING id, live_approval_eligible, revision, status",
        [dt.datetime(2026, 7, 16, 12, 0), dt.datetime(2026, 7, 16, 12, 0)],
        fetch="one",
    )

    assert row == (1, False, 1, "PENDING")


def test_fresh_bootstrap_is_idempotent_and_records_journal_version(authority):
    _, db, migrator = authority

    apply_proposal_authority_migration(migrator)
    apply_proposal_authority_migration(migrator)

    assert migrator.applied_versions() == {PROPOSAL_AUTHORITY_MIGRATION_VERSION}
    assert db.execute("SELECT COUNT(*) FROM trade_proposals", fetch="one") == (0,)


def test_proposal_record_decodes_the_explicit_column_contract(authority):
    _, db, migrator = authority
    apply_proposal_authority_migration(migrator)
    now = dt.datetime(2026, 7, 16, 12, 0)
    db.execute(
        "INSERT INTO trade_proposals (symbol, action, metadata, execution, order_ids, "
        "created_at, updated_at, account_id, conid) "
        "VALUES ('AAPL', 'BUY', '{\"source\": \"fresh\"}', '{\"type\": \"MKT\"}', "
        "'[42]', ?, ?, 'DU123', 265598)",
        [now, now],
    )

    row = db.execute(
        f"SELECT {', '.join(PROPOSAL_COLUMNS)} FROM trade_proposals WHERE id = 1",
        fetch="one",
    )
    record = ProposalRecord.from_row(row)
    assert record.id == 1
    assert record.metadata == {"source": "fresh"}
    assert record.execution == {"type": "MKT"}
    assert record.order_ids == [42]
    assert record.account_id == "DU123" and record.conid == 265598
