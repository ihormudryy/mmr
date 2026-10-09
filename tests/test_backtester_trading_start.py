import datetime as dt

import pandas as pd
import pytest

from tests.research.evaluation_fixtures import CONIDS, TIME_OF_DAY_STRATEGY, write_trend_bars, write_universe
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestConfig

UTC = dt.timezone.utc
START = dt.datetime(2024, 3, 18, tzinfo=UTC)
TRADING_START = dt.datetime(2024, 3, 20, tzinfo=UTC)
END = dt.datetime(2024, 3, 22, 23, 59, tzinfo=UTC)


@pytest.fixture
def run(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    strategy = tmp_path / "time_of_day.py"
    strategy.write_text(TIME_OF_DAY_STRATEGY)

    def _run(trading_start, params):
        config = BacktestConfig(start_date=START, end_date=END, initial_capital=100_000.0,
                                bar_size=BarSize.parse_str("15 mins"), order_notional=1900.0,
                                trading_start=trading_start)
        return Backtester(TickStorage(tmp_duckdb_path), config).run_from_module(
            str(strategy), "TimeOfDay", CONIDS[:2], universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
            params=params)
    return _run


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts


def test_warm_up_books_no_fill_no_cost_and_equity_starts_at_initial_capital(run):
    result = run(TRADING_START, {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660})
    assert result.trades and all(_utc(t.timestamp) >= pd.Timestamp(TRADING_START) for t in result.trades)
    assert _utc(result.equity_curve.index[0]) >= pd.Timestamp(TRADING_START)
    assert result.equity_curve.iloc[0] == 100_000.0


def test_a_signal_on_the_last_warm_up_bar_never_fills_at_the_first_trading_open(run):
    result = run(TRADING_START, {"ENTRY_MINUTE": 945, "EXIT_MINUTE": 600})   # BUY on every 15:45 bar
    first_day = {_utc(t.timestamp).tz_convert("America/New_York").date() for t in result.trades}
    assert dt.date(2024, 3, 20) not in first_day                              # 03-19 15:45 BUY was dropped


def test_without_trading_start_the_warm_up_days_trade(run):
    days = {_utc(t.timestamp).tz_convert("America/New_York").date()
            for t in run(None, {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}).trades}
    assert dt.date(2024, 3, 18) in days


def test_a_naive_trading_start_is_refused():
    with pytest.raises(ValueError):
        BacktestConfig(trading_start=dt.datetime(2024, 3, 20))


def test_without_trading_start_the_result_is_unchanged(run):
    # Values recorded from the backtester before trading_start existed.
    result = run(None, {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660})
    assert len(result.trades) == 20
    assert len(result.equity_curve) == 130
    assert result.equity_curve.iloc[-1] == pytest.approx(100038.7765192101, rel=1e-12)
    assert result.total_return == pytest.approx(0.0003877651921011527, rel=1e-12)
    assert sum(t.commission for t in result.trades) == pytest.approx(1.03)
    assert result.trades[0].timestamp == pd.Timestamp("2024-03-18 10:15:00-04:00")
    assert result.trades[0].price == pytest.approx(162.47507724546855, rel=1e-12)
    # Same-timestamp bars go in conid order since issue #96: the last trade is the 1002 SELL, not the 1001 one.
    assert (result.trades[-1].conid, str(result.trades[-1].action)) == (1002, "SELL")
    assert result.trades[-1].price == pytest.approx(190.6454417050304, rel=1e-12)


def test_a_trading_start_before_the_first_bar_changes_nothing(run):
    params = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
    baseline = run(None, params)
    early = run(dt.datetime(2000, 1, 1, tzinfo=UTC), params)
    assert early.trades == baseline.trades
    pd.testing.assert_series_equal(early.equity_curve, baseline.equity_curve)


def test_warm_up_bars_feed_the_strategy_state(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    strategy = tmp_path / "bar_counter.py"
    strategy.write_text(
        "from trader.objects import Action\n"
        "from trader.trading.strategy import Signal, Strategy\n\n\n"
        "class BarCounter(Strategy):\n"
        "    def on_prices(self, prices):\n"
        "        if len(prices) == 60:\n"
        "            return Signal(source_name='bar_counter', action=Action.BUY, probability=0.5, risk=0.5)\n"
        "        return None\n")
    config = BacktestConfig(start_date=START, end_date=END, initial_capital=100_000.0,
                            bar_size=BarSize.parse_str("15 mins"), order_notional=1900.0,
                            trading_start=TRADING_START)
    result = Backtester(TickStorage(tmp_duckdb_path), config).run_from_module(
        str(strategy), "BarCounter", CONIDS[:1], universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"))
    # 26 bars a day: the 60th bar is on the third day (03-20), which only exists if warm-up bars were counted.
    assert [_utc(t.timestamp).tz_convert("America/New_York").date() for t in result.trades] == [dt.date(2024, 3, 20)]
