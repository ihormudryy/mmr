"""A longer run must replay a shorter run exactly up to the shorter run's end (SP2c shadow replay, issue #96).

Bars of several conids share each timestamp. With the gross cap admitting only some of a bar's signals, the
order of those conids decides which trade fills, so it must not depend on how many bars the run loads.
"""
import datetime as dt

import pandas as pd
import pytest

from tests.research.evaluation_fixtures import CONIDS, TIME_OF_DAY_STRATEGY, write_trend_bars, write_universe
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.live_rules import PaperAutomationRules

UTC = dt.timezone.utc
START = dt.datetime(2024, 3, 11, tzinfo=UTC)
SHORT_END = dt.datetime(2024, 3, 18, 23, 59, tzinfo=UTC)


@pytest.fixture
def run(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    strategy = tmp_path / "time_of_day.py"
    strategy.write_text(TIME_OF_DAY_STRATEGY)

    def _run(end):
        config = BacktestConfig(start_date=START, end_date=end, initial_capital=100_000.0,
                                bar_size=BarSize.parse_str("15 mins"), order_notional=1900.0,
                                live_rules=PaperAutomationRules(max_gross_allocation=0.05))
        return Backtester(TickStorage(tmp_duckdb_path), config).run_from_module(
            str(strategy), "TimeOfDay", CONIDS, universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
            params={"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660})
    return _run


def _trades(result, until):
    return [(pd.Timestamp(t.timestamp), t.conid, str(t.action), t.quantity, t.price, t.commission)
            for t in result.trades if pd.Timestamp(t.timestamp) <= pd.Timestamp(until)]


@pytest.mark.timeout(120)
@pytest.mark.parametrize("long_end", [dt.datetime(2024, 3, 19, 23, 59, tzinfo=UTC),
                                      dt.datetime(2024, 3, 28, 23, 59, tzinfo=UTC)])
def test_a_longer_run_repeats_the_shorter_run_up_to_its_end(run, long_end):
    short, long = run(SHORT_END), run(long_end)
    assert _trades(short, SHORT_END) and len({t[1] for t in _trades(short, SHORT_END)}) < len(CONIDS)
    assert _trades(long, SHORT_END) == _trades(short, SHORT_END)
    pd.testing.assert_series_equal(long.equity_curve[long.equity_curve.index <= SHORT_END], short.equity_curve)
