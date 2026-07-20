"""Deterministic, public-only research evidence bundles."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from trader.research import signing
from trader.research.artifact import (
    TRIAL_SUCCEEDED, ExperimentFamily, artifact_id as strategy_artifact_id, trial_id,
)
from trader.research.attestation import (
    AttestationRepository, EligibilityAttestation, attestation_payload_bytes,
    payload_digest,
)
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.eligibility import EligibilityDecision, EligibilityDecisionRepository, RuleResult
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

    def verify(self, path: Path, *, trusted_public_keys: Mapping[str, Any] = {}) -> VerifiedResearchBundle:
        """Accept only a complete, checksum-valid public export directory."""
        if not path.is_dir() or path.is_symlink():
            raise BundleError("bundle root must be a real directory")
        manifest_path = path / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise BundleError("bundle manifest is unsafe")
        try:
            raw_manifest = manifest_path.read_bytes()
            manifest = json.loads(raw_manifest)
        except (OSError, ValueError) as exc:
            raise BundleError("invalid bundle manifest") from exc
        if canonical_json_bytes(manifest) != raw_manifest:
            raise BundleError("manifest is not canonical JSON")
        _validate_manifest(manifest)
        if {child.name for child in path.iterdir()} != set(_FILE_NAMES):
            raise BundleError("bundle has unexpected or missing files")
        for name, checksum in manifest["files"].items():
            child = path / name
            if child.is_symlink() or not child.is_file():
                raise BundleError(f"bundle file is unsafe: {name}")
            if _sha256(child.read_bytes()) != checksum:
                raise BundleError(f"checksum mismatch for {name}")
        if path.stat().st_mode & 0o222 or any(
                (path / name).stat().st_mode & 0o222 for name in _FILE_NAMES):
            raise BundleError("bundle is not read-only")
        _validate_payload_bindings(path, manifest, trusted_public_keys)
        digest = manifest["manifest_digest"]
        trace_digests = _parse_trace_digests(manifest.get("trace_digests"))
        return VerifiedResearchBundle(
            manifest_digest=digest, artifact_id=manifest["artifact_id"],
            dataset_manifest_digest=manifest["dataset_manifest_digest"],
            attestation=dict(manifest["attestation"]),
            trace_digests=trace_digests)

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
        if (attestation.source_digest != family.source_tree_digest or
                attestation.config_digest != family.dependency_lock_digest or
                attestation.dataset_manifest_digest != family.dataset_manifest_digest):
            raise BundleError("attestation source, config, or dataset binding disagrees with family")
        folds = self._registry.get_validation_folds(family.family_id)
        if not folds:
            raise BundleError("artifact family has no validation folds")
        trials_digest = _bundle_digest("trials", [_trial_public(t) for t in trials])
        folds_digest = _bundle_digest("folds", folds)
        if (f"bundle_trials:{trials_digest}" not in attestation.evidence_refs or
                f"bundle_folds:{folds_digest}" not in attestation.evidence_refs):
            raise BundleError("attestation trial or validation-fold binding disagrees")

        trace_digests = self._trace_digests_for(artifact.selected_trial_id)
        return {
            "artifact": _artifact_public(artifact, self._holdout_for(artifact_id)),
            "family": _family_public(family),
            "trials": [_trial_public(t) for t in trials],
            "folds": folds,
            "decision": _decision_public(decision),
            "review": _review_public(review),
            "attestation": _attestation_public(attestation),
            "trace_digests": trace_digests,
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

    def _trace_digests_for(self, selected_trial_id: str) -> tuple[str, ...]:
        trial = self._registry.get_trial(selected_trial_id)
        if trial is None:
            raise BundleError("selected trial is missing from registry")
        trace = trial.metrics.get("trace_signature")
        if not isinstance(trace, str) or len(trace) != 64 or not all(
                char in "0123456789abcdef" for char in trace):
            raise BundleError("selected trial is missing a valid trace_signature metric")
        return (trace,)

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
        trials_digest = _bundle_digest("trials", evidence["trials"])
        folds_digest = _bundle_digest("folds", evidence["folds"])
        trace_digests = _parse_trace_digests(evidence["trace_digests"])
        manifest = {
            "format_version": FORMAT_VERSION,
            "artifact_id": evidence["artifact"]["artifact_id"],
            "attestation": {"payload_digest": attestation["payload_digest"],
                            "public_key_id": attestation["public_key_id"]},
            "source_digest": attestation["source_digest"],
            "config_digest": attestation["config_digest"],
            "dataset_manifest_digest": attestation["dataset_manifest_digest"],
            "ruleset_digest": attestation["ruleset_digest"],
            "trials_digest": trials_digest,
            "folds_digest": folds_digest,
            "trace_digests": list(trace_digests),
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


def _parse_trace_digests(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise BundleError("invalid trace digest list")
    digests: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) != 64 or not all(
                char in "0123456789abcdef" for char in item):
            raise BundleError("invalid trace digest entry")
        digests.append(item)
    return tuple(digests)


def _validate_manifest(manifest: Any) -> None:
    required = {"format_version", "artifact_id", "attestation", "source_digest",
                "config_digest", "dataset_manifest_digest", "ruleset_digest", "trials_digest",
                "folds_digest", "trace_digests", "files", "manifest_digest"}
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise BundleError("unsupported bundle format")
    if set(manifest) != required or not isinstance(manifest["artifact_id"], str):
        raise BundleError("invalid bundle manifest")
    attestation = manifest["attestation"]
    if not isinstance(attestation, dict) or set(attestation) != {"payload_digest", "public_key_id"}:
        raise BundleError("invalid manifest attestation")
    digests = ("source_digest", "config_digest", "dataset_manifest_digest",
               "ruleset_digest", "trials_digest", "folds_digest", "manifest_digest")
    if not all(isinstance(manifest[key], str) and manifest[key] for key in digests):
        raise BundleError("invalid manifest digest")
    if not all(isinstance(attestation[key], str) and attestation[key]
               for key in ("payload_digest", "public_key_id")):
        raise BundleError("invalid manifest attestation")
    files = manifest["files"]
    expected = set(_FILE_NAMES) - {"manifest.json"}
    if not isinstance(files, dict) or set(files) != expected or list(files) != sorted(files):
        raise BundleError("invalid manifest file table")
    if not all(isinstance(value, str) and len(value) == 64 and
               all(char in "0123456789abcdef" for char in value)
               for value in files.values()):
        raise BundleError("invalid manifest checksum table")
    _parse_trace_digests(manifest["trace_digests"])
    digest_body = dict(manifest)
    digest = digest_body.pop("manifest_digest")
    if _sha256(canonical_json_bytes(digest_body)) != digest:
        raise BundleError("manifest checksum mismatch")


def _validate_payload_bindings(root: Path, manifest: Mapping[str, Any],
                               trusted_public_keys: Mapping[str, Any]) -> None:
    payloads = {}
    for name in _FILE_NAMES:
        if name == "manifest.json":
            continue
        raw = (root / name).read_bytes()
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BundleError(f"malformed payload: {name}") from exc
        if canonical_json_bytes(payload) != raw:
            raise BundleError(f"noncanonical payload: {name}")
        payloads[name.removesuffix(".json")] = payload

    artifact = _mapping_payload(payloads, "artifact")
    family = _mapping_payload(payloads, "family")
    decision = _mapping_payload(payloads, "decision")
    review = _mapping_payload(payloads, "review")
    attestation = _mapping_payload(payloads, "attestation")
    trials = payloads["trials"]
    folds = payloads["folds"]
    if not isinstance(trials, list) or not isinstance(folds, list) or not folds:
        raise BundleError("invalid trial or validation-fold payload")
    try:
        if artifact["artifact_id"] != manifest["artifact_id"]:
            raise BundleError("artifact binding disagrees with manifest")
        reconstructed_family = ExperimentFamily(
            strategy_path=family["strategy_path"], class_name=family["class_name"],
            repository_commit=family["repository_commit"],
            source_tree_digest=family["source_tree_digest"],
            dependency_lock_digest=family["dependency_lock_digest"],
            container_digest=family["container_digest"],
            dataset_manifest_digest=family["dataset_manifest_digest"],
            search_space=family["search_space"], cost_model=family["cost_model"],
            validation_protocol=family["validation_protocol"], provenance=family["provenance"])
        if reconstructed_family.family_id != family["family_id"]:
            raise BundleError("family digest binding disagrees")
        if artifact["family_id"] != family["family_id"]:
            raise BundleError("artifact family binding disagrees")
        if strategy_artifact_id(artifact["family_id"], artifact["selected_trial_id"],
                                artifact["selected_parameters"], artifact["provenance"]) != \
                artifact["artifact_id"]:
            raise BundleError("artifact digest binding disagrees")
        selected_trials = [trial for trial in trials if isinstance(trial, dict) and
                           trial.get("trial_id") == artifact["selected_trial_id"]]
        if len(selected_trials) != 1:
            raise BundleError("selected trial binding disagrees")
        selected_trial = selected_trials[0]
        if (selected_trial.get("status") != TRIAL_SUCCEEDED or
                selected_trial.get("parameters") != artifact["selected_parameters"]):
            raise BundleError("selected trial state or parameters binding disagrees")
        if any(not isinstance(trial, dict) or trial.get("family_id") != family["family_id"]
               for trial in trials):
            raise BundleError("trial family binding disagrees")
        if any(trial_id(family["family_id"], trial["trial_key"], trial["parameters"])
               != trial["trial_id"] for trial in trials):
            raise BundleError("trial digest binding disagrees")
        if any(not isinstance(fold, dict) for fold in folds):
            raise BundleError("invalid validation-fold payload")
        if (_bundle_digest("trials", trials) != manifest["trials_digest"] or
                _bundle_digest("folds", folds) != manifest["folds_digest"]):
            raise BundleError("trial or validation-fold digest binding disagrees")
        trace_digests = _parse_trace_digests(manifest["trace_digests"])
        trace_metric = selected_trial.get("metrics", {}).get("trace_signature")
        if trace_metric != trace_digests[0] or len(trace_digests) != 1:
            raise BundleError("selected trial trace binding disagrees")
        if review["artifact_id"] != artifact["artifact_id"] or \
                review["eligibility_decision_digest"] != decision["decision_digest"]:
            raise BundleError("review binding disagrees")
        if (attestation["artifact_digest"] != artifact["artifact_id"] or
                attestation["eligibility_decision_digest"] != decision["decision_digest"] or
                attestation["review_digest"] != review["review_digest"] or
                attestation["ruleset_digest"] != decision["ruleset_digest"]):
            raise BundleError("attestation evidence binding disagrees")
        if (attestation["source_digest"] != family["source_tree_digest"] or
                attestation["config_digest"] != family["dependency_lock_digest"] or
                attestation["dataset_manifest_digest"] != family["dataset_manifest_digest"]):
            raise BundleError("attestation family binding disagrees")
        if (manifest["source_digest"] != attestation["source_digest"] or
                manifest["config_digest"] != attestation["config_digest"] or
                manifest["dataset_manifest_digest"] != attestation["dataset_manifest_digest"] or
                manifest["ruleset_digest"] != attestation["ruleset_digest"] or
                manifest["attestation"]["payload_digest"] != attestation["payload_digest"] or
                manifest["attestation"]["public_key_id"] != attestation["public_key_id"]):
            raise BundleError("manifest attestation binding disagrees")
        decision_object = EligibilityDecision(
            state=decision["state"], ruleset_name=decision["ruleset_name"],
            ruleset_version=decision["ruleset_version"], ruleset_digest=decision["ruleset_digest"],
            passed=decision["passed"], results=tuple(
                RuleResult(code=result["code"], passed=result["passed"],
                           observed=result["observed"], threshold=result["threshold"],
                           evidence_ref=result["evidence_ref"], detail=result["detail"])
                for result in decision["results"]))
        if decision_object.digest != decision["decision_digest"]:
            raise BundleError("decision digest binding disagrees")
        if decision["passed"] != all(result["passed"] for result in decision["results"]):
            raise BundleError("decision pass-state binding disagrees")
        review_object = OperatorReview(
            artifact_id=review["artifact_id"],
            eligibility_decision_digest=review["eligibility_decision_digest"],
            reviewer=review["reviewer"], reviewed_at=_parse_datetime(review["reviewed_at"]),
            economic_rationale=review["economic_rationale"],
            edge_survives_costs=review["edge_survives_costs"],
            known_failure_regimes=review["known_failure_regimes"],
            data_and_survivorship_limits=review["data_and_survivorship_limits"],
            parameter_sensitivity=review["parameter_sensitivity"],
            operational_dependencies=review["operational_dependencies"],
            capacity_and_decay=review["capacity_and_decay"],
            episode_dominance=review["episode_dominance"],
            holdout_opened_once_confirmed=review["holdout_opened_once_confirmed"])
        if review_object.digest != review["review_digest"]:
            raise BundleError("review digest binding disagrees")
        attestation_fields = {key: value for key, value in attestation.items()
                              if key != "payload_digest"}
        attestation_object = EligibilityAttestation(
            **{**attestation_fields, "created_at": _parse_datetime(attestation["created_at"]),
               "expires_at": _parse_datetime(attestation["expires_at"]),
               "operator_approved_at": _parse_datetime(attestation["operator_approved_at"]),
               "promoted_at": (_parse_datetime(attestation["promoted_at"])
                               if attestation["promoted_at"] is not None else None)})
        if attestation_object.payload_digest != attestation["payload_digest"]:
            raise BundleError("attestation payload digest binding disagrees")
        _verify_attestation_signature(attestation_object, trusted_public_keys)
        if (f"bundle_trials:{manifest['trials_digest']}" not in attestation_object.evidence_refs or
                f"bundle_folds:{manifest['folds_digest']}" not in attestation_object.evidence_refs):
            raise BundleError("attestation trial or validation-fold binding disagrees")
    except (KeyError, TypeError) as exc:
        raise BundleError("incomplete payload binding") from exc


def _mapping_payload(payloads: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    payload = payloads[name]
    if not isinstance(payload, dict):
        raise BundleError(f"invalid payload: {name}.json")
    return payload


def _bundle_digest(kind: str, value: Any) -> str:
    return sha256_digest(f"research_bundle_{kind}", value)


def _parse_datetime(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise BundleError("invalid timestamp payload")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BundleError("timestamp payload must be timezone-aware")
    return parsed


def _verify_attestation_signature(attestation: EligibilityAttestation,
                                  trusted_public_keys: Mapping[str, Any]) -> None:
    if not isinstance(trusted_public_keys, Mapping):
        raise BundleError("trusted public keys must be an explicit mapping")
    public_key = trusted_public_keys.get(attestation.public_key_id)
    if public_key is None:
        raise BundleError("attestation public key is not trusted")
    try:
        if signing.public_key_id(public_key) != attestation.public_key_id:
            raise BundleError("trusted public-key mapping disagrees with key identity")
    except BundleError:
        raise
    except Exception as exc:
        raise BundleError("invalid trusted public-key mapping") from exc
    try:
        signing.verify_bytes(public_key, attestation_payload_bytes(attestation),
                             attestation.signature)
    except signing.BadSignature as exc:
        raise BundleError("attestation signature does not verify") from exc


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
