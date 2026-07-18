"""The complete experiment registry (P2 Task 4).

Records the WHOLE parameter search, not just its winners: every family's exact
code/dependency/container/data identity, and every trial -- SUCCEEDED, FAILED,
INVALID, or TIMED_OUT -- so the multiple-testing denominator can never be
quietly shrunk. Trials are append-only (there is no delete API and archiving
keeps a trial in ``selection_trial_count``); the final holdout is opened exactly
once (a failed holdout permanently RETIRES the artifact); and imported legacy
backtests are recorded as ``LEGACY_UNQUALIFIED`` families that are structurally
unable to seal an artifact or open a holdout -- they can never earn eligibility.

Research migrations 3-6 create the tables in the SEPARATE offline research
DuckDB. Every mutation runs inside a single ``DuckDBConnection.transaction`` so a
partial write (e.g. a trial-metrics insert failing) never leaves a half-recorded
trial. This module is OFFLINE-ONLY.
"""
from __future__ import annotations

import hashlib
import json
import math  # noqa: F401  (kept for symmetry with backtest_store helpers)
from datetime import timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from trader.data.backtest_store import (
    BacktestRecord,
    legacy_cost_model,
    legacy_import_group_key,
    legacy_result_metrics,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import (
    ARTIFACT_STATE_CANDIDATE,
    ARTIFACT_STATE_RETIRED,
    PROVENANCE_LEGACY_UNQUALIFIED,
    TERMINAL_TRIAL_STATUSES,
    TRIAL_RUNNING,
    TRIAL_SUCCEEDED,
    ArtifactRecord,
    ExperimentFamily,
    TrialRecord,
    artifact_id,
    trial_id,
)
from trader.research.canonical import canonical_json_bytes

RESEARCH_MIGRATION_FAMILIES = 3
RESEARCH_MIGRATION_TRIALS = 4
RESEARCH_MIGRATION_ARTIFACTS = 5
RESEARCH_MIGRATION_HOLDOUT = 6

_FAMILY_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS experiment_families (
        family_id VARCHAR PRIMARY KEY,
        strategy_path VARCHAR NOT NULL,
        class_name VARCHAR NOT NULL,
        repository_commit VARCHAR NOT NULL,
        source_tree_digest VARCHAR NOT NULL,
        dependency_lock_digest VARCHAR NOT NULL,
        container_digest VARCHAR NOT NULL,
        dataset_manifest_digest VARCHAR NOT NULL,
        search_space VARCHAR NOT NULL,
        cost_model VARCHAR NOT NULL,
        validation_protocol VARCHAR NOT NULL,
        provenance VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS validation_folds (
        family_id VARCHAR NOT NULL,
        sequence INTEGER NOT NULL,
        kind VARCHAR NOT NULL,
        spec VARCHAR NOT NULL,
        PRIMARY KEY (family_id, sequence)
    )
    """,
)

_TRIAL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS experiment_trials (
        trial_id VARCHAR PRIMARY KEY,
        family_id VARCHAR NOT NULL,
        trial_key VARCHAR NOT NULL,
        parameters VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        traceback_digest VARCHAR,
        safe_summary VARCHAR NOT NULL DEFAULT '',
        archived BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trial_metrics (
        trial_id VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        value VARCHAR NOT NULL,
        PRIMARY KEY (trial_id, name)
    )
    """,
)

_ARTIFACT_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS strategy_artifacts (
        artifact_id VARCHAR PRIMARY KEY,
        family_id VARCHAR NOT NULL,
        selected_trial_id VARCHAR NOT NULL,
        selected_parameters VARCHAR NOT NULL,
        provenance VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        sealed_at TIMESTAMPTZ NOT NULL
    )
    """,
)

_HOLDOUT_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS holdout_access_log (
        artifact_id VARCHAR PRIMARY KEY,
        opened_at TIMESTAMPTZ NOT NULL,
        passed BOOLEAN NOT NULL,
        detail VARCHAR NOT NULL DEFAULT ''
    )
    """,
)


def apply_experiment_migrations(migrator: SchemaMigrator) -> None:
    """Research DB migrations 3-6 (idempotent)."""
    migrator.apply(version=RESEARCH_MIGRATION_FAMILIES,
                   name="research_experiment_families",
                   statements=list(_FAMILY_STATEMENTS))
    migrator.apply(version=RESEARCH_MIGRATION_TRIALS,
                   name="research_experiment_trials",
                   statements=list(_TRIAL_STATEMENTS))
    migrator.apply(version=RESEARCH_MIGRATION_ARTIFACTS,
                   name="research_strategy_artifacts",
                   statements=list(_ARTIFACT_STATEMENTS))
    migrator.apply(version=RESEARCH_MIGRATION_HOLDOUT,
                   name="research_holdout_access",
                   statements=list(_HOLDOUT_STATEMENTS))


class RegistryError(Exception):
    """Base class for experiment-registry policy violations (fail loudly)."""


class UnknownFamily(RegistryError):
    pass


class UnknownTrial(RegistryError):
    pass


class UnknownArtifact(RegistryError):
    pass


class TrialAlreadyExists(RegistryError):
    """A trial_key was reused within a family -- would collide two experiments."""


class TrialAlreadyFinished(RegistryError):
    """A trial already reached a terminal state; its outcome is immutable."""


class TrialNotSucceeded(RegistryError):
    """Only a SUCCEEDED trial can be sealed into an artifact."""


class HoldoutAlreadyOpened(RegistryError):
    """The holdout is write-once per artifact; it cannot be re-opened or re-tested."""


class LegacyNotEligible(RegistryError):
    """A LEGACY_UNQUALIFIED family can be recorded but can never earn eligibility."""


def _dumps(value: Any) -> str:
    """Stable canonical-JSON text for a VARCHAR column (fails loud on non-canonical)."""
    return canonical_json_bytes(value).decode("utf-8")


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ExperimentRegistry:
    """Transactional, append-only registry of experiment families, trials, and
    sealed artifacts in the offline research DuckDB."""

    def __init__(self, db: Any):
        self._db = db

    # ---- families ------------------------------------------------------- #
    def create_family(self, family: ExperimentFamily, *, created_at,
                      validation_folds: Sequence[Mapping[str, Any]] = ()) -> str:
        fid = family.family_id

        def _tx(conn):
            if conn.execute("SELECT 1 FROM experiment_families WHERE family_id = ?",
                            [fid]).fetchone() is not None:
                return fid  # idempotent: content-addressed identity already recorded
            self._insert_family(conn, family, created_at, validation_folds)
            return fid

        return self._db.transaction(_tx)

    def get_family(self, family_id: str) -> Optional[ExperimentFamily]:
        def _tx(conn):
            r = conn.execute(
                "SELECT strategy_path, class_name, repository_commit, source_tree_digest, "
                "dependency_lock_digest, container_digest, dataset_manifest_digest, "
                "search_space, cost_model, validation_protocol, provenance "
                "FROM experiment_families WHERE family_id = ?", [family_id]).fetchone()
            if r is None:
                return None
            return ExperimentFamily(
                strategy_path=r[0], class_name=r[1], repository_commit=r[2],
                source_tree_digest=r[3], dependency_lock_digest=r[4], container_digest=r[5],
                dataset_manifest_digest=r[6], search_space=json.loads(r[7]),
                cost_model=json.loads(r[8]), validation_protocol=json.loads(r[9]),
                provenance=r[10])

        return self._db.transaction(_tx)

    def get_validation_folds(self, family_id: str) -> list[dict]:
        def _tx(conn):
            rows = conn.execute(
                "SELECT spec FROM validation_folds WHERE family_id = ? ORDER BY sequence",
                [family_id]).fetchall()
            return [json.loads(r[0]) for r in rows]

        return self._db.transaction(_tx)

    def _insert_family(self, conn, family: ExperimentFamily, created_at,
                       folds: Iterable[Mapping[str, Any]]) -> None:
        conn.execute(
            "INSERT INTO experiment_families (family_id, strategy_path, class_name, "
            "repository_commit, source_tree_digest, dependency_lock_digest, container_digest, "
            "dataset_manifest_digest, search_space, cost_model, validation_protocol, "
            "provenance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [family.family_id, family.strategy_path, family.class_name,
             family.repository_commit, family.source_tree_digest, family.dependency_lock_digest,
             family.container_digest, family.dataset_manifest_digest,
             _dumps(family.search_space), _dumps(family.cost_model),
             _dumps(family.validation_protocol), family.provenance, created_at])
        for seq, fold in enumerate(folds):
            conn.execute(
                "INSERT INTO validation_folds (family_id, sequence, kind, spec) "
                "VALUES (?, ?, ?, ?)",
                [family.family_id, seq, str(dict(fold).get("kind", "")), _dumps(dict(fold))])

    # ---- trials --------------------------------------------------------- #
    def start_trial(self, family_id: str, *, trial_key: str,
                    parameters: Mapping[str, Any], started_at) -> str:
        def _tx(conn):
            fam = conn.execute(
                "SELECT provenance FROM experiment_families WHERE family_id = ?",
                [family_id]).fetchone()
            if fam is None:
                raise UnknownFamily(family_id)
            if fam[0] == PROVENANCE_LEGACY_UNQUALIFIED:
                raise LegacyNotEligible(
                    f"family {family_id} is LEGACY_UNQUALIFIED; no new trials")
            if conn.execute(
                    "SELECT 1 FROM experiment_trials WHERE family_id = ? AND trial_key = ?",
                    [family_id, trial_key]).fetchone() is not None:
                raise TrialAlreadyExists(f"trial_key {trial_key!r} already used in {family_id}")
            tid = trial_id(family_id, trial_key, parameters)
            conn.execute(
                "INSERT INTO experiment_trials (trial_id, family_id, trial_key, parameters, "
                "status, started_at, finished_at, traceback_digest, safe_summary, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '', FALSE)",
                [tid, family_id, trial_key, _dumps(dict(parameters)), TRIAL_RUNNING, started_at])
            return tid

        return self._db.transaction(_tx)

    def finish_trial(self, trial_id: str, *, status: str, finished_at,
                     metrics: Optional[Mapping[str, Any]] = None,
                     traceback: Optional[str] = None, safe_summary: str = "") -> None:
        if status not in TERMINAL_TRIAL_STATUSES:
            raise ValueError(
                f"finish_trial status must be one of {sorted(TERMINAL_TRIAL_STATUSES)}, "
                f"got {status!r}")
        # Store ONLY a digest of the traceback -- the raw text may carry filesystem
        # paths or secrets and never belongs in the evidence store.
        tb_digest = (hashlib.sha256(traceback.encode("utf-8")).hexdigest()
                     if traceback else None)
        tid = trial_id

        def _tx(conn):
            row = conn.execute(
                "SELECT status FROM experiment_trials WHERE trial_id = ?", [tid]).fetchone()
            if row is None:
                raise UnknownTrial(tid)
            if row[0] != TRIAL_RUNNING:
                raise TrialAlreadyFinished(
                    f"trial {tid} already terminal ({row[0]}); outcome is immutable")
            conn.execute(
                "UPDATE experiment_trials SET status = ?, finished_at = ?, "
                "traceback_digest = ?, safe_summary = ? WHERE trial_id = ?",
                [status, finished_at, tb_digest, safe_summary, tid])
            for name, value in (metrics or {}).items():
                conn.execute(
                    "INSERT INTO trial_metrics (trial_id, name, value) VALUES (?, ?, ?)",
                    [tid, name, _dumps(value)])

        return self._db.transaction(_tx)

    def get_trial(self, trial_id: str) -> Optional[TrialRecord]:
        tid = trial_id

        def _tx(conn):
            r = conn.execute(
                "SELECT family_id, trial_key, parameters, status, started_at, finished_at, "
                "traceback_digest, safe_summary, archived FROM experiment_trials "
                "WHERE trial_id = ?", [tid]).fetchone()
            if r is None:
                return None
            return self._row_to_trial(conn, tid, r)

        return self._db.transaction(_tx)

    def list_trials(self, family_id: str, *, include_archived: bool = False
                    ) -> list[TrialRecord]:
        def _tx(conn):
            q = ("SELECT trial_id, family_id, trial_key, parameters, status, started_at, "
                 "finished_at, traceback_digest, safe_summary, archived "
                 "FROM experiment_trials WHERE family_id = ?")
            if not include_archived:
                q += " AND archived = FALSE"
            q += " ORDER BY started_at, trial_key"
            rows = conn.execute(q, [family_id]).fetchall()
            return [self._row_to_trial(conn, r[0], r[1:]) for r in rows]

        return self._db.transaction(_tx)

    def _row_to_trial(self, conn, tid: str, r) -> TrialRecord:
        metrics = {
            m[0]: json.loads(m[1])
            for m in conn.execute(
                "SELECT name, value FROM trial_metrics WHERE trial_id = ? ORDER BY name",
                [tid]).fetchall()}
        return TrialRecord(
            trial_id=tid, family_id=r[0], trial_key=r[1], parameters=json.loads(r[2]),
            status=r[3], started_at=_as_utc(r[4]), finished_at=_as_utc(r[5]),
            metrics=metrics, traceback_digest=r[6], safe_summary=r[7], archived=bool(r[8]))

    def selection_trial_count(self, family_id: str) -> int:
        """Every trial that reached a terminal state -- the multiple-testing
        denominator. Archived trials are still counted; only still-RUNNING trials
        (no result yet) are excluded."""
        def _tx(conn):
            row = conn.execute(
                "SELECT COUNT(*) FROM experiment_trials WHERE family_id = ? AND status != ?",
                [family_id, TRIAL_RUNNING]).fetchone()
            return int(row[0])

        return self._db.transaction(_tx)

    def set_trial_archived(self, trial_id: str, archived: bool) -> None:
        tid = trial_id

        def _tx(conn):
            if conn.execute("SELECT 1 FROM experiment_trials WHERE trial_id = ?",
                            [tid]).fetchone() is None:
                raise UnknownTrial(tid)
            conn.execute("UPDATE experiment_trials SET archived = ? WHERE trial_id = ?",
                         [bool(archived), tid])

        return self._db.transaction(_tx)

    # ---- artifacts + holdout ------------------------------------------- #
    def seal_artifact(self, family_id: str, *, selected_trial_id: str,
                      selected_parameters: Mapping[str, Any], sealed_at) -> str:
        def _tx(conn):
            fam = conn.execute(
                "SELECT provenance FROM experiment_families WHERE family_id = ?",
                [family_id]).fetchone()
            if fam is None:
                raise UnknownFamily(family_id)
            if fam[0] == PROVENANCE_LEGACY_UNQUALIFIED:
                raise LegacyNotEligible(
                    f"family {family_id} is LEGACY_UNQUALIFIED; cannot seal an artifact")
            trow = conn.execute(
                "SELECT family_id, status FROM experiment_trials WHERE trial_id = ?",
                [selected_trial_id]).fetchone()
            if trow is None or trow[0] != family_id:
                raise UnknownTrial(
                    f"trial {selected_trial_id} not found in family {family_id}")
            if trow[1] != TRIAL_SUCCEEDED:
                raise TrialNotSucceeded(
                    f"selected trial {selected_trial_id} is {trow[1]}, not SUCCEEDED")
            aid = artifact_id(family_id, selected_trial_id, selected_parameters, fam[0])
            if conn.execute("SELECT 1 FROM strategy_artifacts WHERE artifact_id = ?",
                            [aid]).fetchone() is not None:
                return aid  # idempotent
            conn.execute(
                "INSERT INTO strategy_artifacts (artifact_id, family_id, selected_trial_id, "
                "selected_parameters, provenance, state, sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [aid, family_id, selected_trial_id, _dumps(dict(selected_parameters)),
                 fam[0], ARTIFACT_STATE_CANDIDATE, sealed_at])
            return aid

        return self._db.transaction(_tx)

    def get_artifact(self, artifact_id: str) -> Optional[ArtifactRecord]:
        aid = artifact_id

        def _tx(conn):
            r = conn.execute(
                "SELECT family_id, selected_trial_id, selected_parameters, provenance, "
                "state, sealed_at FROM strategy_artifacts WHERE artifact_id = ?",
                [aid]).fetchone()
            if r is None:
                return None
            h = conn.execute(
                "SELECT passed FROM holdout_access_log WHERE artifact_id = ?", [aid]).fetchone()
            return ArtifactRecord(
                artifact_id=aid, family_id=r[0], selected_trial_id=r[1],
                selected_parameters=json.loads(r[2]), provenance=r[3], state=r[4],
                sealed_at=_as_utc(r[5]), holdout_opened=h is not None,
                holdout_passed=(bool(h[0]) if h is not None else None))

        return self._db.transaction(_tx)

    def open_holdout(self, artifact_id: str, *, opened_at, passed: bool,
                     detail: str = "") -> None:
        aid = artifact_id

        def _tx(conn):
            arow = conn.execute(
                "SELECT state FROM strategy_artifacts WHERE artifact_id = ?", [aid]).fetchone()
            if arow is None:
                raise UnknownArtifact(aid)
            if conn.execute("SELECT 1 FROM holdout_access_log WHERE artifact_id = ?",
                            [aid]).fetchone() is not None:
                raise HoldoutAlreadyOpened(
                    f"artifact {aid} holdout is write-once and was already opened")
            conn.execute(
                "INSERT INTO holdout_access_log (artifact_id, opened_at, passed, detail) "
                "VALUES (?, ?, ?, ?)", [aid, opened_at, bool(passed), detail])
            if not passed:
                # a failed holdout is a one-way trip -- retire the version so it can
                # never be re-tuned and re-tested under a different name.
                conn.execute("UPDATE strategy_artifacts SET state = ? WHERE artifact_id = ?",
                             [ARTIFACT_STATE_RETIRED, aid])

        return self._db.transaction(_tx)

    # ---- legacy import -------------------------------------------------- #
    def import_legacy_backtests(self, records: Sequence[BacktestRecord],
                                *, imported_at) -> list[str]:
        """Import historical ``BacktestRecord`` rows as ``LEGACY_UNQUALIFIED``
        families. Runs of the same (strategy_path, class_name) form one family;
        each run becomes a SUCCEEDED trial so it counts in the denominator, but
        the family can never seal an artifact or open a holdout."""
        groups: dict[tuple[str, str], list[tuple[int, BacktestRecord]]] = {}
        for i, rec in enumerate(records):
            groups.setdefault(legacy_import_group_key(rec), []).append((i, rec))

        family_ids: list[str] = []
        for (path, cls), items in groups.items():
            first = items[0][1]
            family = ExperimentFamily(
                strategy_path=path, class_name=cls,
                repository_commit="legacy:unknown",
                source_tree_digest=(first.code_hash or "legacy:unknown"),
                dependency_lock_digest="legacy:unknown",
                container_digest="legacy:unknown",
                dataset_manifest_digest="legacy:unqualified",
                search_space={}, cost_model=legacy_cost_model(first),
                validation_protocol={"protocol": "legacy_unqualified"},
                provenance=PROVENANCE_LEGACY_UNQUALIFIED)
            fid = family.family_id

            def _tx(conn, family=family, fid=fid, items=items):
                if conn.execute("SELECT 1 FROM experiment_families WHERE family_id = ?",
                                [fid]).fetchone() is None:
                    self._insert_family(conn, family, imported_at, ())
                for idx, rec in items:
                    key = f"legacy:{rec.id if rec.id is not None else idx}"
                    tid = trial_id(fid, key, rec.params or {})
                    if conn.execute("SELECT 1 FROM experiment_trials WHERE trial_id = ?",
                                    [tid]).fetchone() is not None:
                        continue  # idempotent re-import
                    ts = _as_utc(rec.created_at) or imported_at
                    conn.execute(
                        "INSERT INTO experiment_trials (trial_id, family_id, trial_key, "
                        "parameters, status, started_at, finished_at, traceback_digest, "
                        "safe_summary, archived) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, FALSE)",
                        [tid, fid, key, _dumps(dict(rec.params or {})), TRIAL_SUCCEEDED,
                         ts, ts, "imported legacy backtest"])
                    for name, value in legacy_result_metrics(rec).items():
                        conn.execute(
                            "INSERT INTO trial_metrics (trial_id, name, value) VALUES (?, ?, ?)",
                            [tid, name, _dumps(value)])
                return fid

            family_ids.append(self._db.transaction(_tx))
        return family_ids
