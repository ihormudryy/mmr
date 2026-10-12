"""An Opening Range Breakout port as an AuthoredStrategy (SP3 Plan 1 Task 2).

Faithful to strategies/opening_range_breakout.py within the SP3 API: one
symbol, one on_bar, no precompute, US regular hours only (ruling 3), two
breakout cross signals only (ruling 4). It passes trader.authored.static_check
(Task 6). Reused by the benchmark and by Plans 2-4.
"""
from typing import Mapping

import numpy as np
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class OpeningRangeBreakout(AuthoredStrategy):
    LOOKBACK_BARS = 420

    RANGE_MINUTES = 30
    VOLUME_MULT = 1.5
    RTH_OPEN_MIN = 570
    RTH_CLOSE_MIN = 960
    VOLUME_SMA_BARS = 20
    MIN_BARS = 40

    def on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None:
        range_minutes = int(params["RANGE_MINUTES"])
        volume_mult = float(params["VOLUME_MULT"])
        rth_open = int(params["RTH_OPEN_MIN"])
        rth_close = int(params["RTH_CLOSE_MIN"])
        sma_bars = int(params["VOLUME_SMA_BARS"])
        if len(history) <= int(params["MIN_BARS"]) or len(history) < sma_bars + 2:
            return None

        eastern = history.index.tz_convert("America/New_York")
        minute = np.asarray(eastern.hour) * 60 + np.asarray(eastern.minute)
        same_day = np.asarray(eastern.normalize() == eastern.normalize()[-1])

        if not (same_day[-1] and rth_open <= minute[-1] < rth_close):
            return None
        if minute[-1] < rth_open + range_minutes:
            return None

        in_range = same_day & (minute >= rth_open) & (minute < rth_open + range_minutes)
        if not in_range.any():
            return None

        high = history["high"].to_numpy()
        low = history["low"].to_numpy()
        close = history["close"].to_numpy()
        volume = history["volume"].to_numpy()

        range_high = float(np.max(high[in_range]))
        range_low = float(np.min(low[in_range]))
        volume_average = float(np.mean(volume[-sma_bars:]))
        if np.isnan(volume_average) or volume_average <= 0.0:
            return None

        last_close = float(close[-1])
        prev_close = float(close[-2])
        volume_ok = volume[-1] > volume_average * volume_mult

        if last_close > range_high and prev_close <= range_high and volume_ok:
            return Decision(action="BUY", probability=0.60)
        if last_close < range_low and prev_close >= range_low:
            return Decision(action="SELL", probability=0.60)
        return None
