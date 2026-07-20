"""Shared paper automation keygen + fixture PAPER_ELIGIBLE bundle export."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
import tempfile
from pathlib import Path

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import ExperimentFamily, TRIAL_FAILED, TRIAL_SUCCEEDED
from trader.research.attestation import AttestationRepository, build_attestation
from trader.research.bundle import ResearchBundle
from trader.research.canonical import sha256_digest
from trader.research.eligibility import (
    EligibilityDecisionRepository,
    EligibilityEvidence,
    evaluate_eligibility,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import OperatorReview, OperatorReviewRepository
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.research.signing import (
    AttestationSigner,
    generate_private_key_pem,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def default_key_paths(config_dir: Path) -> tuple[Path, Path, Path]:
    """Return ``(private_pem, verify_dir, public_pem)`` under *config_dir*."""
    verify_dir = config_dir / "keys" / "verify"
    private_pem = config_dir / "keys" / "private" / "signing.pem"
    public_pem = verify_dir / "paper-automation.pem"
    return private_pem, verify_dir, public_pem


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
        regime_transitions_stable=True,
    )


def _write_private_key(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"refusing to overwrite existing private key {path}; pass --force"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = generate_private_key_pem()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"private key permissions too open: {oct(mode)}")


def _write_public_key(signer: AttestationSigner, path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"refusing to overwrite existing public key {path}; pass --force"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(signer.public_key_pem())
    os.chmod(path, 0o644)


def ensure_signing_keypair(
    *,
    private_key_path: Path,
    public_key_path: Path,
    force: bool = False,
) -> tuple[AttestationSigner, bool]:
    """Ensure Ed25519 PKCS8 signing keypair exists; return ``(signer, reused)``."""
    if (
        private_key_path.exists()
        and public_key_path.exists()
        and not force
    ):
        return AttestationSigner.from_key_file(str(private_key_path)), True

    _write_private_key(private_key_path, force=force)
    signer = AttestationSigner.from_key_file(str(private_key_path))
    _write_public_key(signer, public_key_path, force=force)
    return signer, False


def export_fixture_paper_eligible_bundle(
    *,
    signer: AttestationSigner,
    artifacts_root: Path,
) -> str:
    """Build a fixture PAPER_ELIGIBLE research DB and export the signed bundle."""
    with tempfile.TemporaryDirectory(prefix="mmr-bootstrap-research-") as tmp:
        db = DuckDBConnection.get_instance(str(Path(tmp) / "research.duckdb"))
        apply_research_migrations(SchemaMigrator(db))
        registry = ExperimentRegistry(db)
        family = ExperimentFamily(
            strategy_path="strategies/orb.py",
            class_name="OpeningRangeBreakout",
            repository_commit="a" * 40,
            source_tree_digest="source-1",
            dependency_lock_digest="config-1",
            container_digest="image-1",
            dataset_manifest_digest="dataset-1",
            search_space={"minutes": [15, 30]},
            cost_model={"slippage_bps": 2.0},
            validation_protocol={"holdout": "2025-01-01/2025-12-31"},
        )
        validation_folds = (
            {"kind": "walk_forward", "test": "2024"},
            {"kind": "holdout", "test": "2025"},
        )
        registry.create_family(family, created_at=T0, validation_folds=validation_folds)
        selected = registry.start_trial(
            family.family_id, trial_key="selected",
            parameters={"minutes": 30}, started_at=T0,
        )
        registry.finish_trial(
            selected, status=TRIAL_SUCCEEDED, finished_at=T0,
            metrics={"sharpe": 2.0, "trace_signature": "a" * 64},
        )
        failed = registry.start_trial(
            family.family_id, trial_key="failed",
            parameters={"minutes": 15}, started_at=T0,
        )
        registry.finish_trial(
            failed, status=TRIAL_FAILED, finished_at=T0,
            safe_summary="zero division",
        )
        registry.set_trial_archived(failed, True)
        artifact_id = registry.seal_artifact(
            family.family_id, selected_trial_id=selected,
            selected_parameters={"minutes": 30}, sealed_at=T0,
        )
        registry.open_holdout(artifact_id, opened_at=T0, passed=True, detail="passed")
        decision = evaluate_eligibility(PAPER_V1, _evidence())
        EligibilityDecisionRepository(db).record(
            decision, artifact_id=artifact_id, recorded_at=T0,
        )
        review = OperatorReview(
            artifact_id=artifact_id,
            eligibility_decision_digest=decision.digest,
            reviewer="bootstrap",
            reviewed_at=T0,
            economic_rationale="fixture liquidity edge",
            edge_survives_costs="modeled costs",
            known_failure_regimes="trend days",
            data_and_survivorship_limits="fixture limits",
            parameter_sensitivity="plateau",
            operational_dependencies="market data",
            capacity_and_decay="limited capacity",
            episode_dominance="no dominance",
            holdout_opened_once_confirmed=True,
        )
        OperatorReviewRepository(db).record(review)
        trials = registry.list_trials(family.family_id, include_archived=True)
        trial_payload = [
            {
                "trial_id": t.trial_id, "family_id": t.family_id,
                "trial_key": t.trial_key, "parameters": dict(t.parameters),
                "status": t.status, "started_at": t.started_at,
                "finished_at": t.finished_at, "metrics": dict(t.metrics),
                "traceback_digest": t.traceback_digest,
                "safe_summary": t.safe_summary, "archived": t.archived,
            }
            for t in trials
        ]
        trial_digest = sha256_digest("research_bundle_trials", trial_payload)
        folds_digest = sha256_digest("research_bundle_folds", list(validation_folds))
        unsigned = build_attestation(
            decision=decision,
            review=review,
            public_key_id=signer.public_key_id,
            artifact_digest=artifact_id,
            source_digest="source-1",
            config_digest="config-1",
            dataset_manifest_digest="dataset-1",
            allowlist_digest="allowlist-1",
            training_boundary="2020/2023",
            validation_boundary="2024",
            holdout_boundary="2025",
            evidence_boundary="2020/2025",
            cost_assumptions={"slippage_bps": 2.0},
            capacity_assumptions={"capacity_usd": 1_000_000},
            max_gross_allocation=0.05,
            permitted_instruments=("SPY",),
            created_at=T0,
            expires_at=T0 + dt.timedelta(days=90),
            operator_approved_at=T0,
            evidence_refs=(
                tuple(decision.evidence_refs)
                + ((f"bundle_trials:{trial_digest}", f"bundle_folds:{folds_digest}"))
            ),
        )
        AttestationRepository(db).record(signer.sign(unsigned))

        artifacts_root.mkdir(parents=True, exist_ok=True)
        export_dir = artifacts_root / artifact_id
        if export_dir.exists():
            raise SystemExit(
                f"artifact already exists at {export_dir}; remove it or choose a fresh key run"
            )
        try:
            ResearchBundle(db).export(artifact_id, export_dir)
        except Exception:
            shutil.rmtree(export_dir, ignore_errors=True)
            raise
        return artifact_id
