"""SP2c Plan 2 Task 1: sealed deployment versions (spec 5.2 item 5) and withdrawals."""
from __future__ import annotations

import datetime as dt

import pytest

from trader.automation.ai_deployment_versions import (
    INITIAL, RENEWAL, AiDeploymentVersionStore, DeploymentVersion, apply_ai_deployment_version_migrations,
    version_digest,
)
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.ai_paper_decision import apply_ai_paper_decision_migration
from trader.automation.protective_order_saga import apply_protective_order_saga_migration
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=dt.timezone.utc)
BASE = "sha256:" + "a" * 64


def version(judgment_id="jdg-1", kind=INITIAL, prior=None, first=dt.date(2026, 10, 12), expiry=dt.date(2026, 11, 6)):
    return DeploymentVersion(base_digest=BASE, judgment_id=judgment_id, kind=kind, prior_version=prior,
                             first_session=first, expiry_session=expiry)


@pytest.fixture
def store(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    journal = DomainJournal(db)
    journal.migrate(SchemaMigrator(db))
    migrator = SchemaMigrator(db)
    apply_protective_order_saga_migration(migrator)     # withdraw reads the entries being sent
    apply_ai_paper_decision_migration(migrator)
    apply_ai_deployment_version_migrations(migrator)
    return AiDeploymentVersionStore(journal, now=lambda: NOW)


def seal(store, v, request="sha256:" + "1" * 64):
    return store._db.transaction(lambda conn: store.seal_in_tx(conn, v, request_digest=request,
                                                                principal="ai_research", command_id="c"))


def test_version_digest_has_its_own_domain_and_is_rechecked_on_read(store):
    digest, created = seal(store, version())
    assert created and digest == version_digest(version()) and store.get(digest) == version()
    store._db.execute("UPDATE ai_deployment_versions SET record_json = replace(record_json, '2026-11-06', "
                      "'2026-12-31')")
    with pytest.raises(DeploymentRefused) as refused:
        store.get(digest)
    assert refused.value.code == "DEPLOYMENT_VERSION_TAMPERED"


@pytest.mark.parametrize("column,value", [("judgment_id", "jdg-x"), ("base_digest", "sha256:" + "b" * 64),
                                          ("prior_version", "sha256:" + "c" * 64)])
def test_a_plain_column_that_differs_from_the_sealed_record_is_tampered(store, column, value):
    digest, _ = seal(store, version())
    store._db.execute(f"UPDATE ai_deployment_versions SET {column} = ?", [value])
    judgment_id = value if column == "judgment_id" else "jdg-1"
    for read in (lambda: store.get(digest), lambda: store.bound_to_judgment(judgment_id), store.sealed,
                 lambda: store._db.transaction(lambda conn: store.bound_in_tx(conn, judgment_id))):
        with pytest.raises(DeploymentRefused) as refused:
            read()
        assert refused.value.code == "DEPLOYMENT_VERSION_TAMPERED"


def test_one_version_per_judgment(store):
    first, _ = seal(store, version())
    again, created = seal(store, version(first=dt.date(2026, 10, 13)))     # same request, a later day
    assert (again, created) == (first, False)
    with pytest.raises(DeploymentRefused) as refused:
        seal(store, version(first=dt.date(2026, 10, 13)), request="sha256:" + "2" * 64)
    assert refused.value.code == "JUDGMENT_ALREADY_BOUND"
    assert store.version_for_judgment("jdg-1") == first and store.version_for_judgment("jdg-x") is None
    other, created = seal(store, version("jdg-2"), request="sha256:" + "3" * 64)      # NULL priors coexist
    assert created and other != first


def test_a_renewal_gets_a_fresh_digest_and_a_prior_is_renewed_once(store):
    prior, _ = seal(store, version())
    renewal, _ = seal(store, version("jdg-2", RENEWAL, prior, dt.date(2026, 11, 9), dt.date(2026, 12, 7)))
    assert renewal != prior
    with pytest.raises(DeploymentRefused) as refused:
        seal(store, version("jdg-3", RENEWAL, prior, dt.date(2026, 11, 9), dt.date(2026, 12, 7)))
    assert refused.value.code == "RENEWAL_PRIOR_INVALID"


@pytest.mark.parametrize("change", [{"kind": "OTHER"}, {"kind": RENEWAL}, {"prior_version": BASE},
                                    {"expiry_session": dt.date(2026, 10, 1)},
                                    {"first_session": dt.datetime(2026, 10, 12)}, {"base_digest": "a" * 64},
                                    {"judgment_id": "has space"}, {"binding_verified_by_bundle": False}])
def test_bad_versions_are_refused(change):
    fields = {**version().__dict__, **change}
    with pytest.raises(DeploymentRefused):
        DeploymentVersion(**fields)


def test_withdraw_is_idempotent_and_needs_a_known_version(store):
    digest, _ = seal(store, version())
    assert store.withdraw(digest, reason="operator", principal="cli", command_id="w1") is True
    assert store.withdraw(digest, reason="again", principal="cli", command_id="w2") is False
    assert store.withdrawn() == frozenset({digest})
    with pytest.raises(DeploymentRefused) as refused:
        store.withdraw("sha256:" + "f" * 64, reason="x", principal="cli", command_id="w3")
    assert refused.value.code == "DEPLOYMENT_VERSION_UNKNOWN"
