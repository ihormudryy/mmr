"""Deterministic, public-only research evidence bundles."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from trader.research.attestation import AttestationRepository, EligibilityAttestation
from trader.research.canonical import canonical_json_bytes
from trader.research.eligibility import EligibilityDecision, EligibilityDecisionRepository
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import OperatorReview, OperatorReviewRepository


FORMAT_VERSION = 1
_FILE_NAMES = (
    "artifact.json", "family.json", "trials.json", "folds.json", "decision.json",
    "review.json", "attestation.json", "manifest.json",
)


class BundleError(Exception):
    """The requested evidence chain cannot be exported as a public bundle."""


@dataclass(frozen=True)
class BundleDigest:
    manifest_digest: str
    path: Path


@dataclass(frozen=True)
class VerifiedResearchBundle:
    """A verified public bundle projection (populated by ``verify``)."""

    manifest_digest: str
    artifact_id: str
    dataset_manifest_digest: str
    attestation: Mapping[str, Any]
    trace_digests: tuple[str, ...] = ()


class ResearchBundle:
    def __init__(self, db: Any):
        self._db = db
        self._registry = ExperimentRegistry(db)
        self._decisions = EligibilityDecisionRepository(db)
        self._reviews = OperatorReviewRepository(db)
        self._attestations = AttestationRepository(db)

    def export(self, artifact_id: str, path: Path) -> BundleDigest:
        """Export one complete, sealed evidence chain to a new read-only directory."""
        if path.exists():
            raise BundleError(f"bundle destination already exists: {path}")
        evidence = self._load_public_evidence(artifact_id)
        return self._write_staged(path, self._canonical_files(evidence))

    def verify(self, path: Path) -> VerifiedResearchBundle:
        """Task 1's format reader; strict checksum/path validation follows in Task 2."""
        manifest_path = path / "manifest.json"
        try:
            import json
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BundleError("invalid bundle manifest") from exc
        if manifest.get("format_version") != FORMAT_VERSION:
            raise BundleError("unsupported bundle format")
        digest = manifest.get("manifest_digest")
        if not isinstance(digest, str):
            raise BundleError("invalid bundle manifest digest")
        return VerifiedResearchBundle(
            manifest_digest=digest, artifact_id=manifest["artifact_id"],
            dataset_manifest_digest=manifest["dataset_manifest_digest"],
            attestation=dict(manifest["attestation"]))

    def _load_public_evidence(self, artifact_id: str) -> dict[str, Any]:
        artifact = self._registry.get_artifact(artifact_id)
        if artifact is None:
            raise BundleError(f"missing artifact {artifact_id!r}")
        family = self._registry.get_family(artifact.family_id)
        if family is None:
            raise BundleError(f"missing family {artifact.family_id!r}")
        trials = self._registry.list_trials(family.family_id, include_archived=True)
        if not trials:
            raise BundleError("artifact family has no trials")
        if not any(t.trial_id == artifact.selected_trial_id for t in trials):
            raise BundleError("selected trial is absent from artifact family")
        if not artifact.holdout_opened:
            raise BundleError("artifact has no holdout record")

        decision_digest = self._decision_digest_for(artifact_id)
        decision = self._decisions.get(decision_digest)
        if decision is None or decision.digest != decision_digest:
            raise BundleError("missing or corrupt eligibility decision")
        review_digest = self._review_digest_for(artifact_id, decision_digest)
        review = self._reviews.get(review_digest)
        if review is None or review.digest != review_digest:
            raise BundleError("missing or corrupt operator review")
        attestation_digest = self._attestation_digest_for(decision_digest, review_digest)
        attestation = self._attestations.get(attestation_digest)
        if attestation is None or attestation.payload_digest != attestation_digest:
            raise BundleError("missing or corrupt attestation")

        if review.artifact_id != artifact_id or review.eligibility_decision_digest != decision_digest:
            raise BundleError("review binding disagrees with artifact or decision")
        if (attestation.artifact_digest != artifact_id or
                attestation.eligibility_decision_digest != decision_digest or
                attestation.review_digest != review_digest):
            raise BundleError("attestation binding disagrees with evidence chain")
        if attestation.dataset_manifest_digest != family.dataset_manifest_digest:
            raise BundleError("attestation dataset binding disagrees with family")

        return {
            "artifact": _artifact_public(artifact, self._holdout_for(artifact_id)),
            "family": _family_public(family),
            "trials": [_trial_public(t) for t in trials],
            "folds": self._registry.get_validation_folds(family.family_id),
            "decision": _decision_public(decision),
            "review": _review_public(review),
            "attestation": _attestation_public(attestation),
        }

    def _decision_digest_for(self, artifact_id: str) -> str:
        def query(conn):
            rows = conn.execute(
                "SELECT decision_digest FROM eligibility_decisions WHERE artifact_id = ? "
                "ORDER BY recorded_at, decision_digest", [artifact_id]).fetchall()
            return rows
        rows = self._db.transaction(query)
        if len(rows) != 1:
            raise BundleError("artifact must have exactly one eligibility decision")
        return rows[0][0]

    def _review_digest_for(self, artifact_id: str, decision_digest: str) -> str:
        def query(conn):
            return conn.execute(
                "SELECT review_digest FROM operator_reviews WHERE artifact_id = ? "
                "AND eligibility_decision_digest = ? ORDER BY review_digest",
                [artifact_id, decision_digest]).fetchall()
        rows = self._db.transaction(query)
        if len(rows) != 1:
            raise BundleError("decision must have exactly one operator review")
        return rows[0][0]

    def _attestation_digest_for(self, decision_digest: str, review_digest: str) -> str:
        def query(conn):
            return conn.execute(
                "SELECT payload_digest FROM eligibility_attestations "
                "WHERE eligibility_decision_digest = ? AND review_digest = ? "
                "ORDER BY payload_digest", [decision_digest, review_digest]).fetchall()
        rows = self._db.transaction(query)
        if len(rows) != 1:
            raise BundleError("review must have exactly one signed attestation")
        return rows[0][0]

    def _holdout_for(self, artifact_id: str) -> dict[str, Any]:
        def query(conn):
            return conn.execute(
                "SELECT opened_at, passed, detail FROM holdout_access_log WHERE artifact_id = ?",
                [artifact_id]).fetchone()
        row = self._db.transaction(query)
        if row is None:
            raise BundleError("missing holdout record")
        return {"opened_at": row[0], "passed": bool(row[1]), "detail": row[2]}

    def _canonical_files(self, evidence: Mapping[str, Any]) -> dict[str, bytes]:
        files = {f"{name}.json": canonical_json_bytes(evidence[name])
                 for name in ("artifact", "family", "trials", "folds", "decision", "review", "attestation")}
        checksums = {name: _sha256(data) for name, data in sorted(files.items())}
        attestation = evidence["attestation"]
        manifest = {
            "format_version": FORMAT_VERSION,
            "artifact_id": evidence["artifact"]["artifact_id"],
            "attestation": {"payload_digest": attestation["payload_digest"],
                            "public_key_id": attestation["public_key_id"]},
            "source_digest": attestation["source_digest"],
            "config_digest": attestation["config_digest"],
            "dataset_manifest_digest": attestation["dataset_manifest_digest"],
            "ruleset_digest": attestation["ruleset_digest"],
            "files": checksums,
        }
        manifest["manifest_digest"] = _sha256(canonical_json_bytes(manifest))
        files["manifest.json"] = canonical_json_bytes(manifest)
        return files

    def _write_staged(self, path: Path, files: Mapping[str, bytes]) -> BundleDigest:
        if set(files) != set(_FILE_NAMES):
            raise BundleError("internal bundle file set is invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{path.name}.staging-", dir=path.parent))
        try:
            for name in sorted(files):
                target = staging / name
                with target.open("wb") as handle:
                    handle.write(files[name])
                    handle.flush()
                    os.fsync(handle.fileno())
            self._validate_staged_checksums(staging)
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.replace(staging, path)
            _chmod_read_only(path)
            return BundleDigest(
                manifest_digest=_manifest_digest(path / "manifest.json"), path=path)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    @staticmethod
    def _validate_staged_checksums(staging: Path) -> None:
        import json
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        for name, checksum in manifest["files"].items():
            if _sha256((staging / name).read_bytes()) != checksum:
                raise BundleError(f"staged checksum mismatch for {name}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_digest(path: Path) -> str:
    import json
    return json.loads(path.read_text(encoding="utf-8"))["manifest_digest"]


def _chmod_read_only(root: Path) -> None:
    for child in root.iterdir():
        child.chmod(0o444)
    root.chmod(0o555)


def _artifact_public(artifact: Any, holdout: Mapping[str, Any]) -> dict[str, Any]:
    return {"artifact_id": artifact.artifact_id, "family_id": artifact.family_id,
            "selected_trial_id": artifact.selected_trial_id,
            "selected_parameters": dict(artifact.selected_parameters),
            "provenance": artifact.provenance, "state": artifact.state,
            "sealed_at": artifact.sealed_at, "holdout": dict(holdout)}


def _family_public(family: Any) -> dict[str, Any]:
    return {"family_id": family.family_id, "strategy_path": family.strategy_path,
            "class_name": family.class_name, "repository_commit": family.repository_commit,
            "source_tree_digest": family.source_tree_digest,
            "dependency_lock_digest": family.dependency_lock_digest,
            "container_digest": family.container_digest,
            "dataset_manifest_digest": family.dataset_manifest_digest,
            "search_space": dict(family.search_space), "cost_model": dict(family.cost_model),
            "validation_protocol": dict(family.validation_protocol), "provenance": family.provenance}


def _trial_public(trial: Any) -> dict[str, Any]:
    return {"trial_id": trial.trial_id, "family_id": trial.family_id, "trial_key": trial.trial_key,
            "parameters": dict(trial.parameters), "status": trial.status,
            "started_at": trial.started_at, "finished_at": trial.finished_at,
            "metrics": dict(trial.metrics), "traceback_digest": trial.traceback_digest,
            "safe_summary": trial.safe_summary, "archived": trial.archived}


def _decision_public(decision: EligibilityDecision) -> dict[str, Any]:
    return {"decision_digest": decision.digest, "state": decision.state,
            "ruleset_name": decision.ruleset_name, "ruleset_version": decision.ruleset_version,
            "ruleset_digest": decision.ruleset_digest, "passed": decision.passed,
            "results": [{"code": r.code, "passed": r.passed, "observed": r.observed,
                         "threshold": r.threshold, "evidence_ref": r.evidence_ref,
                         "detail": r.detail} for r in decision.results]}


def _review_public(review: OperatorReview) -> dict[str, Any]:
    return {"review_digest": review.digest, "artifact_id": review.artifact_id,
            "eligibility_decision_digest": review.eligibility_decision_digest,
            "reviewer": review.reviewer, "reviewed_at": review.reviewed_at,
            "economic_rationale": review.economic_rationale,
            "edge_survives_costs": review.edge_survives_costs,
            "known_failure_regimes": review.known_failure_regimes,
            "data_and_survivorship_limits": review.data_and_survivorship_limits,
            "parameter_sensitivity": review.parameter_sensitivity,
            "operational_dependencies": review.operational_dependencies,
            "capacity_and_decay": review.capacity_and_decay,
            "episode_dominance": review.episode_dominance,
            "holdout_opened_once_confirmed": review.holdout_opened_once_confirmed}


def _attestation_public(attestation: EligibilityAttestation) -> dict[str, Any]:
    return {"payload_digest": attestation.payload_digest,
            "artifact_digest": attestation.artifact_digest, "source_digest": attestation.source_digest,
            "config_digest": attestation.config_digest,
            "dataset_manifest_digest": attestation.dataset_manifest_digest,
            "allowlist_digest": attestation.allowlist_digest,
            "training_boundary": attestation.training_boundary,
            "validation_boundary": attestation.validation_boundary,
            "holdout_boundary": attestation.holdout_boundary,
            "evidence_boundary": attestation.evidence_boundary,
            "cost_assumptions": dict(attestation.cost_assumptions),
            "capacity_assumptions": dict(attestation.capacity_assumptions),
            "ruleset_name": attestation.ruleset_name, "ruleset_version": attestation.ruleset_version,
            "ruleset_digest": attestation.ruleset_digest,
            "eligibility_state": attestation.eligibility_state,
            "permitted_account_mode": attestation.permitted_account_mode,
            "max_gross_allocation": attestation.max_gross_allocation,
            "permitted_instruments": list(attestation.permitted_instruments),
            "created_at": attestation.created_at, "expires_at": attestation.expires_at,
            "operator_approved_at": attestation.operator_approved_at,
            "promoted_at": attestation.promoted_at, "reason_codes": list(attestation.reason_codes),
            "evidence_refs": list(attestation.evidence_refs),
            "eligibility_decision_digest": attestation.eligibility_decision_digest,
            "review_digest": attestation.review_digest, "public_key_id": attestation.public_key_id,
            "signature": attestation.signature}
