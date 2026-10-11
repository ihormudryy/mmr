"""SP3 Plan 1 Task 2: the ORB AuthoredStrategy port (spec 10 equivalence, ruling 3,4)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from trader.authored.api import Decision
from tests.authored.fixtures.orb_authored import OpeningRangeBreakout

PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}


def _session(n=120, breakout_at=80, base=100.0):
    """One regular session of 1-min UTC bars, flat open range then a volume-backed break up."""
    start = pd.Timestamp("2024-01-02 14:30:00", tz="UTC")  # 09:30 ET
    index = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = np.full(n, base)
    close[breakout_at:] = base + 2.0          # break above the 09:30-10:00 range high
    volume = np.full(n, 1000.0)
    volume[breakout_at] = 10_000.0            # the breakout bar carries volume
    frame = pd.DataFrame(
        {"open": close, "high": close + 0.25, "low": close - 0.25,
         "close": close, "volume": volume}, index=index)
    frame.index.name = "date"
    return frame


def test_lookback_covers_a_session_plus_the_volume_window():
    assert OpeningRangeBreakout.LOOKBACK_BARS >= 390 + 20


def test_no_signal_until_more_than_min_bars_bars_are_seen():
    # A breakout at bar 30 (10:00 ET) would signal without the gate; the catalogue's MIN_BARS = 40 keeps it quiet.
    frame = _session(breakout_at=30)
    strategy = OpeningRangeBreakout()
    assert OpeningRangeBreakout.MIN_BARS == 40
    assert strategy.on_bar(frame.iloc[:31], PARAMS) is None
    assert strategy.on_bar(frame.iloc[:41], PARAMS) is None          # len 41 > MIN_BARS but no cross at bar 40


def test_no_signal_before_the_opening_range_closes():
    frame = _session()
    strategy = OpeningRangeBreakout()
    # 25 minutes in (bar 24 is 09:54 ET): the 30-minute range has not closed yet.
    assert strategy.on_bar(frame.iloc[:25], PARAMS) is None


def test_breakout_bar_is_a_buy():
    frame = _session(breakout_at=80)
    strategy = OpeningRangeBreakout()
    decision = strategy.on_bar(frame.iloc[: 81], PARAMS)
    assert isinstance(decision, Decision) and decision.action == "BUY"


def test_a_quiet_bar_inside_the_range_is_none():
    frame = _session(breakout_at=80)
    strategy = OpeningRangeBreakout()
    assert strategy.on_bar(frame.iloc[: 60], PARAMS) is None


def test_break_below_the_range_low_is_a_sell():
    frame = _session(breakout_at=80)
    low = frame.copy()
    # .loc, not chained .iloc assignment: pandas 3.0 Copy-on-Write makes the chained
    # form a no-op on a view.
    low.loc[low.index[80:], "close"] = 97.0  # drop below the range low, no volume gate on exit
    low.loc[low.index[80:], "low"] = 96.0
    strategy = OpeningRangeBreakout()
    decision = strategy.on_bar(low.iloc[: 81], PARAMS)
    assert isinstance(decision, Decision) and decision.action == "SELL"
