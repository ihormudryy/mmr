import datetime as dt

import pandas as pd
import pytest

from tests.research.evaluation_fixtures import (
    build_spec_file, write_benchmark_bars, write_costs_config, write_universe,
)
from trader.data.duckdb_store import DuckDBDataStore
from trader.data.universe import UniverseAccessor
from trader.research.evaluation_data import EvaluationDataError, load_benchmark_closes
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
