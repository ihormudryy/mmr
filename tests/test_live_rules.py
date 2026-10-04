"""Backtests that mirror live paper automation: entry window, caps, halts,
and the end-of-day flatten."""
import datetime as dt

import pandas as pd
import pytest

from tests.test_backtest_metrics import _install
from tests.test_backtester_costs import SizedScript
from trader.data.data_access import TickStorage
from trader.data.duckdb_store import DuckDBDataStore
from trader.objects import Action, BarSize
from trader.simulation.backtester import Backtester, BacktestConfig
from trader.simulation.live_rules import PaperAutomationRules
from trader.simulation.slippage import ZeroSlippage

UTC = dt.timezone.utc
TUESDAY_10_ET = dt.datetime(2024, 1, 2, 15, 0, tzinfo=UTC)


def _rules(max_gross_allocation=0.05, equity=100_000.0):
    rules = PaperAutomationRules(max_gross_allocation=max_gross_allocation)
    rules.reset(equity)
    rules.mark(TUESDAY_10_ET, equity)
    return rules


class TestEntryGates:
    def test_open_market_small_order_is_allowed(self):
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) is None

    def test_first_five_minutes_are_blocked(self):
        ts = dt.datetime(2024, 1, 2, 14, 33, tzinfo=UTC)  # 09:33 ET
        assert _rules().entry_block_reason(ts=ts, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) == 'ENTRY_WINDOW'

    def test_last_thirty_minutes_are_blocked(self):
        ts = dt.datetime(2024, 1, 2, 20, 31, tzinfo=UTC)  # 15:31 ET
        assert _rules().entry_block_reason(ts=ts, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) == 'ENTRY_WINDOW'

    def test_weekend_is_blocked(self):
        ts = dt.datetime(2024, 1, 6, 15, 0, tzinfo=UTC)  # Saturday
        assert _rules().entry_block_reason(ts=ts, conid=1, order_notional=1_900,
                                           position_values={}, equity=100_000) == 'ENTRY_WINDOW'

    def test_daily_loss_halts_entries(self):
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=1, order_notional=1_900,
                                           position_values={}, equity=99_400) == 'DAILY_LOSS'

    def test_drawdown_from_the_high_water_mark_halts_entries(self):
        rules = _rules()
        rules.mark(TUESDAY_10_ET, 103_000)
        wednesday = TUESDAY_10_ET + dt.timedelta(days=1)
        rules.mark(wednesday, 99_900)
        assert rules.entry_block_reason(ts=wednesday, conid=1, order_notional=1_000,
                                        position_values={}, equity=99_900) == 'DRAWDOWN'

    def test_fourth_position_is_blocked(self):
        held = {1: 1_000.0, 2: 1_000.0, 3: 1_000.0}
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=4, order_notional=1_000,
                                           position_values=held, equity=100_000) == 'MAX_POSITIONS'

    def test_position_above_five_percent_is_blocked(self):
        assert _rules().entry_block_reason(ts=TUESDAY_10_ET, conid=1, order_notional=6_000,
                                           position_values={}, equity=100_000) == 'POSITION_PCT'

    def test_gross_above_attested_allocation_is_blocked(self):
        assert _rules(max_gross_allocation=0.02).entry_block_reason(
            ts=TUESDAY_10_ET, conid=2, order_notional=1_000,
            position_values={1: 1_500.0}, equity=100_000) == 'GROSS'


class TestFlatten:
    def test_flatten_starts_fifteen_minutes_before_the_close(self):
        rules = _rules()
        assert not rules.flatten_due(dt.datetime(2024, 1, 2, 20, 44, tzinfo=UTC))
        assert rules.flatten_due(dt.datetime(2024, 1, 2, 20, 45, tzinfo=UTC))


def _write_session_bars(duckdb_path, price=100.0, conid=4391):
    index = pd.date_range('2024-01-02 09:30', periods=390, freq='1min',
                          tz='America/New_York').tz_convert('UTC')
    frame = pd.DataFrame({'open': price, 'high': price + 0.01, 'low': price - 0.01,
                          'close': price, 'volume': 10_000.0}, index=index)
    frame.index.name = 'date'
    DuckDBDataStore(duckdb_path).write(str(conid), frame)


def _session_run(duckdb_path, steps):
    config = BacktestConfig(
        start_date=dt.datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        end_date=dt.datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        bar_size=BarSize.Mins1, initial_capital=100_000.0,
        slippage_model=ZeroSlippage(), commission_per_share=0.0,
        live_rules=PaperAutomationRules(max_gross_allocation=0.05))
    strategy = _install(SizedScript(steps), duckdb_path)
    return Backtester(storage=TickStorage(duckdb_path=duckdb_path), config=config).run(strategy, [4391])


def test_backtest_blocks_early_entry_and_flattens_before_close(tmp_duckdb_path):
    _write_session_bars(tmp_duckdb_path)
    steps = [(Action.BUY, 10)] + [None] * 9 + [(Action.BUY, 10)] + [None] * 379

    result = _session_run(tmp_duckdb_path, steps)

    assert result.live_rule_blocks == {'ENTRY_WINDOW': 1}
    buy, flatten = result.trades
    assert buy.action == Action.BUY
    assert pd.Timestamp(buy.timestamp).tz_convert('America/New_York').strftime('%H:%M') == '09:41'
    assert flatten.action == Action.SELL and flatten.quantity == 10
    assert pd.Timestamp(flatten.timestamp).tz_convert('America/New_York').strftime('%H:%M') == '15:45'


def test_explicit_quantity_above_the_position_cap_is_blocked(tmp_duckdb_path):
    _write_session_bars(tmp_duckdb_path)
    steps = [None] * 10 + [(Action.BUY, 60)] + [None] * 379  # 6,000 = 6% of equity

    result = _session_run(tmp_duckdb_path, steps)

    assert result.trades == []
    assert result.live_rule_blocks == {'POSITION_PCT': 1}
