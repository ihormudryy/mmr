"""Public, deterministic research-evidence bundle exports."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import ExperimentFamily, TRIAL_FAILED, TRIAL_SUCCEEDED
from trader.research.attestation import AttestationRepository, build_attestation
from trader.research.canonical import canonical_json_bytes
from trader.research.eligibility import (
    EligibilityDecisionRepository,
    EligibilityEvidence,
    evaluate_eligibility,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import OperatorReview, OperatorReviewRepository
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.research.signing import AttestationSigner

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
def tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def read_canonical_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_text(root: Path) -> str:
    return "".join(p.read_text(encoding="utf-8")
                   for p in sorted(root.rglob("*.json")))


def _evidence() -> EligibilityEvidence:
    return EligibilityEvidence(
        n_round_trips=250, n_instruments=10, expectancy_bps_baseline=5.0,
        expectancy_bps_1_5x=3.0, expectancy_bps_2x=1.0,
        selection_adjusted_confidence=0.97, annualized_sharpe_ci_low=0.5,
        profit_factor=1.5, walk_forward_positive_fraction=0.7,
        max_month_profit_share=0.25, max_instrument_profit_share=0.30,
        scaled_holdout_drawdown=-0.02, neighborhood_robust=True,
        order_within_envelope=True, deterministic_replay_ok=True,
        holdout_opened_once=True, benchmark_drawdown_ratio=0.40,
        eligible_regime_positive_fraction=0.80, worst_eligible_regime_loss=-0.05,
        regime_transitions_stable=True)


def build_populated_db(tmp_path, *, attestation_source_digest="source-1",
                       attestation_config_digest="config-1", validation_folds=None):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    registry = ExperimentRegistry(db)
    family = ExperimentFamily(
        strategy_path="strategies/orb.py", class_name="OpeningRangeBreakout",
        repository_commit="a" * 40, source_tree_digest="source-1",
        dependency_lock_digest="config-1", container_digest="image-1",
        dataset_manifest_digest="dataset-1", search_space={"minutes": [15, 30]},
        cost_model={"slippage_bps": 2.0},
        validation_protocol={"holdout": "2025-01-01/2025-12-31"})
    if validation_folds is None:
        validation_folds = (
            {"kind": "walk_forward", "test": "2024"}, {"kind": "holdout", "test": "2025"})
    registry.create_family(family, created_at=T0, validation_folds=validation_folds)
    selected = registry.start_trial(family.family_id, trial_key="selected",
                                      parameters={"minutes": 30}, started_at=T0)
    registry.finish_trial(selected, status=TRIAL_SUCCEEDED, finished_at=T0,
                          metrics={"sharpe": 2.0})
    failed = registry.start_trial(family.family_id, trial_key="failed",
                                    parameters={"minutes": 15}, started_at=T0)
    registry.finish_trial(failed, status=TRIAL_FAILED, finished_at=T0,
                          safe_summary="zero division")
    registry.set_trial_archived(failed, True)
    artifact_id = registry.seal_artifact(family.family_id, selected_trial_id=selected,
                                         selected_parameters={"minutes": 30}, sealed_at=T0)
    registry.open_holdout(artifact_id, opened_at=T0, passed=True, detail="passed")
    decision = evaluate_eligibility(PAPER_V1, _evidence())
    EligibilityDecisionRepository(db).record(decision, artifact_id=artifact_id, recorded_at=T0)
    review = OperatorReview(
        artifact_id=artifact_id, eligibility_decision_digest=decision.digest,
        reviewer="operator", reviewed_at=T0, economic_rationale="liquidity edge",
        edge_survives_costs="modeled costs", known_failure_regimes="trend days",
        data_and_survivorship_limits="known limits", parameter_sensitivity="plateau",
        operational_dependencies="market data", capacity_and_decay="limited capacity",
        episode_dominance="no dominance", holdout_opened_once_confirmed=True)
    OperatorReviewRepository(db).record(review)
    signer = AttestationSigner.generate()
    unsigned = build_attestation(
        decision=decision, review=review, public_key_id=signer.public_key_id,
        artifact_digest=artifact_id, source_digest=attestation_source_digest,
        config_digest=attestation_config_digest,
        dataset_manifest_digest="dataset-1", allowlist_digest="allowlist-1",
        training_boundary="2020/2023", validation_boundary="2024", holdout_boundary="2025",
        evidence_boundary="2020/2025", cost_assumptions={"slippage_bps": 2.0},
        capacity_assumptions={"capacity_usd": 1_000_000}, max_gross_allocation=0.05,
        permitted_instruments=("SPY",), created_at=T0, expires_at=T0 + dt.timedelta(days=90),
        operator_approved_at=T0)
    AttestationRepository(db).record(signer.sign(unsigned))
    return db, artifact_id


@pytest.fixture
def populated_db(tmp_path):
    return build_populated_db(tmp_path)


def test_export_is_byte_identical_and_read_only(populated_db, tmp_path):
    from trader.research.bundle import ResearchBundle

    db, artifact_id = populated_db
    bundle = ResearchBundle(db)
    first = bundle.export(artifact_id, tmp_path / "first")
    second = bundle.export(artifact_id, tmp_path / "second")

    assert first.manifest_digest == second.manifest_digest
    assert tree_bytes(tmp_path / "first") == tree_bytes(tmp_path / "second")
    assert all(not (p.stat().st_mode & 0o222) for p in (tmp_path / "first").rglob("*"))


def test_export_binds_failed_trial_holdout_cost_stress_and_attestation(populated_db, tmp_path):
    from trader.research.bundle import ResearchBundle

    db, artifact_id = populated_db
    result = ResearchBundle(db).export(artifact_id, tmp_path / "bundle")
    manifest = read_canonical_json(tmp_path / "bundle" / "manifest.json")
    trials = read_canonical_json(tmp_path / "bundle" / "trials.json")
    artifact = read_canonical_json(tmp_path / "bundle" / "artifact.json")
    attestation = read_canonical_json(tmp_path / "bundle" / "attestation.json")

    assert result.manifest_digest == manifest["manifest_digest"]
    assert any(trial["status"] == "FAILED" for trial in trials)
    assert artifact["holdout"]["passed"] is True
    assert attestation["cost_assumptions"]["slippage_bps"] == 2.0
    assert manifest["attestation"]["public_key_id"].startswith("ed25519-")
    assert "private" not in canonical_text(tmp_path / "bundle")


@pytest.mark.parametrize(("field", "value"), [
    ("attestation_source_digest", "other-source"),
    ("attestation_config_digest", "other-config"),
])
def test_export_rejects_attestation_family_digest_mismatch(tmp_path, field, value):
    from trader.research.bundle import BundleError, ResearchBundle

    db, artifact_id = build_populated_db(tmp_path, **{field: value})

    with pytest.raises(BundleError, match="attestation .* binding"):
        ResearchBundle(db).export(artifact_id, tmp_path / "bundle")


def test_export_rejects_missing_validation_folds(tmp_path):
    from trader.research.bundle import BundleError, ResearchBundle

    db, artifact_id = build_populated_db(tmp_path, validation_folds=())

    with pytest.raises(BundleError, match="validation folds"):
        ResearchBundle(db).export(artifact_id, tmp_path / "bundle")


def test_verify_rejects_tampered_or_extra_file(populated_db, tmp_path):
    from trader.research.bundle import BundleError, ResearchBundle

    db, artifact_id = populated_db
    root = tmp_path / "bundle"
    bundle = ResearchBundle(db)
    bundle.export(artifact_id, root)
    (root / "artifact.json").chmod(0o644)
    (root / "artifact.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BundleError, match="checksum"):
        bundle.verify(root)


def test_verify_rejects_forged_self_consistent_payload(populated_db, tmp_path):
    from trader.research.bundle import BundleError, ResearchBundle

    db, artifact_id = populated_db
    root = tmp_path / "bundle"
    bundle = ResearchBundle(db)
    bundle.export(artifact_id, root)
    artifact_path = root / "artifact.json"
    artifact_path.chmod(0o644)
    artifact = read_canonical_json(artifact_path)
    artifact["family_id"] = "forged-family"
    artifact_bytes = canonical_json_bytes(artifact)
    artifact_path.write_bytes(artifact_bytes)
    manifest_path = root / "manifest.json"
    manifest_path.chmod(0o644)
    manifest = read_canonical_json(manifest_path)
    manifest["files"]["artifact.json"] = hashlib.sha256(artifact_bytes).hexdigest()
    manifest_body = dict(manifest)
    manifest_body.pop("manifest_digest")
    manifest["manifest_digest"] = hashlib.sha256(canonical_json_bytes(manifest_body)).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    artifact_path.chmod(0o444)
    manifest_path.chmod(0o444)

    with pytest.raises(BundleError, match="binding"):
        bundle.verify(root)
