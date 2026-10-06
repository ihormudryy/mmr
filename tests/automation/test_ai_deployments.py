"""Plan 3 Task 5: sealed ai deployment records."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from pathlib import Path

import pytest

from trader.automation.ai_deployments import (
    STRATEGY_DIGEST_PROVENANCE, AiDeployment, AiDeploymentStore, DeploymentRefused,
    apply_ai_deployment_migration, deployment_digest,
)
from trader.automation.bundle_finder import NoEligibleBundle, find_eligible_bundle
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import canonical_json_bytes

NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc)
GOOD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
        "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [272093, 265598],
        "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
        "evidence_ref": "trial:42", "evidence_order_notional": 2000.0}


@pytest.fixture
def db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_ai_deployment_migration(SchemaMigrator(db))
    return db


@pytest.fixture
def store(db):
    return AiDeploymentStore(db, now=lambda: NOW)


def register(store, body=GOOD, command_id="c1"):
    return store.register(AiDeployment.from_json(body), principal="ai_research", command_id=command_id)


def test_register_seals_and_returns_a_stable_digest(store):
    digest, created = register(store)
    assert created and digest.startswith("sha256:")
    again = register(store, {**GOOD, "conids": [265598, 272093]}, "c2")
    assert again == (digest, False)                                  # same content, any conid order
    assert store.get_sealed(digest).conids == (265598, 272093)
    assert store.get_sealed(digest) == AiDeployment.from_json(GOOD)


def test_digest_is_domain_separated_from_a_plain_hash():
    d = AiDeployment.from_json(GOOD)
    assert deployment_digest(d) != "sha256:" + hashlib.sha256(canonical_json_bytes(d.to_json())).hexdigest()
    assert deployment_digest(d) == "sha256:" + hashlib.sha256(
        b"mmr.ai-deployment.v1\x00" + canonical_json_bytes(d.to_json())).hexdigest()


def test_unknown_digest_is_not_sealed(store):
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_NOT_SEALED"):
        store.get_sealed("sha256:" + "b" * 64)


def test_edited_row_is_tampered(store, db):
    digest, _ = register(store)
    db.execute("UPDATE ai_deployments SET record_json = replace(record_json, '2000.0', '9000.0')")
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_TAMPERED"):
        store.get_sealed(digest)


def test_unparsable_row_is_tampered(store, db):
    digest, _ = register(store)
    db.execute("UPDATE ai_deployments SET record_json = '{\"conids\": true}'")
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_TAMPERED"):
        store.get_sealed(digest)


@pytest.mark.parametrize("key,value", [
    ("conids", [True]), ("conids", [1.0]), ("conids", ["265598"]), ("conids", []), ("conids", [5, 5]),
    ("conids", [-1]), ("conids", list(range(1, 22))), ("strategy_path", "../etc/x.py"),
    ("strategy_path", "/abs/x.py"), ("strategy_path", "strategies/x.txt"),
    ("strategy_digest", "sha256:XYZ"), ("class_name", "1Bad"), ("bar_size", "7 mins"),
    ("style", "swing_long"), ("decider_verdict", "deploy"), ("decider", "Jev Model"),
    ("evidence_order_notional", 0), ("evidence_order_notional", True), ("evidence_order_notional", math.nan),
    ("params", {"a": math.inf}), ("params", {1: "x"}), ("params", []), ("params", {"a": [[1]]}),
    ("params", {"a": {"b": 1}}), ("evidence_ref", ""), ("evidence_ref", "x" * 257),
    ("evidence_ref", "a\nb")])
def test_strict_validation(key, value):
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_INVALID"):
        AiDeployment.from_json({**GOOD, key: value})


def test_missing_or_extra_key_is_invalid():
    for bad in ({k: v for k, v in GOOD.items() if k != "evidence_order_notional"}, {**GOOD, "x": 1}, [], None):
        with pytest.raises(DeploymentRefused, match="DEPLOYMENT_INVALID"):
            AiDeployment.from_json(bad)


def test_the_constructor_checks_types_too():
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_INVALID"):
        AiDeployment(**{**GOOD, "conids": (True,)})


def test_strategy_digest_is_marked_as_a_claim(store, db):                          # R19, owner answer
    digest, _ = register(store)
    assert store.provenance(digest) == STRATEGY_DIGEST_PROVENANCE == "CLAIMED_NOT_VERIFIED"
    assert db.execute("SELECT strategy_digest_provenance FROM ai_deployments", fetch="one") == (
        "CLAIMED_NOT_VERIFIED",)


def test_store_has_no_update_or_delete():
    assert not [m for m in dir(AiDeploymentStore) if m.startswith(("update", "delete", "seal", "unseal"))]


def test_a_deployment_digest_cannot_arm_the_old_path(tmp_path, store):
    # Activate only reads signed bundles under artifacts/sha256_*; a deployment digest names no bundle.
    digest, _ = register(store)
    (tmp_path / digest.replace(":", "_")).mkdir()
    with pytest.raises(NoEligibleBundle):
        find_eligible_bundle(artifacts_root=tmp_path, verifier=None, strategy={},
                             strategy_file=Path("strategies/opening_range_breakout.py"), now=NOW)
