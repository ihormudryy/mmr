"""Characterize cash-weighted PF vs dollar-weighted expectancy.

All metrics come from Backtester.run with real temporary DuckDB bars and
next-open fills. These are synthetic accounting fixtures, not strategy evidence.
"""

import datetime as dt

import pandas as pd
import pytest

from trader.data.duckdb_store import DuckDBDataStore
from trader.objects import Action, BarSize
from trader.simulation.backtester import BacktestConfig, Backtester
from trader.simulation.slippage import ZeroSlippage
from trader.trading.strategy import Signal, Strategy


class ScheduledOrders(Strategy):
    """Emit a prescribed (action, quantity) on each bar, without pricing logic."""

    def __init__(self, orders):
        super().__init__()
        self.orders = orders

    def on_prices(self, prices):
        index = len(prices) - 1
        if index >= len(self.orders):
            return None
        action, quantity = self.orders[index]
        name, conids = self.name, self.conids
        assert name is not None and conids is not None
        return Signal(
            source_name=name,
            action=action,
            quantity=quantity,
            conid=conids[0],
            probability=1.0,
            risk=0.0,
        )


@pytest.fixture
def run_fills(tmp_duckdb_path, make_strategy_context):
    def run(fills, *, commission_per_share=0.01, final_close=None):
        # 4391 is only a local synthetic storage key; no contract resolution.
        conid = 4391
        opens = [fills[0][2]] + [price for _, _, price in fills]
        closes = opens.copy()
        if final_close is not None:
            closes[-1] = final_close
        start = dt.datetime(2024, 1, 2, 9, 30, tzinfo=dt.timezone.utc)
        dates = pd.date_range(start, periods=len(opens), freq="1min")
        bars = pd.DataFrame({
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes)],
            "low": [min(o, c) for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [10_000.0] * len(opens),
        }, index=dates)
        bars.index.name = "date"
        DuckDBDataStore(tmp_duckdb_path).write(str(conid), bars)
        context = make_strategy_context(name="metric_semantics", conids=[conid])
        strategy = ScheduledOrders([(action, qty) for action, qty, _ in fills])
        strategy.install(context)
        strategy.enable()
        config = BacktestConfig(
            start_date=start,
            end_date=start + dt.timedelta(minutes=len(opens) - 1),
            initial_capital=100_000.0,
            bar_size=BarSize.Mins1,
            fill_policy="next_open",
            slippage_model=ZeroSlippage(),
            commission_per_share=commission_per_share,
        )
        result = Backtester(context.storage, config).run(strategy, [conid])

        # Verify the intended ledger really executed, rather than accepting
        # plausible metrics after a missing, clipped, or mistimed fill.
        assert result.total_trades == len(fills)
        for trade, (action, quantity, price), timestamp in zip(
            result.trades, fills, dates[1:]
        ):
            assert trade.conid == conid
            assert trade.action == action
            assert trade.quantity == pytest.approx(quantity)
            assert trade.price == pytest.approx(price)
            assert trade.commission == pytest.approx(quantity * commission_per_share)
            assert trade.timestamp == timestamp
        return result

    return run


@pytest.mark.parametrize(
    "win_qty,win_exit,loss_qty,loss_exit,net_win,net_loss",
    [
        pytest.param(100, 102, 10, 95, 198, -50.2, id="large-winner-small-loser"),
        pytest.param(10, 105, 100, 98, 49.8, -202, id="small-winner-large-loser"),
    ],
)
def test_varying_quantities_keep_expectancy_sign_with_cash_pnl(
    run_fills, win_qty, win_exit, loss_qty, loss_exit, net_win, net_loss,
):
    result = run_fills([
        (Action.BUY, win_qty, 100),
        (Action.SELL, win_qty, win_exit),
        (Action.BUY, loss_qty, 100),
        (Action.SELL, loss_qty, loss_exit),
    ])
    entry_notional = (win_qty + loss_qty) * 100
    assert result.profit_factor == pytest.approx(net_win / -net_loss)
    assert result.expectancy_bps == pytest.approx(
        (net_win + net_loss) / entry_notional * 10_000)
    assert result.total_return == pytest.approx((net_win + net_loss) / 100_000)
    assert (result.profit_factor > 1) == (result.expectancy_bps > 0)
    assert (result.total_return > 0) == (result.expectancy_bps > 0)


def test_fixed_share_count_does_not_mean_fixed_entry_notional(run_fills):
    result = run_fills([
        (Action.BUY, 10, 100),
        (Action.SELL, 10, 102),
        (Action.BUY, 10, 10),
        (Action.SELL, 10, 9.5),
    ])
    # Entry notionals are 1000 and 100, despite every fill being 10 shares.
    assert result.profit_factor == pytest.approx(19.8 / 5.2)
    assert result.expectancy_bps == pytest.approx(14.6 / 1100 * 10_000)
    assert result.total_return == pytest.approx(14.6 / 100_000)


@pytest.mark.parametrize(
    "second_exit,expected_pf,expected_bps,net_pnl",
    [(49.5, 2, 50, 10), (49, 1, 0, 0), (48.5, 2 / 3, -50, -10)],
)
def test_equal_entry_notionals_agree_on_profitability(
    run_fills, second_exit, expected_pf, expected_bps, net_pnl,
):
    result = run_fills([
        (Action.BUY, 10, 100),
        (Action.SELL, 10, 102),
        (Action.BUY, 20, 50),
        (Action.SELL, 20, second_exit),
    ], commission_per_share=0)
    # Both fully closed entries have the same 1000 notional, not same shares.
    assert result.profit_factor == pytest.approx(expected_pf)
    assert result.expectancy_bps == pytest.approx(expected_bps)
    assert result.total_return == pytest.approx(net_pnl / 100_000)


@pytest.mark.parametrize(
    "commission,expected_pf,expected_bps,net_pnl",
    [(0, float("inf"), 5, 0.5), (0.03, 0, -1, -0.1)],
)
def test_both_commissions_can_turn_price_gain_into_net_loss(
    run_fills, commission, expected_pf, expected_bps, net_pnl,
):
    result = run_fills([
        (Action.BUY, 10, 100),
        (Action.SELL, 10, 100.05),
    ], commission_per_share=commission)
    # Gross gain 0.50; with fees, 0.30 entry + 0.30 exit => net loss 0.10.
    # The denominator is the 1000 entry fill notional, excluding commissions.
    assert result.profit_factor == pytest.approx(expected_pf)
    assert result.expectancy_bps == pytest.approx(expected_bps)
    assert result.total_return == pytest.approx(net_pnl / 100_000)


def test_partial_exits_are_weighted_by_quantity(run_fills):
    result = run_fills([
        (Action.BUY, 100, 100),
        (Action.SELL, 90, 102),
        (Action.SELL, 10, 95),
    ])
    # P&Ls: 180 - 0.90 - 0.90 = 178.20; -50 - 0.10 - 0.10 = -50.20.
    # Dollar-weighted: (178.2 - 50.2) / (9000 + 1000).
    assert result.profit_factor == pytest.approx(178.2 / 50.2)
    assert result.expectancy_bps == pytest.approx(128)
    assert result.total_return == pytest.approx(128 / 100_000)


def test_splitting_same_price_exits_changes_neither_expectancy_nor_pf(run_fills):
    combined = run_fills([
        (Action.BUY, 100, 100),
        (Action.SELL, 90, 102),
        (Action.SELL, 10, 95),
    ])
    split = run_fills([
        (Action.BUY, 100, 100),
        *[(Action.SELL, 10, 102)] * 9,
        (Action.SELL, 10, 95),
    ])
    # Same fills by quantity/price, same proportional fees and same cash P&L.
    # Dollar weighting makes exit segmentation irrelevant.
    assert split.total_return == pytest.approx(combined.total_return)
    assert split.profit_factor == pytest.approx(combined.profit_factor)
    assert combined.expectancy_bps == pytest.approx(128)
    assert split.expectancy_bps == pytest.approx(128)


def test_weighted_entry_survives_partial_exit_addition_and_flat_reset(run_fills):
    result = run_fills([
        (Action.BUY, 10, 100),
        (Action.BUY, 30, 120),
        (Action.SELL, 10, 117),  # avg entry 115, net P&L +19.8
        (Action.BUY, 10, 95),   # remaining 30 @ 115 + 10 @ 95 => avg 110
        (Action.SELL, 30, 109), # net P&L -30.6
        (Action.SELL, 10, 113), # net P&L +29.8, now flat
        (Action.BUY, 10, 50),
        (Action.SELL, 10, 49),  # fresh entry 50, net P&L -10.2
    ])
    assert result.profit_factor == pytest.approx((19.8 + 29.8) / (30.6 + 10.2))
    net_pnls = [19.8, -30.6, 29.8, -10.2]
    entry_notionals = [1150, 3300, 1100, 500]
    assert result.expectancy_bps == pytest.approx(sum(net_pnls) / sum(entry_notionals) * 10_000)
    # All shares are closed: realized P&L must reconcile to the cash ledger.
    assert result.total_return == pytest.approx(8.8 / 100_000)


def test_open_profit_affects_return_but_not_closed_sell_metrics(run_fills):
    result = run_fills([
        (Action.BUY, 10, 100),
        (Action.SELL, 10, 99),   # only closed P&L: -10.2 on 1000 notional
        (Action.BUY, 10, 100),  # left open and marked at 110
    ], final_close=110)
    assert result.profit_factor == 0
    assert result.expectancy_bps == pytest.approx(-102)
    # Closed loss + unrealized gain - the open entry fee; no hypothetical exit fee.
    assert result.total_return == pytest.approx(89.7 / 100_000)


def test_no_closed_sells_have_zero_pf_and_expectancy(run_fills):
    result = run_fills([(Action.BUY, 10, 100)], final_close=110)
    assert result.profit_factor == 0
    assert result.expectancy_bps == 0
    assert result.total_return == pytest.approx(99.9 / 100_000)
