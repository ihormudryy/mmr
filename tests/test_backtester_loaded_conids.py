"""A result says which conids actually had bars, so callers can refuse a partial run."""
import datetime as dt

import pytest

from tests.test_backtest_metrics import _install, _write_bars
from tests.test_backtester_costs import SizedScript
from trader.data.data_access import TickStorage
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.slippage import ZeroSlippage


def _run(duckdb_path, conids):
    cfg = BacktestConfig(
        start_date=dt.datetime(2024, 1, 2, 9, 30, tzinfo=dt.timezone.utc),
        end_date=dt.datetime(2024, 1, 2, 10, 30, tzinfo=dt.timezone.utc),
        bar_size=BarSize.Mins1, initial_capital=100_000.0,
        slippage_model=ZeroSlippage(), commission_per_share=0.0)
    strategy = _install(SizedScript([]), duckdb_path, conids=conids)
    return Backtester(storage=TickStorage(duckdb_path=duckdb_path), config=cfg).run(strategy, conids)


def test_loaded_conids_lists_only_conids_with_bars(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100] * 6, conid=4391)
    _write_bars(tmp_duckdb_path, [50] * 6, conid=265598)
    result = _run(tmp_duckdb_path, [4391, 9999, 265598])
    assert result.loaded_conids == [4391, 265598]


def test_all_conids_missing_still_raises(tmp_duckdb_path):
    with pytest.raises(ValueError, match='no historical data'):
        _run(tmp_duckdb_path, [9999])
