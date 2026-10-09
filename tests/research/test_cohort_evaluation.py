import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, holdout_ruleset, write_costs_config,
                                                write_trend_bars, write_universe)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.research import cohort_evaluation as ce
from trader.research import evaluation
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.research.cohort import RequestRefused, build_cohort_spec, request_body
from trader.research.evaluation import EvaluationPaths, HoldoutOutcome
from trader.research.evaluation_jobs import WindowOutcome
from trader.research.evaluation_store import EvaluationRepository
from trader.research.evidence import GateOutcome
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
TODAY = dt.date(2024, 3, 29)
NOW = dt.datetime(2024, 3, 29, 21, tzinfo=dt.timezone.utc)


@pytest.fixture
def workspace(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    build_spec_file(tmp_path)
    costs = write_costs_config(tmp_path / "execution_costs.yaml")
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    paths = EvaluationPaths(tmp_duckdb_path, tmp_duckdb_path, "Universes", str(costs), tmp_path,
                            tmp_path / "reports", tmp_path / "artifacts" / "evaluations")
    config = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
    judge = BacktestJudgeConfig(strategy_allowlist=(KEY,), max_cohort_points=3)

    def spec_for(cohort):
        return build_cohort_spec(
            request_body({"strategy_key": KEY, "cohort": cohort, "conids": CONIDS, "bar_size": "15 mins"}, TODAY),
            config=config, judge=judge, universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
            costs_config=load_execution_costs_config(str(costs)), repo_root=tmp_path,
            registry=ExperimentRegistry(db), history_db=tmp_duckdb_path)
    return SimpleNamespace(db=db, paths=paths, spec_for=spec_for, registry=ExperimentRegistry(db))


@pytest.fixture
def world(workspace, monkeypatch):
    gates, jobs, opened = {}, [], []

    def fake_run_jobs(env, batch, max_workers=1):
        jobs.extend(batch)
        return [WindowOutcome(job, (), ((job.start, 100_000.0),), 0.0, 0.0, "flat", {}) for job in batch]

    def fake_verdict(index, base, plan, point, neighbours, trials, closes, bars, ruleset):
        passed, statistic = gates[index]
        return ce.PointVerdict(index, point, tuple(neighbours), GateOutcome(passed, () if passed else ("x",), ()),
                               None, SimpleNamespace(selection_adjusted_confidence=statistic), None)

    def fake_holdout(research_db, registry, spec, env, plan, family_id, point, evidence, *rest):
        aid = registry.seal_artifact(family_id, selected_trial_id=point.trial_id,
                                     selected_parameters=dict(point.params), sealed_at=NOW)
        registry.open_holdout(aid, opened_at=NOW, passed=True)
        opened.append(dict(point.params))
        return HoldoutOutcome(aid, True, evidence, SimpleNamespace(state="PAPER_ELIGIBLE"), "d" * 64,
                              {"start": "2024-03-22", "end": "2024-03-28", "passed": True, "detail": ""})

    monkeypatch.setattr(evaluation, "run_jobs", fake_run_jobs)
    monkeypatch.setattr(ce, "_verdict", fake_verdict)
    monkeypatch.setattr(ce, "run_holdout", fake_holdout)
    monkeypatch.setattr(ce, "_record_evaluation", lambda *args, **kwargs: None)

    def run(cohort, statistics):
        gates.update(statistics)
        spec = workspace.spec_for(cohort)
        return spec, ce.evaluate_cohort(spec, research_db=workspace.db, paths=workspace.paths, now=lambda: NOW)
    return SimpleNamespace(run=run, jobs=jobs, opened=opened, registry=workspace.registry, db=workspace.db,
                           paths=workspace.paths)


def test_the_best_point_that_fails_the_gate_is_not_selected(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}], {0: (False, 0.99), 1: (True, 0.6)})
    assert result.selected.index == 1 and world.opened == [{"ENTRY_MINUTE": 615}]
    assert result.stage == "complete" and len(world.registry.family_artifacts(result.family_id)) == 1


def test_every_cohort_point_runs_full_cost_stress_and_neighbours_only_at_1x(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}], {0: (True, 0.5), 1: (True, 0.5)})
    by_point = {}
    for job in world.jobs:
        by_point.setdefault(job.point_key, set()).add(job.cost_multiplier)
    cohort_keys = {evaluation._point_key(p) for p in spec.cohort}
    assert all(by_point[k] == {1.0, 1.5, 2.0} for k in cohort_keys)
    assert all(v == {1.0} for k, v in by_point.items() if k not in cohort_keys and k.startswith("point:"))
    assert result.selected.index == 0                                   # tie: cohort order
    assert result.selected.point.params in spec.cohort                 # a neighbour is never selectable


def test_no_passing_point_seals_nothing_and_opens_no_holdout(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.9)})
    assert result.stage == "pre_holdout" and result.selected is None and result.holdout is None
    assert world.registry.family_artifacts(result.family_id) == [] and world.opened == []
    assert result.replay_index == 0


def test_one_point_and_two_neighbours_add_three_trials(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.1)})
    assert len(world.registry.strategy_trials("strategies/time_of_day.py", "TimeOfDay")) == 3
    assert result.strategy_trials == 3


def test_a_neighbour_equal_to_another_cohort_point_is_one_trial(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 660}], {0: (False, 0.1), 1: (False, 0.2)})
    assert len(world.registry.strategy_trials("strategies/time_of_day.py", "TimeOfDay")) == 5   # not 6


def test_a_trial_stranded_by_a_crash_before_the_bars_changed_is_closed_and_counted(world):
    _, first = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.1)})
    stranded = world.registry.start_trial(first.family_id, trial_key="point:crashed#1",
                                          parameters={"ENTRY_MINUTE": 630}, started_at=NOW)
    write_trend_bars(world.paths.history_db, drift=0.0007)              # new dataset manifest: a new family
    _, second = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.1)})
    assert second.family_id != first.family_id
    trial = world.registry.get_trial(stranded)
    assert (trial.status, trial.safe_summary) == ("FAILED", "interrupted before it finished")
    assert second.strategy_trials == 3 + 1 + 3


def test_previously_revealed_sessions_are_labelled(world):
    registry = world.registry
    spec, first = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.1)})
    family = replace(registry.get_family(first.family_id), search_space={"earlier": True})
    fid = registry.create_family(family, created_at=NOW, validation_folds=[
        {"kind": "holdout", "start": "2024-02-20", "end": "2024-02-23"}])
    tid = registry.start_trial(fid, trial_key="point:{}#1", parameters={}, started_at=NOW)
    registry.finish_trial(tid, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": 0.1})
    registry.open_holdout(registry.seal_artifact(fid, selected_trial_id=tid, selected_parameters={},
                                                 sealed_at=NOW), opened_at=NOW, passed=False)
    _, later = world.run([{"ENTRY_MINUTE": 615}], {0: (False, 0.1)})
    assert later.previously_revealed == ["2024-02-20", "2024-02-21", "2024-02-22", "2024-02-23"]
    assert later.holdouts_opened_before == 1


def _verdict_of(index, passed, statistic):
    return ce.PointVerdict(index, None, (), GateOutcome(passed, (), ()), None,
                           SimpleNamespace(selection_adjusted_confidence=statistic), None)


def test_selection_ranks_by_statistic_then_cohort_order_and_ignores_unmeasured_values():
    verdicts = [_verdict_of(0, True, 0.5), _verdict_of(1, True, 0.5), _verdict_of(2, True, float("nan")),
                _verdict_of(3, False, 0.99)]
    assert ce.select_point(verdicts).index == 0
    assert ce.select_point([_verdict_of(0, True, None), _verdict_of(1, True, float("nan"))]).index == 0
    assert ce.select_point([_verdict_of(0, False, 0.9)]) is None


def test_the_replay_point_is_the_best_statistic_when_nothing_is_selected():
    verdicts = [_verdict_of(0, False, 0.2), _verdict_of(1, False, 0.8), _verdict_of(2, False, 0.8)]
    assert ce.replay_index(verdicts, None) == 1
    assert ce.replay_index([_verdict_of(0, False, None)], None) == 0
    assert ce.replay_index(verdicts, verdicts[2]) == 2


def test_an_empty_cohort_is_refused_not_an_index_error(workspace):
    spec = replace(workspace.spec_for([{"ENTRY_MINUTE": 600}]), cohort=(), neighbours=())
    with pytest.raises(evaluation.EvaluationError, match="no points"):
        ce.evaluate_cohort(spec, research_db=workspace.db, paths=workspace.paths, now=lambda: NOW)


@pytest.mark.timeout(600)
def test_a_real_cohort_with_a_passing_holdout_is_recorded_and_complete(workspace):
    spec = workspace.spec_for([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}])
    result = ce.evaluate_cohort(spec, research_db=workspace.db, paths=workspace.paths, now=lambda: NOW,
                                ruleset=holdout_ruleset())
    assert result.stage == "complete" and result.selected is not None and result.holdout.passed
    assert result.selected.point.params in spec.cohort
    artifacts = workspace.registry.family_artifacts(result.family_id)
    assert [a.artifact_id for a in artifacts] == [result.holdout.artifact_id] and artifacts[0].holdout_opened
    assert [v.index for v in result.verdicts] == [0, 1] and result.replay_index == result.selected.index
    record = EvaluationRepository(workspace.db).list()[0]
    assert (record.stage, record.family_id) == ("complete", result.family_id)
    assert record.decision_digest == result.holdout.decision_digest and record.state == "PAPER_ELIGIBLE"
    for verdict in result.verdicts:                        # the holdout adds nothing to a verdict's own context
        assert "benchmark" not in verdict.context.market_context


@pytest.mark.timeout(600)
def test_a_real_cohort_that_fails_pre_holdout_is_recorded_without_an_artifact(workspace):
    spec = workspace.spec_for([{"ENTRY_MINUTE": 600}])
    result = ce.evaluate_cohort(spec, research_db=workspace.db, paths=workspace.paths, now=lambda: NOW)
    assert result.stage == "pre_holdout" and result.selected is None and result.holdout is None
    assert workspace.registry.family_artifacts(result.family_id) == []
    record = EvaluationRepository(workspace.db).list()[0]
    assert (record.stage, record.state, record.artifact_id) == ("pre_holdout", "CANDIDATE", None)
    assert "min_round_trips" in record.failed_rules


@pytest.mark.timeout(600)
def test_a_holdout_that_dies_after_its_backtests_still_counts_as_revealed(workspace, monkeypatch):
    spec = workspace.spec_for([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}])

    def broken(*args, **kwargs):
        raise evaluation.EvaluationError("market context unavailable: test")
    monkeypatch.setattr(evaluation, "_holdout_market_evidence", broken)
    with pytest.raises(evaluation.EvaluationError, match="market context unavailable"):
        ce.evaluate_cohort(spec, research_db=workspace.db, paths=workspace.paths, now=lambda: NOW,
                           ruleset=holdout_ruleset())
    windows = workspace.registry.opened_holdout_windows("strategies/time_of_day.py", "TimeOfDay")
    assert [(w["start"], w["end"]) for w in windows] == [(dt.date(2024, 3, 22), dt.date(2024, 3, 28))]
    artifact = workspace.registry.get_artifact(windows[0]["artifact_id"])
    assert artifact.holdout_opened and artifact.holdout_passed is False and artifact.state == "RETIRED"
    for cohort in ([{"ENTRY_MINUTE": 630}], [{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}]):   # new and same family
        with pytest.raises(RequestRefused) as refused:
            workspace.spec_for(cohort)
        assert refused.value.code == "HOLDOUT_NOT_AVAILABLE"
    with pytest.raises(RequestRefused) as refused:                                  # a stale spec is refused too
        ce.evaluate_cohort(spec, research_db=workspace.db, paths=workspace.paths, now=lambda: NOW,
                           ruleset=holdout_ruleset())
    assert refused.value.code == "HOLDOUT_NOT_AVAILABLE"
