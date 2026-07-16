"""[M1-F3] Task 1 — proposal schema migration + exclusive versioned cutover.

Binding cross-file topology (pre-flight resolution B2): the migrated,
guard-complete `trade_proposals` table lives in the JOURNAL db
(`journal_duckdb_path`), never in-place in the legacy `mmr.duckdb` file the
[S0] `ProposalStore` still points at. `apply_proposal_authority_migration`
relocates existing rows across files via a read-only ATTACH + INSERT +
DETACH and leaves the legacy table's own schema/rows completely untouched
— it only freezes further WRITES to it (reads stay available).

Every fixture/test below is retargeted accordingly: `journal_db`/`migrator`
wrap a SEPARATE tmp file from `legacy_db`/`store`.
"""
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    PROPOSAL_AUTHORITY_MIGRATION_VERSION,
    PROPOSAL_COLUMNS,
    apply_proposal_authority_migration,
)
from trader.data.proposal_store import ProposalStore, ProposalStoreFrozen
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.proposal import TradeProposal

UTC = dt.timezone.utc


@pytest.fixture
def authority(tmp_path):
    """A legacy (mmr.duckdb-style) file backing an [S0] ProposalStore, and
    a SEPARATE journal file backing the [M1-F3] SchemaMigrator — the
    cross-file topology B2 requires. Neither file is shared."""
    legacy_path = str(tmp_path / "trader.duckdb")
    journal_path = str(tmp_path / "journal.duckdb")

    legacy_db = DuckDBConnection.get_instance(legacy_path)
    store = ProposalStore(legacy_path)

    journal_db = DuckDBConnection.get_instance(journal_path)
    migrator = SchemaMigrator(journal_db)

    return SimpleNamespace(
        legacy_db=legacy_db,
        legacy_path=legacy_path,
        store=store,
        journal_db=journal_db,
        migrator=migrator,
    )


def _migrate(fx) -> None:
    apply_proposal_authority_migration(fx.migrator, fx.legacy_path)


def test_migration_copies_parsable_metadata_exactly(authority):
    fx = authority
    pid = fx.store.add(TradeProposal(
        symbol="AAPL", action="BUY", amount=5000.0, source="strategy:orb",
        metadata={"conid": 265598, "expires_at": "2026-07-15T10:00:00+00:00"},
    ))
    _migrate(fx)
    row = fx.journal_db.execute(
        "SELECT conid, expires_at, live_approval_eligible, revision, metadata "
        "FROM trade_proposals WHERE id = ?", [pid], fetch="one")
    assert row[0] == 265598
    assert row[1] == dt.datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert row[2] is False            # legacy rows never gain live guards
    assert row[3] == 1                # every migrated row starts at revision 1
    assert json.loads(row[4])["conid"] == 265598   # original metadata preserved


def test_migration_leaves_invalid_values_null_and_live_ineligible(authority):
    fx = authority
    pid = fx.store.add(TradeProposal(
        symbol="AAPL", action="BUY", source="strategy:orb",
        metadata={"conid": "not-a-number", "expires_at": "2026-07-15T10:30:00"},
    ))  # naive timestamp and non-integer conid
    _migrate(fx)
    row = fx.journal_db.execute(
        "SELECT conid, expires_at, account_id, account_mode, reference_price, "
        "live_approval_eligible FROM trade_proposals WHERE id = ?",
        [pid], fetch="one")
    assert row[0] is None and row[1] is None
    assert row[2] is None and row[3] is None and row[4] is None  # never inferred
    assert row[5] is False


def test_migration_preserves_missing_expiry_marker_for_task2(authority):
    """[S0] semantics preservation: a legacy manual proposal with NO
    expires_at key anywhere in metadata must migrate with expires_at NULL
    *and* no '$.expires_at' key in the preserved metadata blob — this is
    the exact signal [M1-F3] Task 2's expiry sweep needs to keep treating
    it as "valid forever", as distinct from a present-but-unparsable
    expiry (also NULL, but WITH a metadata key) which must fail closed.
    """
    fx = authority
    pid = fx.store.add(TradeProposal(symbol="AAPL", action="BUY", source="manual"))
    _migrate(fx)
    row = fx.journal_db.execute(
        "SELECT expires_at, metadata FROM trade_proposals WHERE id = ?",
        [pid], fetch="one")
    assert row[0] is None
    assert "expires_at" not in json.loads(row[1])


def test_migration_is_idempotent(authority):
    fx = authority
    fx.store.add(TradeProposal(symbol="AAPL", action="BUY"))
    _migrate(fx)
    _migrate(fx)   # must be a no-op
    count = fx.journal_db.execute("SELECT COUNT(*) FROM trade_proposals", fetch="one")
    assert count[0] == 1


def test_migration_registers_version_20_on_journal_and_legacy_ledgers(authority):
    fx = authority
    fx.store.add(TradeProposal(symbol="AAPL", action="BUY"))
    _migrate(fx)
    assert PROPOSAL_AUTHORITY_MIGRATION_VERSION == 20
    assert fx.migrator.applied_versions() == {20}
    legacy_versions = SchemaMigrator(fx.legacy_db).applied_versions()
    assert legacy_versions == {20}


def test_legacy_writers_are_frozen_after_cutover(authority):
    fx = authority
    pid = fx.store.add(TradeProposal(symbol="AAPL", action="BUY"))
    _migrate(fx)
    with pytest.raises(ProposalStoreFrozen):
        fx.store.add(TradeProposal(symbol="MSFT", action="BUY"))
    with pytest.raises(ProposalStoreFrozen):
        fx.store.update_status(pid, "REJECTED")
    with pytest.raises(ProposalStoreFrozen):
        fx.store.try_transition(pid, "PENDING", "REJECTED")
    with pytest.raises(ProposalStoreFrozen):
        fx.store.update_metadata(pid, {"foo": "bar"})
    with pytest.raises(ProposalStoreFrozen):
        fx.store.claim_for_approval(pid, dt.datetime(2026, 7, 15, tzinfo=UTC))
    with pytest.raises(ProposalStoreFrozen):
        fx.store.expire_stale_pending(dt.datetime(2026, 7, 15, tzinfo=UTC))
    with pytest.raises(ProposalStoreFrozen):
        fx.store.delete(pid)
    # Reads stay available during the read-only window.
    assert fx.store.get(pid).symbol == "AAPL"
    assert len(fx.store.query()) == 1


def test_freeze_detected_even_on_a_store_constructed_before_cutover(authority):
    """A ProposalStore instance created (and used) BEFORE the migration ran
    must still detect the freeze on its NEXT write after cutover — the
    `_frozen` cache may only ever latch True, never memoize a stale False.
    """
    fx = authority
    fx.store.add(TradeProposal(symbol="AAPL", action="BUY"))  # pre-cutover write
    assert fx.store._cutover_applied() is False
    _migrate(fx)
    with pytest.raises(ProposalStoreFrozen):
        fx.store.add(TradeProposal(symbol="MSFT", action="BUY"))


def test_explicit_column_list_survives_added_columns(authority):
    fx = authority
    pid = fx.store.add(TradeProposal(
        symbol="BHP", action="BUY", exchange="ASX", currency="AUD", group="mining"))
    _migrate(fx)
    restored = fx.store.get(pid)
    assert (restored.exchange, restored.currency, restored.group) == ("ASX", "AUD", "mining")
    assert len(PROPOSAL_COLUMNS) == 29
