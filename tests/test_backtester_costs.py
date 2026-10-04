"""The backtester charges what its execution-cost model says, and the cost
model only sees bars that were known when the order was decided."""

import datetime as dt

import pandas as pd
import pytest

from tests.test_backtest_metrics import _install, _write_bars
from trader.data.data_access import TickStorage
from trader.objects import Action, BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.execution_costs import (
    CommissionSchedule,
    RealisticCosts,
    TickTable,
    Venue,
)
from trader.trading.strategy import Signal, Strategy


class SizedScript(Strategy):
    """Emits ``(action, quantity)`` steps (or None) one bar at a time."""

    def __init__(self, steps):
        super().__init__()
        self._steps = list(steps)

    def on_prices(self, prices):
        step = self._steps.pop(0) if self._steps else None
        if step is None:
            return None
        action, quantity = step
        return Signal(source_name='sized', action=action, probability=0.5,
                      risk=0.5, quantity=quantity)


class RecordingCosts:
    """Zero-cost model that remembers the reference bar of every fill."""

    name = 'recording'

    def __init__(self):
        self.reference_bars = []

    def fill_price(self, conid, price, quantity, action, reference_bar):
        self.reference_bars.append(reference_bar)
        return price

    def commission(self, conid, quantity, price):
        return 0.0

    def scaled(self, multiplier):
        return self


def _backtester(duckdb_path, cost_model, fill_policy='next_open'):
    config = BacktestConfig(
        start_date=dt.datetime(2024, 1, 2, 9, 30, tzinfo=dt.timezone.utc),
        end_date=dt.datetime(2024, 1, 2, 10, 30, tzinfo=dt.timezone.utc),
        bar_size=BarSize.Mins1,
        initial_capital=100_000.0,
        cost_model=cost_model,
        fill_policy=fill_policy,
    )
    return Backtester(storage=TickStorage(duckdb_path=duckdb_path), config=config)


def test_next_open_fill_sees_the_signal_bar_not_the_fill_bar(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100, 101, 102, 103, 104])
    costs = RecordingCosts()
    strategy = _install(SizedScript([(Action.BUY, 1), None, None, None, None]), tmp_duckdb_path)

    _backtester(tmp_duckdb_path, costs).run(strategy, [4391])

    signal_bar_time = pd.Timestamp('2024-01-02 09:30', tz='UTC')
    assert [bar.name for bar in costs.reference_bars] == [signal_bar_time]


def test_trades_pay_the_cost_model_commission(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100, 100, 100, 101, 101, 101])
    us = Venue(
        name='us', primary_exchanges=frozenset({'NASDAQ'}),
        commission=CommissionSchedule(per_share=0.005, minimum=1.0, max_pct_of_value=0.01),
        ticks=TickTable([(0.0, 0.0001), (1.0, 0.01)]),
    )
    costs = RealisticCosts(venue_by_conid={4391: us}, spread_ticks=1.0,
                           min_half_spread_bps=0.5, impact_k=0.0)
    steps = [(Action.BUY, 10), None, (Action.SELL, 10), None, None, None]
    strategy = _install(SizedScript(steps), tmp_duckdb_path)

    result = _backtester(tmp_duckdb_path, costs).run(strategy, [4391])

    assert [t.commission for t in result.trades] == pytest.approx([1.0, 1.0])
    buy, sell = result.trades
    assert buy.action == Action.BUY and buy.price == pytest.approx(100.0 * 1.00005)
    # Half a tick at 101 is 0.495 bps, so the 0.5 bps floor applies.
    assert sell.action == Action.SELL and sell.price == pytest.approx(101.0 * 0.99995)
