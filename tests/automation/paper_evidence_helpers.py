"""Synthetic TEST-ONLY signed research records; never performance claims."""
import datetime as dt
from dataclasses import asdict

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import ExperimentFamily, TRIAL_SUCCEEDED
from trader.research.attestation import AttestationRepository, build_attestation
from trader.research.bundle import ResearchBundle
from trader.research.canonical import sha256_digest
from trader.research.eligibility import EligibilityDecisionRepository, EligibilityEvidence, evaluate_eligibility
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import OperatorReview, OperatorReviewRepository
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2026, 7, 20, 12, tzinfo=dt.timezone.utc)


def research_bundle(tmp_path, *, reviewer="test-operator", evidence_kind=None, decision=None):
    db = DuckDBConnection.get_instance(str(tmp_path / "test-research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    registry = ExperimentRegistry(db)
    protocol = {"holdout": "2025"}
    if evidence_kind is not None:
        protocol["evidence_kind"] = evidence_kind
    family = ExperimentFamily(
        strategy_path="strategies/orb.py", class_name="OpeningRangeBreakout",
        repository_commit="a" * 40, source_tree_digest="test-source",
        dependency_lock_digest="test-lock", container_digest="test-image",
        dataset_manifest_digest="test-dataset", search_space={"minutes": [30]},
        cost_model={"slippage_bps": 2.0}, validation_protocol=protocol,
    )
    folds = ({"kind": "walk_forward", "test": "2024"}, {"kind": "holdout", "test": "2025"})
    registry.create_family(family, created_at=NOW, validation_folds=folds)
    trial_id = registry.start_trial(family.family_id, trial_key="selected", parameters={"minutes": 30}, started_at=NOW)
    registry.finish_trial(trial_id, status=TRIAL_SUCCEEDED, finished_at=NOW, metrics={"trace_signature": "b" * 64})
    artifact_id = registry.seal_artifact(family.family_id, selected_trial_id=trial_id, selected_parameters={"minutes": 30}, sealed_at=NOW)
    registry.open_holdout(artifact_id, opened_at=NOW, passed=True, detail="test-only holdout")
    if decision is None:
        decision = evaluate_eligibility(PAPER_V1, EligibilityEvidence(
            n_round_trips=250, n_instruments=10, expectancy_bps_baseline=5.0,
            expectancy_bps_1_5x=3.0, expectancy_bps_2x=1.0,
            selection_adjusted_confidence=0.97, annualized_sharpe_ci_low=0.5,
            profit_factor=1.5, walk_forward_positive_fraction=0.7,
            max_month_profit_share=0.25, max_instrument_profit_share=0.30,
            scaled_holdout_drawdown=-0.02, neighborhood_robust=True,
            order_within_envelope=True, deterministic_replay_ok=True,
            holdout_opened_once=True, benchmark_drawdown_ratio=0.40,
            eligible_regime_positive_fraction=0.80, worst_eligible_regime_loss=-0.05,
            regime_transitions_stable=True,
        ))
    EligibilityDecisionRepository(db).record(decision, artifact_id=artifact_id, recorded_at=NOW)
    review = OperatorReview(
        artifact_id=artifact_id, eligibility_decision_digest=decision.digest,
        reviewer=reviewer, reviewed_at=NOW, economic_rationale="test edge",
        edge_survives_costs="test costs", known_failure_regimes="test regimes",
        data_and_survivorship_limits="synthetic test records only", parameter_sensitivity="test sensitivity",
        operational_dependencies="test dependencies", capacity_and_decay="test capacity",
        episode_dominance="test concentration", holdout_opened_once_confirmed=True,
    )
    OperatorReviewRepository(db).record(review)
    trials_digest = sha256_digest("research_bundle_trials", [asdict(t) for t in registry.list_trials(family.family_id, include_archived=True)])
    folds_digest = sha256_digest("research_bundle_folds", list(folds))
    signer = AttestationSigner.generate()
    unsigned = build_attestation(
        decision=decision, review=review, public_key_id=signer.public_key_id,
        artifact_digest=artifact_id, source_digest=family.source_tree_digest,
        config_digest=family.dependency_lock_digest, dataset_manifest_digest=family.dataset_manifest_digest,
        allowlist_digest="test-allowlist", training_boundary="2023", validation_boundary="2024",
        holdout_boundary="2025", evidence_boundary="2023/2025", cost_assumptions=family.cost_model,
        capacity_assumptions={"capacity_usd": 1000}, max_gross_allocation=0.05,
        permitted_instruments=("SPY",), created_at=NOW, expires_at=NOW + dt.timedelta(days=90),
        operator_approved_at=NOW,
        evidence_refs=tuple(decision.evidence_refs) + (f"bundle_trials:{trials_digest}", f"bundle_folds:{folds_digest}"),
    )
    AttestationRepository(db).record(signer.sign(unsigned))
    bundle_path = tmp_path / "existing-research" / artifact_id
    ResearchBundle(db).export(artifact_id, bundle_path)
    key_ring = tmp_path / "public-keys"
    key_ring.mkdir()
    (key_ring / "research.pem").write_bytes(signer.public_key_pem())
    return bundle_path, key_ring
