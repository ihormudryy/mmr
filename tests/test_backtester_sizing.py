"""Fixed order notional: a BUY without a quantity buys floor(notional / price)."""
import datetime as dt

from tests.test_backtest_metrics import _install, _write_bars
from tests.test_backtester_costs import SizedScript
from trader.data.data_access import TickStorage
from trader.objects import Action, BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.slippage import ZeroSlippage


def _run(duckdb_path, steps, **config):
    cfg = BacktestConfig(
        start_date=dt.datetime(2024, 1, 2, 9, 30, tzinfo=dt.timezone.utc),
        end_date=dt.datetime(2024, 1, 2, 10, 30, tzinfo=dt.timezone.utc),
        bar_size=BarSize.Mins1, initial_capital=100_000.0,
        slippage_model=ZeroSlippage(), commission_per_share=0.0, **config)
    strategy = _install(SizedScript(steps), duckdb_path)
    return Backtester(storage=TickStorage(duckdb_path=duckdb_path), config=cfg).run(strategy, [4391])


def test_buy_without_quantity_uses_order_notional(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100, 100, 100, 101, 101, 101])
    result = _run(tmp_duckdb_path, [(Action.BUY, 0), None, (Action.SELL, 0), None, None, None],
                  order_notional=1_950.0)
    assert [t.quantity for t in result.trades] == [19, 19]


def test_explicit_quantity_wins_over_order_notional(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100] * 6)
    result = _run(tmp_duckdb_path, [(Action.BUY, 5), None, None, None, None, None],
                  order_notional=1_950.0)
    assert result.trades[0].quantity == 5


def test_without_order_notional_buys_ten_percent_of_cash(tmp_duckdb_path):
    _write_bars(tmp_duckdb_path, [100] * 6)
    result = _run(tmp_duckdb_path, [(Action.BUY, 0), None, None, None, None, None])
    assert result.trades[0].quantity == 100
