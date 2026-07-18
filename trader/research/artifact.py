"""Experiment-family + artifact identity for the research registry (P2 Task 4).

An ``ExperimentFamily`` is the full, frozen identity of a search: the exact code
(repository commit + source-tree digest), the exact environment (dependency-lock
+ container digests), the exact input data (sealed dataset-manifest digest), the
declared parameter search space, the cost model, and the validation protocol.
Its ``family_id`` is a content digest over all of that, so two families are the
same identity iff every one of those inputs matches -- there is no way to quietly
change the code or the data and reuse a family's evidence.

A ``StrategyArtifact`` is one selected, sealed strategy version drawn from a
family. Its ``artifact_id`` digests the family + the selected trial + the
selected parameters.

Provenance is load-bearing: ``LEGACY_UNQUALIFIED`` families (imported historical
backtests) are recorded for the statistical denominator but can never seal an
artifact or open a holdout -- they lack the qualified inputs eligibility
requires, and the registry refuses to pretend otherwise.

This module is OFFLINE-ONLY: it imports only the canonical-digest helpers and
nothing operational.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from trader.research.canonical import canonical_json_bytes, sha256_digest

FAMILY_DIGEST_PREFIX = "experiment_family"
ARTIFACT_DIGEST_PREFIX = "strategy_artifact"
TRIAL_DIGEST_PREFIX = "experiment_trial"

# Provenance -- whether a family/artifact can ever earn eligibility.
PROVENANCE_RESEARCH = "RESEARCH"
PROVENANCE_LEGACY_UNQUALIFIED = "LEGACY_UNQUALIFIED"
PROVENANCES = frozenset({PROVENANCE_RESEARCH, PROVENANCE_LEGACY_UNQUALIFIED})

# Trial lifecycle. RUNNING is the only non-terminal state; the four terminal
# states are permanent and all count toward the selection denominator.
TRIAL_RUNNING = "RUNNING"
TRIAL_SUCCEEDED = "SUCCEEDED"
TRIAL_FAILED = "FAILED"
TRIAL_INVALID = "INVALID"
TRIAL_TIMED_OUT = "TIMED_OUT"
TERMINAL_TRIAL_STATUSES = frozenset(
    {TRIAL_SUCCEEDED, TRIAL_FAILED, TRIAL_INVALID, TRIAL_TIMED_OUT})
TRIAL_STATUSES = TERMINAL_TRIAL_STATUSES | {TRIAL_RUNNING}

# Artifact lifecycle owned by Task 4. A failed holdout is a one-way trip to
# RETIRED; downstream eligibility states (PAPER_ELIGIBLE, ...) belong to the
# eligibility service (Task 6) and are not settable here.
ARTIFACT_STATE_CANDIDATE = "CANDIDATE"
ARTIFACT_STATE_RETIRED = "RETIRED"


def _require_nonblank(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"ExperimentFamily.{name} must be a non-empty string")


def _require_canonical(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"ExperimentFamily.{name} must be a mapping")
    # Fail loudly NOW on anything that can't canonicalize (NaN, naive datetime,
    # unsupported types) rather than forking the digest chain later.
    canonical_json_bytes(dict(value))
    return dict(value)


@dataclass(frozen=True)
class ExperimentFamily:
    """The complete, frozen identity of one parameter-search family."""

    strategy_path: str
    class_name: str
    repository_commit: str
    source_tree_digest: str
    dependency_lock_digest: str
    container_digest: str
    dataset_manifest_digest: str
    search_space: Mapping[str, Any]
    cost_model: Mapping[str, Any]
    validation_protocol: Mapping[str, Any]
    provenance: str = PROVENANCE_RESEARCH

    def __post_init__(self) -> None:
        for name in ("strategy_path", "class_name", "repository_commit",
                     "source_tree_digest", "dependency_lock_digest",
                     "container_digest", "dataset_manifest_digest"):
            _require_nonblank(name, getattr(self, name))
        if self.provenance not in PROVENANCES:
            raise ValueError(
                f"ExperimentFamily.provenance {self.provenance!r} not in {sorted(PROVENANCES)}")
        # normalize+validate the three canonicalizable mappings.
        object.__setattr__(self, "search_space", _require_canonical("search_space", self.search_space))
        object.__setattr__(self, "cost_model", _require_canonical("cost_model", self.cost_model))
        object.__setattr__(self, "validation_protocol",
                           _require_canonical("validation_protocol", self.validation_protocol))

    def _body(self) -> dict:
        return {
            "strategy_path": self.strategy_path,
            "class_name": self.class_name,
            "repository_commit": self.repository_commit,
            "source_tree_digest": self.source_tree_digest,
            "dependency_lock_digest": self.dependency_lock_digest,
            "container_digest": self.container_digest,
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "search_space": self.search_space,
            "cost_model": self.cost_model,
            "validation_protocol": self.validation_protocol,
            "provenance": self.provenance,
        }

    @property
    def family_id(self) -> str:
        return sha256_digest(FAMILY_DIGEST_PREFIX, self._body())


def trial_id(family_id: str, trial_key: str, parameters: Mapping[str, Any]) -> str:
    """Content-addressed trial identifier: same (family, key, parameters) -> same id."""
    return sha256_digest(
        TRIAL_DIGEST_PREFIX,
        {"family_id": family_id, "trial_key": trial_key, "parameters": dict(parameters)})


def artifact_id(family_id: str, selected_trial_id: str,
                selected_parameters: Mapping[str, Any], provenance: str) -> str:
    """Content-addressed artifact identifier over the selected configuration."""
    return sha256_digest(
        ARTIFACT_DIGEST_PREFIX,
        {"family_id": family_id, "selected_trial_id": selected_trial_id,
         "selected_parameters": dict(selected_parameters), "provenance": provenance})


@dataclass(frozen=True)
class TrialRecord:
    """A persisted trial as read back from the registry."""

    trial_id: str
    family_id: str
    trial_key: str
    parameters: Mapping[str, Any]
    status: str
    started_at: Any
    finished_at: Any
    metrics: Mapping[str, Any] = field(default_factory=dict)
    traceback_digest: Any = None
    safe_summary: str = ""
    archived: bool = False


@dataclass(frozen=True)
class ArtifactRecord:
    """A persisted strategy artifact as read back from the registry."""

    artifact_id: str
    family_id: str
    selected_trial_id: str
    selected_parameters: Mapping[str, Any]
    provenance: str
    state: str
    sealed_at: Any
    holdout_opened: bool = False
    holdout_passed: Any = None
