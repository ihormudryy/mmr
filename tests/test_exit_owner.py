# tests/test_exit_owner.py
import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import (
    EXIT_OWNER_MIGRATION_VERSION, ExitInProgress, ExitOwnerRegistry, apply_exit_owner_migration,
)

NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=dt.timezone.utc)
ACCOUNT = "DU123"


@pytest.fixture
def db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "owners.duckdb"))
    apply_exit_owner_migration(SchemaMigrator(db))
    return db


@pytest.fixture
def registry(db):
    return ExitOwnerRegistry(db)


def test_migration_35_creates_exit_owners_table(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "m.duckdb"))
    migrator = SchemaMigrator(db)
    assert EXIT_OWNER_MIGRATION_VERSION == 35
    assert apply_exit_owner_migration(migrator) is True
    assert apply_exit_owner_migration(migrator) is False
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 35", fetch="one") == ("sp1_exit_owners",)


def test_first_scoped_claim_is_claimed(registry):
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("c-1", "CLAIMED")
    assert registry.owner_for(ACCOUNT, 1).goal == "zero"


def test_second_full_close_joins_existing_full_owner(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("c-1", "JOINED")
    assert registry.get("c-2") is None


def test_full_close_upgrades_partial_owner_to_zero(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("p-1", "UPGRADED")
    owner = registry.get("p-1")
    assert (owner.goal, owner.goal_quantity) == ("zero", None)


def test_partial_against_any_existing_owner_is_refused(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    with pytest.raises(ExitInProgress) as ex:
        registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-2", goal_quantity=3.0, now=NOW)
    assert ex.value.code == "EXIT_IN_PROGRESS"
    assert ex.value.root_id == "c-1"


def test_other_conid_is_independent(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    assert registry.claim_scoped(account_id=ACCOUNT, conid=2, root_id="c-9", goal_quantity=None, now=NOW).outcome == "CLAIMED"


def test_account_claim_supersedes_scoped_owners_in_one_step(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    registry.claim_scoped(account_id=ACCOUNT, conid=2, root_id="c-2", goal_quantity=None, now=NOW)
    claim = registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    assert (claim.outcome, claim.superseded) == ("CLAIMED", ("c-2", "p-1"))
    assert registry.get("p-1").state == "SUPERSEDED"
    assert registry.owner_for(ACCOUNT, 1) is None
    assert registry.account_owner(ACCOUNT).root_id == "flat-1"


def test_scoped_full_request_joins_active_flatten_and_partial_is_refused(registry):
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("flat-1", "JOINED_FLATTEN")
    with pytest.raises(ExitInProgress):
        registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=2.0, now=NOW)


def test_superseded_owner_is_never_upgraded_or_revived(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW)
    assert claim.root_id == "flat-1"
    assert (registry.get("p-1").state, registry.get("p-1").goal) == ("SUPERSEDED", "partial")


def test_second_account_claim_joins_the_first(registry):
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_account(account_id=ACCOUNT, root_id="flat-2", now=NOW)
    assert (claim.root_id, claim.outcome) == ("flat-1", "JOINED_FLATTEN")
    assert registry.get("flat-2") is None


def test_release_frees_the_slot_and_failed_safe_is_not_an_owner(registry, db):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    registry.release("c-1", NOW)
    assert registry.get("c-1").state == "RELEASED"
    assert registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW).outcome == "CLAIMED"
    db.transaction(lambda conn: registry.finish_in_tx(conn, "c-2", "FAILED_SAFE", NOW))
    assert registry.owner_for(ACCOUNT, 1) is None
    assert registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-3", goal_quantity=None, now=NOW).outcome == "CLAIMED"


def test_a_used_root_id_cannot_be_claimed_again(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    registry.release("c-1", NOW)
    with pytest.raises(ValueError):
        registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)


def test_claim_rolls_back_with_the_callers_transaction(registry, db):
    """R6: a claim made inside a caller's transaction disappears when that transaction fails."""
    class _Boom(Exception):
        pass

    def write(conn):
        registry.claim_scoped_in_tx(conn, account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
        raise _Boom()

    with pytest.raises(_Boom):
        db.transaction(write)
    assert registry.get("c-1") is None


def test_root_ids_with_a_colon_are_refused(registry):
    with pytest.raises(ValueError):
        registry.claim_account(account_id=ACCOUNT, root_id="flat:1", now=NOW)


def test_partial_check_refuses_any_active_owner_and_writes_nothing(registry, db):
    """D15: a partial request learns ExitInProgress before its quantity is admitted."""
    db.transaction(lambda conn: registry.ensure_partial_allowed_in_tx(conn, ACCOUNT, 1))
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    with pytest.raises(ExitInProgress) as ex:
        db.transaction(lambda conn: registry.ensure_partial_allowed_in_tx(conn, ACCOUNT, 1))
    assert ex.value.root_id == "c-1"
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    with pytest.raises(ExitInProgress) as ex:
        db.transaction(lambda conn: registry.ensure_partial_allowed_in_tx(conn, ACCOUNT, 2))
    assert ex.value.root_id == "flat-1"
