import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.research.evaluation_fixtures import (
    build_spec_file, write_costs_config, write_trend_bars, write_universe,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import TRIAL_FAILED
from trader.research.eligibility import EligibilityEvidence, Ruleset
from trader.research.evaluation import (
    EvaluationError, EvaluationPaths, PointResult, _family, evaluate,
)
from trader.research.evaluation_report import write_evaluation_report
from trader.research.evaluation_spec import load_evaluation_spec
from trader.research.evaluation_store import (
    STAGE_COMPLETE, STAGE_PRE_HOLDOUT, EvaluationRepository, describe_latest_evaluation,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.simulation.execution_costs import load_execution_costs_config

PHASE_B_PRE_HOLDOUT = {
    'liquidity_capacity_envelope', 'regime_positive_expectancy_fraction',
    'regime_loss_tolerance', 'regime_transition_stability',
}
CLOCK = [dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)]


def _now():
    CLOCK[0] += dt.timedelta(seconds=1)
    return CLOCK[0]


@pytest.fixture
def workspace(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    db = DuckDBConnection.get_instance(str(tmp_path / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(db))
    paths = EvaluationPaths(history_db=tmp_duckdb_path, universe_db=tmp_duckdb_path,
                            universe_library='Universes', execution_costs=str(costs),
                            repo_root=tmp_path, reports_dir=tmp_path / 'reports',
                            summaries_dir=tmp_path / 'artifacts' / 'evaluations')
    return tmp_path, tmp_duckdb_path, db, paths


def _spec(workspace, **overrides):
    repo, db_path, _, paths = workspace
    from trader.data.universe import UniverseAccessor
    return load_evaluation_spec(
        build_spec_file(repo, **overrides),
        universe_accessor=UniverseAccessor(db_path, 'Universes'),
        costs_config=load_execution_costs_config(paths.execution_costs), repo_root=repo)


@pytest.mark.timeout(240)
def test_phase_a_stops_before_the_holdout(workspace):
    repo, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)

    result = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)

    assert result.stage == STAGE_PRE_HOLDOUT and result.state == 'CANDIDATE'
    assert set(result.missing_rules) == PHASE_B_PRE_HOLDOUT
    assert 'min_round_trips' in result.failed_rules
    assert ExperimentRegistry(db).family_artifacts(result.family_id) == []
    assert result.report_path.exists() and result.report_path.with_suffix('.json').exists()
    assert EvaluationRepository(db).list()[0].family_id == result.family_id
    assert 'pre_holdout' in describe_latest_evaluation(
        paths.summaries_dir, 'strategies/time_of_day.py', 'TimeOfDay')


@pytest.mark.timeout(240)
def test_losing_strategy_fails_expectancy(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=-0.0006)
    result = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    assert 'expectancy_baseline_positive' in result.failed_rules


@pytest.mark.timeout(240)
def test_interrupted_trial_is_failed_and_a_rerun_resumes(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace)
    first = evaluate(spec, research_db=db, paths=paths, now=_now)
    registry = ExperimentRegistry(db)
    stale = registry.start_trial(
        first.family_id, trial_key='point:{"ENTRY_MINUTE":615,"EXIT_MINUTE":660}#2',
        parameters={'ENTRY_MINUTE': 615, 'EXIT_MINUTE': 660}, started_at=_now())

    second = evaluate(spec, research_db=db, paths=paths, now=_now)

    assert second.family_id == first.family_id
    assert registry.get_trial(stale).status == TRIAL_FAILED


@pytest.mark.timeout(240)
def test_new_param_is_a_new_family_and_the_trial_count_carries_over(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    first = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    registry = ExperimentRegistry(db)
    before = len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay'))

    second = evaluate(_spec(workspace, params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 690}),
                      research_db=db, paths=paths, now=_now)

    assert second.family_id != first.family_id
    assert len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay')) > before


@pytest.mark.timeout(240)
def test_edited_strategy_file_is_a_new_family(workspace):
    repo, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    first = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    strategy = repo / 'strategies' / 'time_of_day.py'
    strategy.write_text(strategy.read_text() + '\n# edited\n')
    second = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now)
    assert second.family_id != first.family_id


def _holdout_ruleset():
    keep = {'expectancy_baseline_positive', 'holdout_drawdown_within_canary',
            'deterministic_replay', 'holdout_opened_once'}
    return Ruleset(name='paper-v1-subset', version='test',
                   rules=tuple(r for r in PAPER_V1.rules if r.code in keep), source_digest='test')


@pytest.mark.timeout(240)
def test_passing_gate_opens_the_holdout_once(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace)

    result = evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())

    assert result.stage == STAGE_COMPLETE and result.state == 'PAPER_ELIGIBLE'
    assert ExperimentRegistry(db).get_artifact(result.artifact_id).holdout_opened
    assert result.decision_digest
    with pytest.raises(EvaluationError, match='holdout already opened'):
        evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())


def test_report_serializes_an_infinite_profit_factor(workspace):
    _, _, _, paths = workspace
    point = PointResult(params={'A': 1}, trial_id='t', metrics={}, outcomes={})
    path = write_evaluation_report(
        paths.reports_dir, spec=_spec(workspace), family_id='f', stage=STAGE_PRE_HOLDOUT,
        state='CANDIDATE', artifact_id=None, failed=('profit_factor_after_costs',), missing=(),
        evidence=EligibilityEvidence(profit_factor=float('inf')), main=point, neighbours=[],
        created_at=_now())
    data = json.loads(path.with_suffix('.json').read_text())
    assert data['evidence']['profit_factor'] == 'inf'


def test_uncommitted_strategy_is_refused_before_the_data_is_loaded(workspace):
    repo, _, db, paths = workspace
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
    spec = _spec(workspace)  # no bars were written; loading them would raise a different error

    with pytest.raises(EvaluationError, match='not committed'):
        evaluate(spec, research_db=db, paths=paths, now=_now)


def _family_id(workspace) -> str:
    return _family(_spec(workspace), 'manifest-1', 'commit-1', workspace[3]).family_id


def test_a_comment_in_the_costs_config_keeps_the_family(workspace):
    before = _family_id(workspace)
    costs = Path(workspace[3].execution_costs)
    costs.write_text('# reviewed 2026-10-04\n' + costs.read_text() + '\n# end\n')

    assert _family_id(workspace) == before


def test_a_real_cost_change_is_a_new_family(workspace):
    before = _family_id(workspace)
    costs = Path(workspace[3].execution_costs)
    config = yaml.safe_load(costs.read_text())
    config['spread_ticks'] = 2.0
    costs.write_text(yaml.safe_dump(config))

    assert _family_id(workspace) != before


def test_the_container_digest_is_not_part_of_the_family(workspace, monkeypatch):
    monkeypatch.setenv('MMR_CONTAINER_DIGEST', 'sha256:image-a')
    first = _family_id(workspace)
    monkeypatch.setenv('MMR_CONTAINER_DIGEST', 'sha256:image-b')

    assert _family_id(workspace) == first


def test_the_report_records_the_container_digest(workspace, monkeypatch):
    _, _, _, paths = workspace
    point = PointResult(params={'A': 1}, trial_id='t', metrics={}, outcomes={})
    path = write_evaluation_report(
        paths.reports_dir, spec=_spec(workspace), family_id='f', stage=STAGE_PRE_HOLDOUT,
        state='CANDIDATE', artifact_id=None, failed=(), missing=(),
        evidence=EligibilityEvidence(), main=point, neighbours=[], created_at=_now(),
        container_digest='sha256:image-a')
    assert json.loads(path.with_suffix('.json').read_text())['container_digest'] == 'sha256:image-a'


def test_two_reports_written_in_the_same_second_both_survive(workspace):
    _, _, _, paths = workspace
    point = PointResult(params={'A': 1}, trial_id='t', metrics={}, outcomes={})
    created_at = _now()
    reports = [write_evaluation_report(
        paths.reports_dir, spec=_spec(workspace), family_id='f', stage=STAGE_PRE_HOLDOUT,
        state='CANDIDATE', artifact_id=None, failed=(), missing=(), evidence=EligibilityEvidence(),
        main=point, neighbours=[], created_at=created_at) for _ in range(2)]

    assert reports[0] != reports[1]
    assert all(r.exists() and r.with_suffix('.json').exists() for r in reports)
