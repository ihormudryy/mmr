import datetime as dt
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import exchange_calendars as xcals
import numpy as np
import pandas as pd
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
    _holdout_market_evidence,
    EvaluationError, EvaluationPaths, PointResult, _family, evaluate,
)
from trader.research import market_context as mcx
from trader.research.evaluation_data import load_benchmark_closes
from trader.research.evaluation_report import write_evaluation_report
from trader.research.evaluation_spec import load_evaluation_spec
from trader.research.evaluation_store import (
    STAGE_COMPLETE, STAGE_PRE_HOLDOUT, EvaluationRepository, describe_latest_evaluation,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.simulation.execution_costs import load_execution_costs_config

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
    assert set(result.missing_rules) == {'regime_transition_stability'}
    assert 'min_round_trips' in result.failed_rules
    assert ExperimentRegistry(db).family_artifacts(result.family_id) == []
    assert result.report_path.exists() and result.report_path.with_suffix('.json').exists()
    assert EvaluationRepository(db).list()[0].family_id == result.family_id
    assert 'pre_holdout' in describe_latest_evaluation(
        paths.summaries_dir, 'strategies/time_of_day.py', 'TimeOfDay')
    report = json.loads(result.report_path.with_suffix('.json').read_text())
    assert report['evidence']['order_within_envelope'] is True
    assert report['evidence']['eligible_regime_positive_fraction'] == 1.0
    assert report['missing_causes']['regime_transition_stability'] == (
        'no regime change in the walk-forward sessions')
    assert report['market_context']['liquidity']['rows'][0]['floor_median'] > 0
    assert report['market_context']['regimes']['n_changes'] == 0
    markdown = result.report_path.read_text()
    assert '## Market context' in markdown and '## Missing evidence causes' in markdown


@pytest.mark.timeout(240)
def test_an_oversized_order_fails_the_liquidity_rule(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace, sizing={'order_notional': 10_000_000, 'account_equity': 100_000_000})
    result = evaluate(spec, research_db=db, paths=paths, now=_now)
    assert 'liquidity_capacity_envelope' in result.failed_rules


@pytest.mark.timeout(240)
def test_the_holdout_records_the_benchmark_evidence(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    result = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now,
                      ruleset=_holdout_ruleset())
    report = json.loads(result.report_path.with_suffix('.json').read_text())
    ev = report['evidence']
    assert isinstance(ev['benchmark_drawdown_ratio'], float)
    assert 'benchmark_relative_drawdown' not in report['missing_causes']
    assert 0 <= ev['strategy_time_in_market'] <= 1
    assert ev['benchmark_return'] is not None
    assert report['market_context']['benchmark']['raw_spy_return'] is not None
    assert '### Benchmark' in result.report_path.read_text()


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


def test_new_spy_data_is_a_new_family(workspace):
    from trader.research.evaluation_data import load_bars, qualify_dataset
    _, db_path, _, _ = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace)
    bars = load_bars(db_path, spec.conids, spec.bar_size,
                     dt.datetime.combine(spec.period_start, dt.time.min, tzinfo=dt.timezone.utc),
                     dt.datetime.combine(spec.period_end, dt.time.max, tzinfo=dt.timezone.utc))
    closes = load_benchmark_closes(db_path, spec)

    sealed = qualify_dataset(bars, spec, benchmark_closes=closes)
    changed = closes.copy()
    changed.iloc[-1] = changed.iloc[-1] * 1.01

    assert any(f.path == 'tick_data/756733/1 day' for f in sealed.files)
    assert qualify_dataset(bars, spec, benchmark_closes=closes).digest == sealed.digest
    assert qualify_dataset(bars, spec, benchmark_closes=changed).digest != sealed.digest
    assert qualify_dataset(bars, spec).digest != sealed.digest


def test_regime_definition_is_part_of_the_family(workspace):
    before = _family_id(workspace)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcx, 'TREND_SMA_SESSIONS', 150)
        assert _family_id(workspace) != before


@pytest.mark.timeout(240)
def test_same_strategy_overlapping_holdout_is_refused_before_any_trial(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace)
    evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())
    other = _spec(workspace, params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 690})  # new family
    registry = ExperimentRegistry(db)
    trials_before = len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay'))

    with pytest.raises(EvaluationError, match='already opened a holdout'):
        evaluate(other, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())

    assert len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay')) == trials_before


@pytest.mark.timeout(240)
def test_a_benchmark_failure_does_not_spend_the_holdout(workspace, monkeypatch):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace)

    def broken(*args, **kwargs):
        raise mcx.MarketContextError('benchmark broke')

    with monkeypatch.context() as patch:
        patch.setattr(mcx, 'vol_matched_benchmark', broken)
        with pytest.raises(EvaluationError, match='benchmark broke'):
            evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())

    registry = ExperimentRegistry(db)
    family_id = EvaluationRepository(db).list()
    assert family_id == []  # nothing recorded for the failed run
    families = db.transaction(lambda c: c.execute(
        'SELECT family_id FROM experiment_families').fetchall())
    for (fid,) in families:
        assert all(not a.holdout_opened for a in registry.family_artifacts(fid))

    result = evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())
    assert result.stage == STAGE_COMPLETE
    assert ExperimentRegistry(db).get_artifact(result.artifact_id).holdout_opened


def test_holdout_session_one_keeps_its_spy_return(monkeypatch):
    sessions = [s.date() for s in xcals.get_calendar('XNYS').sessions_in_range(
        '2024-03-01', '2024-03-20')]
    holdout = sessions[-5:]
    spy_closes = pd.Series(400.0 + np.arange(len(sessions)), index=pd.Index(sessions))
    equity = tuple((pd.Timestamp(day, tz='UTC') + pd.Timedelta(hours=20), 100_000.0)
                   for day in holdout)
    outcome = SimpleNamespace(trades=(), equity_series=lambda: pd.Series(
        [v for _, v in equity], index=pd.DatetimeIndex([t for t, _ in equity])))
    received = {}

    def capture(strategy_equity, spy, **kwargs):
        received['spy'] = spy
        return mcx.BenchmarkEvidence(mcx.Measured(None, 'captured'))

    monkeypatch.setattr(mcx, 'vol_matched_benchmark', capture)
    _holdout_market_evidence(SimpleNamespace(account_equity=100_000.0, calendar='XNYS'),
                             spy_closes, outcome, holdout[0], holdout[-1], sessions)

    spy = received['spy']
    assert spy.index[0] == sessions[-6]                 # last close before the holdout
    assert len(spy.pct_change().dropna()) == len(holdout)
