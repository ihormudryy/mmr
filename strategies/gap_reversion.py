"""Gap Reversion — intraday fade of opening gaps back to session VWAP.

Thesis: Liquid large-cap stocks that gap > X% at the US open often revert
partially (or fully) toward the session VWAP during the trading day. This
strategy enters the fade the first time price crosses session VWAP in the
reversion direction, after the gap has been established, during regular
trading hours only. The exit is a cross back through VWAP or an EOD flat.

This is complementary to VwapReversion (continuous mean-reversion on
rolling std) and OpeningRangeBreakout (pure breakout). It specifically
targets gap days — a distinct intraday regime.
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional
from datetime import time as dtime

import numpy as np
import pandas as pd


class GapReversion(Strategy):
    """Intraday fade of the opening gap back to session VWAP. EOD flat."""

    GAP_MIN_PCT = 0.5       # absolute gap from prior close, in percent (0.5 = 0.5%)
    GAP_MAX_PCT = 5.0       # ignore catastrophic gaps — likely real news, don't fade
    VOL_MULT = 1.2          # current bar volume vs 20-bar SMA
    VOL_WINDOW = 20
    EOD_CLOSE_MIN = 15 * 60 + 45   # 15:45 ET — flat before close
    RTH_OPEN_MIN  = 9 * 60 + 30    # 09:30 ET
    RTH_CLOSE_MIN = 16 * 60        # 16:00 ET
    MIN_BARS = 40

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert("America/New_York")

        et_minute = (et.hour * 60 + et.minute).to_numpy()
        et_date_arr = et.date  # python date objects
        rth_mask = (et_minute >= self.RTH_OPEN_MIN) & (et_minute < self.RTH_CLOSE_MIN)

        close = prices["close"].to_numpy()
        high = prices["high"].to_numpy()
        low = prices["low"].to_numpy()
        volume = prices["volume"].to_numpy()

        # Session VWAP — RTH-anchored: pre/after-market bars contribute
        # nothing, so thin extended-hours prints can't skew the fade target
        # (and backtest-vs-live can't diverge on session coverage).
        et_date_series = pd.Series(et_date_arr, index=prices.index)
        rth_ser = pd.Series(rth_mask, index=prices.index)
        pv_series = (prices["close"] * prices["volume"]).where(rth_ser, 0.0)
        v_series = prices["volume"].where(rth_ser, 0.0)
        cum_pv = pv_series.groupby(et_date_series).cumsum()
        cum_v = v_series.groupby(et_date_series).cumsum()
        vwap_s = cum_pv / cum_v.replace(0, np.nan)
        vwap = vwap_s.to_numpy()

        # Gap per day — vectorised. For each ET date, first RTH open /
        # prior ET date's last RTH close - 1.
        date_rth = et_date_series.where(rth_ser)  # NaN outside RTH
        # first RTH open + last RTH close per day
        opens = prices["open"].groupby(date_rth).first()
        closes = prices["close"].groupby(date_rth).last()
        prev_closes = closes.shift(1)
        gap_by_day = ((opens / prev_closes) - 1.0) * 100.0  # percent
        # Map gap back to every bar via its (date, rth-member) key
        gap_map = date_rth.map(gap_by_day)
        gap_pct = gap_map.to_numpy(dtype=float)

        vol_avg = prices["volume"].rolling(self.VOL_WINDOW).mean().to_numpy()

        # eod_flat flag — bars at/after 15:45 ET
        eod_flat = (et_minute >= self.EOD_CLOSE_MIN) & rth_mask

        # Day ordinal per bar so on_bar can require same-session crossings.
        day_ord = pd.factorize(et_date_series)[0]

        return {
            "close": close,
            "vwap": vwap,
            "gap_pct": gap_pct,
            "vol": volume,
            "vol_avg": vol_avg,
            "rth": rth_mask,
            "eod_flat": eod_flat,
            "et_minute": et_minute,
            "day_ord": day_ord,
        }

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        if not state or index < self.MIN_BARS:
            return None
        if not state["rth"][index]:
            return None

        close = state["close"][index]
        vwap = state["vwap"][index]
        gap = state["gap_pct"][index]
        vol = state["vol"][index]
        vol_avg = state["vol_avg"][index]
        prev_close = state["close"][index - 1]
        prev_vwap = state["vwap"][index - 1]

        # EOD flat — one SELL at the FIRST 15:45 bar (edge-triggered), as a
        # backstop for the close_by_time the BUY already carries. The
        # previous version emitted a SELL on EVERY bar from 15:45 to 16:00,
        # every day, position or not — pure signal spam.
        if state["eod_flat"][index] and not state["eod_flat"][index - 1]:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.55, risk=0.45,
            )
        if state["eod_flat"][index]:
            return None

        if np.isnan(vwap) or np.isnan(prev_vwap) or np.isnan(gap) or np.isnan(vol_avg) or vol_avg <= 0:
            return None
        # Crossing comparisons must be within the same session, against an
        # RTH bar — not yesterday's close or a pre-market print.
        if state["day_ord"][index] != state["day_ord"][index - 1]:
            return None
        if not state["rth"][index - 1]:
            return None

        abs_gap = abs(gap)
        if abs_gap < self.GAP_MIN_PCT or abs_gap > self.GAP_MAX_PCT:
            return None

        vol_ok = vol > vol_avg * self.VOL_MULT

        # GAP-DOWN FADE: gap < 0 → expect revert UP. BUY when close crosses
        # from below vwap to at-or-above vwap with volume. Carries the EOD
        # flat rule (ET) so the fade never holds overnight.
        if gap < 0 and prev_close < prev_vwap and close >= vwap and vol_ok:
            return Signal(
                source_name=self.name, action=Action.BUY,
                probability=0.62, risk=0.38,
                close_by_time=dtime(self.EOD_CLOSE_MIN // 60, self.EOD_CLOSE_MIN % 60),
                close_by_tz="America/New_York",
            )

        # GAP-UP FADE: gap > 0 → expect revert DOWN. SELL (exit/short) when
        # close crosses from above vwap to at-or-below vwap with volume.
        if gap > 0 and prev_close > prev_vwap and close <= vwap and vol_ok:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.62, risk=0.38,
            )

        # After position is entered, exit when price crosses back through VWAP.
        # With gap < 0 (long): we were BOUGHT when price crossed UP through VWAP.
        # Exit when price crosses back DOWN through VWAP.
        if gap < 0 and prev_close > prev_vwap and close <= vwap:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.60, risk=0.40,
            )

        return None
