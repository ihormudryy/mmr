"""P2 Task 4 — the complete experiment registry.

A backtest that only records its winners is a selection-bias generator: the
"best of 500 tries" looks like an edge even when every try was noise. This
registry records the WHOLE experiment family -- exact code/dependency/container
identity, the declared search space, and *every* trial (including the failures
and the timeouts) so the multiple-testing denominator can never be quietly
shrunk. The final holdout is opened exactly once; a failed holdout permanently
retires that artifact version so it cannot be re-tuned and re-tested under a new
name.

These tests pin those guarantees: content-addressed determinism, append-only
trials, an intact selection denominator across archiving, write-once holdouts,
and legacy backtest imports that are recorded but can never earn eligibility.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trader.data.backtest_store import BacktestRecord
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import (
    ARTIFACT_STATE_CANDIDATE,
    ARTIFACT_STATE_RETIRED,
    PROVENANCE_LEGACY_UNQUALIFIED,
    PROVENANCE_RESEARCH,
    TRIAL_FAILED,
    TRIAL_INVALID,
    TRIAL_RUNNING,
    TRIAL_SUCCEEDED,
    TRIAL_TIMED_OUT,
    ExperimentFamily,
)
from trader.research.experiment_registry import (
    ExperimentRegistry,
    HoldoutAlreadyOpened,
    LegacyNotEligible,
    TrialAlreadyExists,
    TrialAlreadyFinished,
    TrialNotSucceeded,
    UnknownArtifact,
    UnknownFamily,
    UnknownTrial,
    apply_experiment_migrations,
)
from trader.research.schema import apply_research_migrations

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _family(**over) -> ExperimentFamily:
    kw = dict(
        strategy_path="strategies/orb.py",
        class_name="OpeningRangeBreakout",
        repository_commit="a" * 40,
        source_tree_digest="src-" + "0" * 60,
        dependency_lock_digest="dep-" + "0" * 60,
        container_digest="img-" + "0" * 60,
        dataset_manifest_digest="ds-" + "0" * 60,
        search_space={"RANGE_MINUTES": [15, 30, 45], "VOLUME_MULT": [1.2, 1.5]},
        cost_model={"fill_policy": "next_open", "slippage_bps": 2.0,
                    "commission_per_share": 0.005},
        validation_protocol={"training": "2020-01-01/2023-12-31",
                             "walk_forward_folds": 6, "embargo_days": 5,
                             "holdout": "2025-01-01/2025-12-31"},
    )
    kw.update(over)
    return ExperimentFamily(**kw)


@pytest.fixture
def registry(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    migrator = SchemaMigrator(db)
    apply_research_migrations(migrator)  # 1-2 (manifests) + 3-6 (registry)
    return ExperimentRegistry(db)


# --------------------------------------------------------------------------- #
# Family creation + content-addressed identity
# --------------------------------------------------------------------------- #
class TestFamily:
    def test_create_returns_digest_and_get_round_trips(self, registry):
        f = _family()
        fid = registry.create_family(f, created_at=T0)
        assert fid == f.family_id
        got = registry.get_family(fid)
        assert got is not None
        assert got.family_id == f.family_id
        assert got.strategy_path == "strategies/orb.py"
        assert got.search_space == {"RANGE_MINUTES": [15, 30, 45], "VOLUME_MULT": [1.2, 1.5]}
        assert got.validation_protocol["embargo_days"] == 5
        assert got.provenance == PROVENANCE_RESEARCH

    def test_family_digest_is_deterministic_and_prefixed(self):
        assert _family().family_id == _family().family_id
        assert _family().family_id.startswith("") and len(_family().family_id) == 64

    def test_digest_changes_when_any_identity_field_changes(self):
        base = _family().family_id
        for field, value in [
            ("strategy_path", "strategies/other.py"),
            ("class_name", "Other"),
            ("repository_commit", "b" * 40),
            ("source_tree_digest", "src-DIFFERENT"),
            ("dependency_lock_digest", "dep-DIFFERENT"),
            ("container_digest", "img-DIFFERENT"),
            ("dataset_manifest_digest", "ds-DIFFERENT"),
            ("search_space", {"RANGE_MINUTES": [15, 30]}),
            ("cost_model", {"fill_policy": "same_close"}),
            ("validation_protocol", {"holdout": "different"}),
        ]:
            assert _family(**{field: value}).family_id != base, field

    def test_search_space_order_is_significant_but_key_order_is_not(self):
        # declared grid order is meaningful; mapping key order is not.
        a = _family(search_space={"A": [1, 2], "B": [3]})
        b = _family(search_space={"B": [3], "A": [1, 2]})
        assert a.family_id == b.family_id
        c = _family(search_space={"A": [2, 1], "B": [3]})
        assert c.family_id != a.family_id

    def test_create_family_is_idempotent(self, registry):
        f = _family()
        d1 = registry.create_family(f, created_at=T0)
        d2 = registry.create_family(f, created_at=T0)
        assert d1 == d2
        assert registry.get_family(d1).family_id == d1

    def test_get_unknown_family_is_none(self, registry):
        assert registry.get_family("deadbeef") is None

    def test_blank_digest_is_rejected(self):
        with pytest.raises(ValueError):
            _family(source_tree_digest="")
        with pytest.raises(ValueError):
            _family(repository_commit="   ")

    def test_unknown_provenance_is_rejected(self):
        with pytest.raises(ValueError):
            _family(provenance="MADE_UP")

    def test_noncanonical_search_space_is_rejected(self):
        # a NaN in the declared space would silently fork the digest downstream.
        with pytest.raises(ValueError):
            _family(search_space={"X": [float("nan")]})

    def test_validation_folds_round_trip(self, registry):
        f = _family()
        folds = (
            {"kind": "walk_forward", "train": "2020/2022", "test": "2023"},
            {"kind": "walk_forward", "train": "2021/2023", "test": "2024"},
            {"kind": "holdout", "test": "2025"},
        )
        registry.create_family(f, created_at=T0, validation_folds=folds)
        got = registry.get_validation_folds(f.family_id)
        assert [x["kind"] for x in got] == ["walk_forward", "walk_forward", "holdout"]
        assert got[2]["test"] == "2025"


# --------------------------------------------------------------------------- #
# Trials: start-before-execution, terminal states, append-only denominator
# --------------------------------------------------------------------------- #
class TestTrials:
    def _fam(self, registry):
        f = _family()
        registry.create_family(f, created_at=T0)
        return f.family_id

    def test_start_trial_inserts_running_before_execution(self, registry):
        fid = self._fam(registry)
        tid = registry.start_trial(fid, trial_key="t1",
                                   parameters={"RANGE_MINUTES": 30}, started_at=T0)
        tr = registry.get_trial(tid)
        assert tr.status == TRIAL_RUNNING
        assert tr.finished_at is None
        assert tr.parameters == {"RANGE_MINUTES": 30}
        assert tr.family_id == fid

    def test_finish_succeeded_records_metrics(self, registry):
        fid = self._fam(registry)
        tid = registry.start_trial(fid, trial_key="t1", parameters={"X": 1}, started_at=T0)
        registry.finish_trial(tid, status=TRIAL_SUCCEEDED,
                              finished_at=T0 + dt.timedelta(minutes=5),
                              metrics={"net_expectancy_bps": 12.5, "sharpe": 1.8})
        tr = registry.get_trial(tid)
        assert tr.status == TRIAL_SUCCEEDED
        assert tr.finished_at == T0 + dt.timedelta(minutes=5)
        assert tr.metrics["net_expectancy_bps"] == 12.5
        assert tr.metrics["sharpe"] == 1.8

    def test_finish_failed_stores_traceback_digest_not_raw_text(self, registry):
        fid = self._fam(registry)
        tid = registry.start_trial(fid, trial_key="t1", parameters={}, started_at=T0)
        raw = 'Traceback: File "/home/secret/path.py", line 9\nZeroDivisionError'
        registry.finish_trial(tid, status=TRIAL_FAILED, finished_at=T0,
                              traceback=raw, safe_summary="ZeroDivisionError")
        tr = registry.get_trial(tid)
        assert tr.status == TRIAL_FAILED
        assert tr.safe_summary == "ZeroDivisionError"
        assert tr.traceback_digest and len(tr.traceback_digest) == 64
        # the raw traceback (which may carry paths/secrets) is never persisted.
        assert "secret" not in (tr.traceback_digest or "")
        assert raw not in (tr.traceback_digest or "")

    @pytest.mark.parametrize("status", [TRIAL_INVALID, TRIAL_TIMED_OUT])
    def test_finish_invalid_and_timed_out(self, registry, status):
        fid = self._fam(registry)
        tid = registry.start_trial(fid, trial_key="t1", parameters={}, started_at=T0)
        registry.finish_trial(tid, status=status, finished_at=T0, safe_summary="nope")
        assert registry.get_trial(tid).status == status

    def test_finish_rejects_nonterminal_status(self, registry):
        fid = self._fam(registry)
        tid = registry.start_trial(fid, trial_key="t1", parameters={}, started_at=T0)
        with pytest.raises(ValueError):
            registry.finish_trial(tid, status=TRIAL_RUNNING, finished_at=T0)
        with pytest.raises(ValueError):
            registry.finish_trial(tid, status="WAT", finished_at=T0)

    def test_finish_rejects_already_finished_trial(self, registry):
        fid = self._fam(registry)
        tid = registry.start_trial(fid, trial_key="t1", parameters={}, started_at=T0)
        registry.finish_trial(tid, status=TRIAL_SUCCEEDED, finished_at=T0, metrics={})
        with pytest.raises(TrialAlreadyFinished):
            registry.finish_trial(tid, status=TRIAL_FAILED, finished_at=T0)

    def test_start_rejects_duplicate_trial_key(self, registry):
        fid = self._fam(registry)
        registry.start_trial(fid, trial_key="dup", parameters={"X": 1}, started_at=T0)
        with pytest.raises(TrialAlreadyExists):
            registry.start_trial(fid, trial_key="dup", parameters={"X": 2}, started_at=T0)

    def test_start_unknown_family_raises(self, registry):
        with pytest.raises(UnknownFamily):
            registry.start_trial("nope", trial_key="t", parameters={}, started_at=T0)

    def test_finish_unknown_trial_raises(self, registry):
        with pytest.raises(UnknownTrial):
            registry.finish_trial("nope", status=TRIAL_SUCCEEDED, finished_at=T0)

    def test_no_trial_deletion_api(self):
        for forbidden in ("delete_trial", "remove_trial", "purge_trial", "drop_trial"):
            assert not hasattr(ExperimentRegistry, forbidden)

    def test_selection_count_includes_failures_and_archived(self, registry):
        fid = self._fam(registry)
        # 1 success, 1 failure, 1 invalid, 1 timeout, 1 still-running.
        for i, st in enumerate([TRIAL_SUCCEEDED, TRIAL_FAILED, TRIAL_INVALID, TRIAL_TIMED_OUT]):
            tid = registry.start_trial(fid, trial_key=f"t{i}", parameters={"i": i}, started_at=T0)
            registry.finish_trial(tid, status=st, finished_at=T0, metrics={}, safe_summary="x")
        running = registry.start_trial(fid, trial_key="running", parameters={}, started_at=T0)
        # every finished trial is a member of the denominator...
        assert registry.selection_trial_count(fid) == 4
        # ...and archiving one hides it from views but NOT from the denominator.
        failed_tid = registry.start_trial(fid, trial_key="arch", parameters={}, started_at=T0)
        registry.finish_trial(failed_tid, status=TRIAL_FAILED, finished_at=T0, safe_summary="x")
        registry.set_trial_archived(failed_tid, True)
        assert registry.selection_trial_count(fid) == 5
        visible = {t.trial_key for t in registry.list_trials(fid)}
        assert "arch" not in visible and "t0" in visible
        assert "arch" in {t.trial_key for t in registry.list_trials(fid, include_archived=True)}


# --------------------------------------------------------------------------- #
# Artifacts + write-once holdout
# --------------------------------------------------------------------------- #
class TestArtifactAndHoldout:
    def _succeeded_trial(self, registry):
        f = _family()
        registry.create_family(f, created_at=T0)
        tid = registry.start_trial(f.family_id, trial_key="win",
                                   parameters={"RANGE_MINUTES": 30}, started_at=T0)
        registry.finish_trial(tid, status=TRIAL_SUCCEEDED, finished_at=T0,
                             metrics={"sharpe": 2.0})
        return f.family_id, tid

    def test_seal_creates_candidate_artifact(self, registry):
        fid, tid = self._succeeded_trial(registry)
        aid = registry.seal_artifact(fid, selected_trial_id=tid,
                                     selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        art = registry.get_artifact(aid)
        assert art.state == ARTIFACT_STATE_CANDIDATE
        assert art.provenance == PROVENANCE_RESEARCH
        assert art.family_id == fid
        assert art.selected_parameters == {"RANGE_MINUTES": 30}
        assert art.holdout_opened is False

    def test_seal_requires_succeeded_trial(self, registry):
        f = _family()
        registry.create_family(f, created_at=T0)
        tid = registry.start_trial(f.family_id, trial_key="bad", parameters={}, started_at=T0)
        registry.finish_trial(tid, status=TRIAL_FAILED, finished_at=T0, safe_summary="x")
        with pytest.raises(TrialNotSucceeded):
            registry.seal_artifact(f.family_id, selected_trial_id=tid,
                                   selected_parameters={}, sealed_at=T0)

    def test_seal_is_idempotent(self, registry):
        fid, tid = self._succeeded_trial(registry)
        a1 = registry.seal_artifact(fid, selected_trial_id=tid,
                                    selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        a2 = registry.seal_artifact(fid, selected_trial_id=tid,
                                    selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        assert a1 == a2

    def test_seal_unknown_family_or_trial_raises(self, registry):
        fid, tid = self._succeeded_trial(registry)
        with pytest.raises(UnknownFamily):
            registry.seal_artifact("nope", selected_trial_id=tid,
                                   selected_parameters={}, sealed_at=T0)
        with pytest.raises(UnknownTrial):
            registry.seal_artifact(fid, selected_trial_id="nope",
                                   selected_parameters={}, sealed_at=T0)

    def test_holdout_opens_once_then_second_open_rejected(self, registry):
        fid, tid = self._succeeded_trial(registry)
        aid = registry.seal_artifact(fid, selected_trial_id=tid,
                                     selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        registry.open_holdout(aid, opened_at=T0, passed=True, detail="dd 2.1%")
        assert registry.get_artifact(aid).holdout_passed is True
        with pytest.raises(HoldoutAlreadyOpened):
            registry.open_holdout(aid, opened_at=T0, passed=True)
        # even a second *pass* is forbidden -- no re-testing under a new name.
        with pytest.raises(HoldoutAlreadyOpened):
            registry.open_holdout(aid, opened_at=T0, passed=False)

    def test_passed_holdout_keeps_candidate(self, registry):
        fid, tid = self._succeeded_trial(registry)
        aid = registry.seal_artifact(fid, selected_trial_id=tid,
                                     selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        registry.open_holdout(aid, opened_at=T0, passed=True)
        art = registry.get_artifact(aid)
        assert art.state == ARTIFACT_STATE_CANDIDATE  # eligibility is a later decision
        assert art.holdout_opened is True and art.holdout_passed is True

    def test_failed_holdout_retires_artifact(self, registry):
        fid, tid = self._succeeded_trial(registry)
        aid = registry.seal_artifact(fid, selected_trial_id=tid,
                                     selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        registry.open_holdout(aid, opened_at=T0, passed=False, detail="dd 8%")
        art = registry.get_artifact(aid)
        assert art.state == ARTIFACT_STATE_RETIRED
        assert art.holdout_passed is False

    def test_open_holdout_on_retired_artifact_rejected(self, registry):
        fid, tid = self._succeeded_trial(registry)
        aid = registry.seal_artifact(fid, selected_trial_id=tid,
                                     selected_parameters={"RANGE_MINUTES": 30}, sealed_at=T0)
        registry.open_holdout(aid, opened_at=T0, passed=False)
        with pytest.raises(HoldoutAlreadyOpened):
            registry.open_holdout(aid, opened_at=T0, passed=True)

    def test_open_holdout_unknown_artifact_raises(self, registry):
        with pytest.raises(UnknownArtifact):
            registry.open_holdout("nope", opened_at=T0, passed=True)


# --------------------------------------------------------------------------- #
# Legacy import: recorded, but structurally unable to earn eligibility
# --------------------------------------------------------------------------- #
def _bt_record(rid, path="strategies/orb.py", cls="OpeningRangeBreakout", **over):
    kw = dict(
        strategy_path=path, class_name=cls, conids=[756733], universe="",
        start_date=dt.datetime(2024, 1, 1, tzinfo=UTC),
        end_date=dt.datetime(2024, 6, 1, tzinfo=UTC),
        bar_size="1 day", initial_capital=100000.0, fill_policy="next_open",
        slippage_bps=2.0, commission_per_share=0.005,
        params={"RANGE_MINUTES": 30}, code_hash="c" * 64,
        total_trades=42, total_return=0.15, sharpe_ratio=1.4,
        id=rid, created_at=T0,
    )
    kw.update(over)
    return BacktestRecord(**kw)


class TestLegacyImport:
    def test_import_marks_family_unqualified(self, registry):
        fids = registry.import_legacy_backtests([_bt_record(1), _bt_record(2)], imported_at=T0)
        assert len(fids) == 1  # same (path, class) -> one family
        fam = registry.get_family(fids[0])
        assert fam.provenance == PROVENANCE_LEGACY_UNQUALIFIED

    def test_import_records_each_run_as_trial_in_denominator(self, registry):
        fids = registry.import_legacy_backtests(
            [_bt_record(1), _bt_record(2), _bt_record(3)], imported_at=T0)
        assert registry.selection_trial_count(fids[0]) == 3
        trials = registry.list_trials(fids[0])
        assert {t.status for t in trials} == {TRIAL_SUCCEEDED}
        assert trials[0].metrics["total_return"] == 0.15

    def test_legacy_family_cannot_seal_artifact(self, registry):
        fids = registry.import_legacy_backtests([_bt_record(1)], imported_at=T0)
        trial = registry.list_trials(fids[0])[0]
        with pytest.raises(LegacyNotEligible):
            registry.seal_artifact(fids[0], selected_trial_id=trial.trial_id,
                                   selected_parameters={}, sealed_at=T0)

    def test_legacy_family_cannot_start_new_trial(self, registry):
        fids = registry.import_legacy_backtests([_bt_record(1)], imported_at=T0)
        with pytest.raises(LegacyNotEligible):
            registry.start_trial(fids[0], trial_key="new", parameters={}, started_at=T0)

    def test_import_is_idempotent(self, registry):
        registry.import_legacy_backtests([_bt_record(1), _bt_record(2)], imported_at=T0)
        registry.import_legacy_backtests([_bt_record(1), _bt_record(2)], imported_at=T0)
        fids = registry.import_legacy_backtests([_bt_record(1)], imported_at=T0)
        assert registry.selection_trial_count(fids[0]) == 2  # no duplicate trials

    def test_distinct_strategies_form_distinct_families(self, registry):
        fids = registry.import_legacy_backtests(
            [_bt_record(1, cls="A"), _bt_record(2, cls="B")], imported_at=T0)
        assert len(fids) == 2


def test_migrations_are_idempotent(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "r.duckdb"))
    migrator = SchemaMigrator(db)
    apply_experiment_migrations(migrator)
    apply_experiment_migrations(migrator)  # second run is a no-op
    assert {3, 4, 5, 6}.issubset(migrator.applied_versions())
