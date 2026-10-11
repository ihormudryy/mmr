"""#150: the backtester must hand on_bar only the bars up to the current one."""
from typing import Any, Dict, Optional

import pandas as pd
import pytest

from trader.objects import Action
from trader.trading.strategy import Signal, Strategy
from tests.test_backtester import _install_strategy, _make_backtester, _write_synthetic_bars


class FrameLengthRecorder(Strategy):
    def __init__(self):
        super().__init__()
        self.seen = []

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        self.seen.append((index, len(prices), prices.index[-1]))
        return None


class PeekNextClose(Strategy):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        self.calls += 1
        if prices["close"].iloc[index + 1] > prices["close"].iloc[index]:
            return Signal(source_name="peek", action=Action.BUY, probability=0.9, risk=0.1,
                          conid=self.conids[0], quantity=10)
        return None


def test_on_bar_sees_only_bars_up_to_the_current_one(tmp_duckdb_path):
    bars = _write_synthetic_bars(tmp_duckdb_path)
    strategy = _install_strategy(FrameLengthRecorder(), tmp_duckdb_path)
    _make_backtester(tmp_duckdb_path).run(strategy, [4391])
    assert strategy.seen
    for index, length, last_time in strategy.seen:
        assert length == index + 1
        assert last_time == bars.index[index]


def test_a_strategy_that_reads_the_next_bar_fails_on_its_first_bar(tmp_duckdb_path):
    _write_synthetic_bars(tmp_duckdb_path)
    strategy = _install_strategy(PeekNextClose(), tmp_duckdb_path)
    with pytest.raises(IndexError):
        _make_backtester(tmp_duckdb_path).run(strategy, [4391])
    assert strategy.calls == 1
