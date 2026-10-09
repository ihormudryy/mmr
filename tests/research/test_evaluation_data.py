import datetime as dt

import pandas as pd
import pytest

from tests.research.evaluation_fixtures import (
    CONIDS, build_spec_file, write_alpaca_extended_hours_bar, write_benchmark_bars, write_costs_config,
    write_trend_bars, write_universe,
)
from trader.data.duckdb_store import DuckDBDataStore
from trader.data.universe import UniverseAccessor
from trader.research.evaluation_data import (
    BarsMissing, EvaluationDataError, load_bars, load_benchmark_closes, qualify_dataset, require_bars_available,
)
from trader.research.market_context import BENCHMARK_CONID
from trader.research.evaluation_spec import load_evaluation_spec
from trader.simulation.execution_costs import load_execution_costs_config


def _spec(tmp_path, tmp_duckdb_path):
    costs = write_costs_config(tmp_path / 'execution_costs.yaml')
    return load_evaluation_spec(
        build_spec_file(tmp_path),
        universe_accessor=UniverseAccessor(tmp_duckdb_path, 'Universes'),
        costs_config=load_execution_costs_config(str(costs)), repo_root=tmp_path)


def test_benchmark_closes_cover_the_lookback(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_benchmark_bars(tmp_duckdb_path)
    spec = _spec(tmp_path, tmp_duckdb_path)
    closes = load_benchmark_closes(tmp_duckdb_path, spec)
    before = [d for d in closes.index if d < spec.period_start]
    assert len(before) >= 220
    assert all(isinstance(d, dt.date) for d in closes.index)


def test_missing_spy_names_the_download_command(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    spec = _spec(tmp_path, tmp_duckdb_path)
    with pytest.raises(EvaluationDataError, match=r'mmr data download SPY --bar-size "1 day"'):
        load_benchmark_closes(tmp_duckdb_path, spec)


def test_short_spy_history_names_the_download_command(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_benchmark_bars(tmp_duckdb_path, start='2024-01-02')
    spec = _spec(tmp_path, tmp_duckdb_path)
    with pytest.raises(EvaluationDataError, match=r'220.*mmr data download SPY --bar-size "1 day"'):
        load_benchmark_closes(tmp_duckdb_path, spec)


def test_duplicate_spy_sessions_are_refused(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_benchmark_bars(tmp_duckdb_path)
    spec = _spec(tmp_path, tmp_duckdb_path)
    duplicate_day = spec.period_start
    DuckDBDataStore(tmp_duckdb_path).write(str(BENCHMARK_CONID), pd.DataFrame(
        {'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 999.0, 'volume': 1.0, 'bar_size': '1 day'},
        index=pd.DatetimeIndex([pd.Timestamp(duplicate_day, tz='UTC') + pd.Timedelta(hours=12)],
                               name='date')))
    with pytest.raises(EvaluationDataError, match=rf'756733.*{duplicate_day.isoformat()}.*re-download'):
        load_benchmark_closes(tmp_duckdb_path, spec)


def test_full_history_passes_the_bars_check(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0)
    require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_a_conid_without_bars_is_named(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0, conids=CONIDS[:-1])
    with pytest.raises(BarsMissing, match=rf'conid {CONIDS[-1]}: no regular-session 15 mins bars on 2024-02-01'):
        require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_bars_that_stop_before_the_period_end_are_stale(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0, end='2024-03-27')
    with pytest.raises(BarsMissing, match=r'no regular-session 15 mins bars on 2024-03-28 \(period end\)'):
        require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_bars_that_start_after_the_period_start_are_missing(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0, start='2024-02-05')
    with pytest.raises(BarsMissing, match=r'no regular-session 15 mins bars on 2024-02-01 \(period start\)'):
        require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_daily_bars_never_count_as_intraday_bars(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_benchmark_bars(tmp_duckdb_path)
    days = pd.DatetimeIndex(pd.bdate_range('2024-01-02', '2024-03-28', tz='UTC'), name='date')
    for conid in CONIDS:
        DuckDBDataStore(tmp_duckdb_path).write(str(conid), pd.DataFrame(
            {'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0, 'bar_size': '1 day'}, index=days))
    with pytest.raises(BarsMissing, match=r'no regular-session 15 mins bars'):
        require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_short_spy_daily_history_is_missing(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0)
    store = DuckDBDataStore(tmp_duckdb_path)
    store.delete(str(BENCHMARK_CONID))
    write_benchmark_bars(tmp_duckdb_path, start='2024-01-02')
    with pytest.raises(BarsMissing, match=r'SPY \(conid 756733\): 1 day bars start 2024-01-02'):
        require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_an_extended_hours_bar_does_not_make_stale_bars_fresh(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0, end='2024-03-27')
    write_alpaca_extended_hours_bar(tmp_duckdb_path, '2024-03-28')
    with pytest.raises(BarsMissing, match=r'no regular-session 15 mins bars on 2024-03-28 \(period end\)'):
        require_bars_available(tmp_duckdb_path, _spec(tmp_path, tmp_duckdb_path))


def test_extended_hours_bars_are_dropped_before_qualification(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0)
    for day in ('2024-02-01', '2024-02-15', '2024-03-28'):
        write_alpaca_extended_hours_bar(tmp_duckdb_path, day)
    write_alpaca_extended_hours_bar(tmp_duckdb_path, '2024-03-28', ny_time=dt.time(16, 0))   # post-market
    spec = _spec(tmp_path, tmp_duckdb_path)
    require_bars_available(tmp_duckdb_path, spec)
    bars = load_bars(tmp_duckdb_path, spec.conids, spec.bar_size, dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc),
                     dt.datetime(2024, 3, 29, tzinfo=dt.timezone.utc), calendar_name=spec.calendar)
    local = bars[CONIDS[0]].index.tz_convert('America/New_York')
    assert local.time.min() == dt.time(9, 30) and local.time.max() == dt.time(15, 45)
    assert qualify_dataset(bars, spec).research_eligible
