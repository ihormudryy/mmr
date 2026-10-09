"""Tests only: a signed research bundle whose reviewer is a judgment, as an AI deployment registers it."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.research.evaluation_fixtures import FIXED_NOW, evaluate_synthetic, holdout_ruleset
from trader.research.attest_export import attest_and_export
from trader.research.review import OperatorReview, OperatorReviewRepository
from trader.research.signing import AttestationSigner


@dataclass(frozen=True)
class JudgedBundle:
    bundle_path: Path
    bundle_digest: str
    artifact_id: str
    verify_dir: Path
    research_db: object
    spec: object


def export_judged_bundle(repo: Path, duckdb_path: str, *, judgment_id: str,
                         model_id: str = "openrouter/jev") -> JudgedBundle:
    evaluation = evaluate_synthetic(repo, duckdb_path)
    result = evaluation.result
    OperatorReviewRepository(evaluation.research_db).record(OperatorReview(
        artifact_id=result.artifact_id, eligibility_decision_digest=result.decision_digest,
        reviewer=f"{model_id}#{judgment_id}", reviewed_at=FIXED_NOW,
        economic_rationale="time of day drift", edge_survives_costs="modeled",
        known_failure_regimes="flat days", data_and_survivorship_limits="synthetic",
        parameter_sensitivity="neighbours", operational_dependencies="none",
        capacity_and_decay="small", episode_dominance="none",
        holdout_opened_once_confirmed=True, reviewer_kind="llm"))
    signer = AttestationSigner.generate()
    bundle_path = attest_and_export(evaluation.research_db, artifact_id=result.artifact_id, signer=signer,
                                    artifacts_root=repo / "artifacts", now=FIXED_NOW,
                                    ruleset=holdout_ruleset())
    verify_dir = repo / "verify"
    verify_dir.mkdir(exist_ok=True)
    (verify_dir / "research.pem").write_bytes(signer.public_key_pem())
    return JudgedBundle(bundle_path, "sha256:" + bundle_path.name.removeprefix("sha256_"),
                        result.artifact_id, verify_dir, evaluation.research_db, evaluation.spec)
