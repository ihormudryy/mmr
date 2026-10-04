"""VWAP Reclaim — intraday long reversal when price reclaims session VWAP.

Thesis: Stocks that open weak (below prior close AND below session VWAP)
and then reclaim VWAP on volume in the first 2 hours of RTH tend to
continue higher for the rest of the morning. Classic day-trader pattern —
"gap down, base, reclaim VWAP = long". EOD flat, max-hold cap.

Long-only. Designed for ~50-200 trades/symbol/year (low frequency).
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional
from datetime import time as dtime

import numpy as np
import pandas as pd


class VwapReclaim(Strategy):
    """First-2-hour VWAP reclaim long, volume-confirmed, EOD flat."""

    # Entry window — only first N minutes of RTH qualify
    ENTRY_WINDOW_MIN = 120         # 09:30 - 11:30 ET
    # Skip the first few RTH minutes: with one bar of data the session VWAP
    # equals that bar's own close, so a "reclaim" at 09:30 is degenerate —
    # and gap opens (the riskiest fade) are exactly when it would fire.
    MIN_MIN_AFTER_OPEN = 5
    # Below-VWAP threshold — price must have been this % below VWAP earlier in the day
    MIN_DIP_PCT = 0.3              # i.e. day's low was at least 0.3% under open VWAP
    # Volume confirmation
    VOL_MULT = 1.3                 # reclaim bar volume vs 20-bar SMA
    VOL_WINDOW = 20
    # Stop / target (shares are auto-sized, we only emit signals)
    MAX_HOLD_BARS = 180            # bail after 3 hours
    # EOD flat time (ET)
    EOD_HOUR = 15
    EOD_MINUTE = 45
    SESSION_TZ = "America/New_York"
    MIN_BARS = 60
    RTH_OPEN_MIN = 9 * 60 + 30

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert(self.SESSION_TZ)

        et_minute = (et.hour * 60 + et.minute).to_numpy()
        et_date = pd.Series(et.date, index=prices.index)
        rth_mask = (et_minute >= self.RTH_OPEN_MIN) & (et_minute < 16 * 60)
        rth_ser = pd.Series(rth_mask, index=prices.index)

        close = prices["close"].to_numpy()
        volume = prices["volume"].to_numpy()

        # Session VWAP, RTH-anchored (resets daily). Pre/after-market bars
        # contribute NOTHING — with extended-hours data in the feed, letting
        # thin pre-market prints into the accumulation skewed both the VWAP
        # baseline and the dip test, and made backtest-vs-live behaviour
        # depend on each side's session coverage.
        pv = (prices["close"] * prices["volume"]).where(rth_ser, 0.0)
        v = prices["volume"].where(rth_ser, 0.0)
        cum_pv = pv.groupby(et_date).cumsum()
        cum_v = v.groupby(et_date).cumsum()
        vwap = (cum_pv / cum_v.replace(0, np.nan)).to_numpy()

        # Cumulative intraday minimum of the RTH low (resets daily; NaN
        # pre-market so an overnight/pre-market wick can't fake the dip).
        day_low = prices["low"].where(rth_ser).groupby(et_date).cummin().to_numpy()

        # Day-open reference: the first RTH close of the day, CARRIED FORWARD
        # causally (ffill). The previous transform('first') broadcast the
        # 09:30 close backward onto pre-market bars — future data at those
        # indices, flagged by assert_no_lookahead.
        first_rth = rth_ser & (rth_ser.groupby(et_date).cumsum() == 1)
        open_ref = prices["close"].where(first_rth).groupby(et_date).ffill().to_numpy()

        vol_avg = prices["volume"].rolling(self.VOL_WINDOW).mean().to_numpy()

        # Entry window mask — skip the degenerate first minutes (see
        # MIN_MIN_AFTER_OPEN above).
        entry_mask = (
            rth_mask
            & (et_minute >= self.RTH_OPEN_MIN + self.MIN_MIN_AFTER_OPEN)
            & (et_minute < self.RTH_OPEN_MIN + self.ENTRY_WINDOW_MIN)
        )

        # Day ordinal per bar, so on_bar can require index-1 to be the SAME
        # session (a 09:30 "reclaim" against yesterday's VWAP is not a reclaim).
        day_ord = pd.factorize(et_date)[0]

        return {
            "close": close,
            "vwap": vwap,
            "day_low": day_low,
            "open_vwap": open_ref,
            "vol": volume,
            "vol_avg": vol_avg,
            "rth": rth_mask,
            "entry_window": entry_mask,
            "et_minute": et_minute,
            "day_ord": day_ord,
        }

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        """Live dispatch path. The backtester uses precompute()+on_bar() directly;
        the live strategy_runtime calls only on_prices(), so without this the
        strategy emitted NOTHING live (base on_prices returns None). Runs the same
        precompute over the accumulated window and evaluates on_bar() at the latest
        bar. Relies on the runtime's tick→bar resampling to supply proper per-bar
        OHLCV (VWAP + volume confirmation need real per-bar volume, not the raw
        cumulative tick counter)."""
        if prices is None or len(prices) < self.MIN_BARS:
            return None
        state = self.precompute(prices)
        if not state:
            return None
        return self.on_bar(prices, state, len(prices) - 1)

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        if not state or index < self.MIN_BARS:
            return None
        if not state["entry_window"][index]:
            return None
        # The crossing comparison below must be within the same session and
        # against an RTH bar — comparing 09:3x against yesterday's close (or
        # a pre-market print) is not a reclaim.
        if state["day_ord"][index] != state["day_ord"][index - 1]:
            return None
        if not state["rth"][index - 1]:
            return None

        close = state["close"][index]
        vwap = state["vwap"][index]
        day_low = state["day_low"][index]
        open_vwap = state["open_vwap"][index]
        prev_close = state["close"][index - 1]
        prev_vwap = state["vwap"][index - 1]
        vol = state["vol"][index]
        vol_avg = state["vol_avg"][index]

        if np.isnan(vwap) or np.isnan(prev_vwap) or np.isnan(day_low) or np.isnan(open_vwap) or np.isnan(vol_avg) or vol_avg <= 0:
            return None

        # Must have had a meaningful dip under open VWAP before reclaim
        dip_pct = (open_vwap - day_low) / open_vwap * 100.0
        if dip_pct < self.MIN_DIP_PCT:
            return None

        # Reclaim: prev bar close < vwap, this bar close >= vwap, volume confirmation
        if prev_close < prev_vwap and close >= vwap and vol > vol_avg * self.VOL_MULT:
            return Signal(
                source_name=self.name, action=Action.BUY,
                probability=0.64, risk=0.36,
                close_by_time=dtime(self.EOD_HOUR, self.EOD_MINUTE),
                close_by_tz=self.SESSION_TZ,
                max_hold_bars=self.MAX_HOLD_BARS,
            )

        return None
