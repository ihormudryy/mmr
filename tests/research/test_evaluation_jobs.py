import datetime as dt
from dataclasses import replace

import pytest

from tests.research.evaluation_fixtures import (
    CONIDS, build_spec_file, write_alpaca_extended_hours_bar, write_costs_config, write_trend_bars, write_universe,
)
from trader.research.evaluation_data import EvaluationDataError, load_bars, qualify_dataset
from trader.objects import BarSize
from trader.research.evaluation import EvaluationPaths, run_environment
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_jobs, run_window_job
from trader.research.evaluation_spec import load_evaluation_spec
from trader.research.regular_sessions import RegularSessionTickData
from trader.simulation.execution_costs import load_execution_costs_config

UTC = dt.timezone.utc
FEB = (dt.datetime(2024, 2, 1, tzinfo=UTC), dt.datetime(2024, 2, 9, 23, 59, tzinfo=UTC))


@pytest.fixture
def market(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    spec_path = build_spec_file(tmp_path)
    return tmp_path, tmp_duckdb_path, costs, spec_path


def _env(market):
    repo, db, costs, _ = market
    return RunEnvironment(
        history_db=db, universe_db=db, universe_library='Universes',
        execution_costs_path=str(costs), strategy_file=str(repo / 'strategies' / 'time_of_day.py'),
        class_name='TimeOfDay', conids=tuple(CONIDS), bar_size='15 mins',
        order_notional=1900.0, account_equity=100_000.0, max_gross_allocation=0.05)


def _job(multiplier=1.0, index=0):
    return WindowJob(point_key='p', params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
                     window_kind='fold', window_index=index, start=FEB[0], end=FEB[1],
                     cost_multiplier=multiplier)


@pytest.mark.timeout(240)
def test_window_job_trades_under_live_rules(market):
    outcome = run_window_job(_env(market), _job())
    assert outcome.net_pnl > 0
    assert len(outcome.trace_signature) == 64
    assert outcome.live_rule_blocks.get('GROSS', 0) > 0  # 8 entries a day, room for 2
    assert all(t['commission'] >= 1.0 for t in outcome.trades)


@pytest.mark.timeout(240)
def test_window_without_bars_for_a_conid_is_refused(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006, conids=CONIDS[:7])
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    build_spec_file(tmp_path)
    market_without_last_conid = (tmp_path, tmp_duckdb_path, costs, None)
    with pytest.raises(EvaluationDataError, match='1008'):
        run_window_job(_env(market_without_last_conid), _job())


@pytest.mark.timeout(240)
def test_higher_costs_never_help(market):
    base, stressed = run_jobs(_env(market), [_job(1.0), _job(2.0)])
    assert stressed.net_pnl < base.net_pnl


@pytest.mark.timeout(240)
def test_process_pool_gives_the_same_trades(market):
    jobs = [_job(1.0, 0), _job(1.5, 1)]
    sequential = run_jobs(_env(market), jobs, max_workers=1)
    pooled = run_jobs(_env(market), jobs, max_workers=2)
    assert [o.trace_signature for o in pooled] == [o.trace_signature for o in sequential]


def test_missing_bars_name_the_conid(market):
    _, db, _, _ = market
    with pytest.raises(EvaluationDataError, match='9999'):
        load_bars(db, CONIDS + [9999], '15 mins', *FEB, calendar_name='XNYS')


def test_qualified_dataset_is_eligible_and_deterministic(market, tmp_duckdb_path):
    repo, db, costs, spec_path = market
    spec = load_evaluation_spec(spec_path, universe_accessor=write_universe(db),
                                costs_config=load_execution_costs_config(str(costs)), repo_root=repo)
    bars = load_bars(db, spec.conids, spec.bar_size,
                     dt.datetime(2024, 2, 1, tzinfo=UTC), dt.datetime(2024, 3, 28, 23, 59, tzinfo=UTC),
                     calendar_name=spec.calendar)
    first = qualify_dataset(bars, spec)
    second = qualify_dataset(bars, spec)
    assert first.research_eligible
    assert first.digest == second.digest


def _worker_logging_threshold() -> int:
    import logging
    return logging.root.manager.disable


def test_spawned_workers_apply_the_parents_logging_switch():
    import logging
    import multiprocessing

    from trader.research.evaluation_jobs import worker_pool

    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with worker_pool(2, mp_context=multiprocessing.get_context('spawn')) as pool:
            assert pool.submit(_worker_logging_threshold).result(timeout=60) == logging.CRITICAL
    finally:
        logging.disable(previous)


@pytest.mark.timeout(240)
def test_window_job_passes_trading_start_to_the_backtest(market):
    trading_start = dt.datetime(2024, 2, 5, tzinfo=UTC)
    job = WindowJob(point_key='p', params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660},
                    window_kind='fold', window_index=0, start=FEB[0], end=FEB[1],
                    cost_multiplier=1.0, trading_start=trading_start)
    outcome = run_window_job(_env(market), job)
    assert outcome.trades and all(t['timestamp'] >= trading_start for t in outcome.trades)
    assert outcome.equity[0][0] >= trading_start


def test_an_evaluation_backtest_reads_only_regular_session_bars(market, monkeypatch):
    import trader.research.evaluation_jobs as jobs
    _, db, _, _ = market
    write_alpaca_extended_hours_bar(db, '2024-02-05')
    seen = {}

    class CapturingBacktester:
        def __init__(self, storage, config):
            seen['bars'] = storage.get_tickdata(config.bar_size).read(CONIDS[0])
            seen['daily'] = storage.get_tickdata(BarSize.parse_str('1 day'))

        def run_from_module(self, *args, **kwargs):
            raise RuntimeError('captured')

    monkeypatch.setattr(jobs, 'Backtester', CapturingBacktester)
    with pytest.raises(RuntimeError, match='captured'):
        run_window_job(replace(_env(market), regular_session_calendar='XNYS'), _job())
    local = seen['bars'].index.tz_convert('America/New_York')
    assert dt.time(8, 0) not in set(local.time) and len(local) > 0
    assert not isinstance(seen['daily'], RegularSessionTickData)          # daily bars are never filtered
    with pytest.raises(RuntimeError, match='captured'):
        run_window_job(_env(market), _job())                                    # shadow replay: unchanged
    assert dt.time(8, 0) in set(seen['bars'].index.tz_convert('America/New_York').time)


def test_the_evaluation_environment_filters_to_the_spec_calendar(market):
    repo, db, costs, spec_path = market
    spec = load_evaluation_spec(spec_path, universe_accessor=write_universe(db),
                                costs_config=load_execution_costs_config(str(costs)), repo_root=repo)
    paths = EvaluationPaths(db, db, 'Universes', str(costs), repo, repo / 'reports', repo / 'evaluations')
    assert run_environment(spec, paths).regular_session_calendar == spec.calendar == 'XNYS'
