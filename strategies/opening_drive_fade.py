"""Opening Drive Fade — fade an extreme first-30-min drive back to VWAP.

Thesis: When a liquid name opens with a big directional drive (>1.5
ATR_day from open) in the first 30 RTH minutes and then shows exhaustion
(volume decline + RSI extreme), the move often reverts partway back
toward session VWAP during the rest of the morning. Fade-of-extremes
variant, gated hard on "this has to be a notable move" so trades are rare.

Long-only (backtester rejects shorts), so we only fade gap-DOWN drives:
extreme first-30-min low + RSI oversold + volume exhaustion → BUY for
revert up to VWAP. EOD flat.
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional
from datetime import time as dtime

import numpy as np
import pandas as pd


class OpeningDriveFade(Strategy):
    """Fade an extreme first-30min downside drive on RSI + volume exhaustion."""

    DRIVE_WINDOW_MIN = 30          # first 30 min establishes the drive extreme
    ENTRY_WINDOW_END_MIN = 120     # entries allowed until 11:30 ET
    # Drive must be >= this × the 14-day DAILY average true range. NOTE: the
    # original code computed "ATR" as the mean PER-1-MINUTE-BAR true range
    # over ~14 days of bars — 1.5× an average one-minute range is a few
    # cents, so the "notable drive" gate was trivially passed by any open.
    # Against a real daily ATR, 1.5 would mean a 30-minute move exceeding
    # 1.5 full daily ranges (flash-crash rare). 0.5 — half a typical day's
    # range in the first 30 minutes — is a judgment default for "big
    # directional drive"; re-validate with a sweep before arming.
    DRIVE_ATR_MULT = 0.5
    ATR_DAYS = 14                  # trailing days for the daily ATR
    RSI_PERIOD = 14
    RSI_OVERSOLD = 30
    VOL_EXHAUST_MULT = 0.75        # current bar vol < 0.75× 20-bar avg = exhaustion
    VOL_WINDOW = 20
    MAX_HOLD_BARS = 120            # 2 hours
    EOD_HOUR = 15
    EOD_MINUTE = 45
    MIN_BARS = 60
    RTH_OPEN_MIN = 9 * 60 + 30

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        idx = prices.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        et = idx.tz_convert("America/New_York")

        et_minute = (et.hour * 60 + et.minute).to_numpy()
        et_date = pd.Series(et.date, index=prices.index)
        rth_mask = (et_minute >= self.RTH_OPEN_MIN) & (et_minute < 16 * 60)

        close = prices["close"].to_numpy()
        low = prices["low"].to_numpy()
        volume = prices["volume"].to_numpy()

        rth_mask_ser = pd.Series(rth_mask, index=prices.index)

        # Session VWAP, RTH-anchored — extended-hours prints contribute
        # nothing to the fade target.
        pv = (prices["close"] * prices["volume"]).where(rth_mask_ser, 0.0)
        v = prices["volume"].where(rth_mask_ser, 0.0)
        cum_pv = pv.groupby(et_date).cumsum()
        cum_v = v.groupby(et_date).cumsum()
        vwap = (cum_pv / cum_v.replace(0, np.nan)).to_numpy()

        # Day-minutes since RTH open (negative pre-market)
        minutes_since_open = et_minute - self.RTH_OPEN_MIN
        drive_window_mask = rth_mask & (minutes_since_open < self.DRIVE_WINDOW_MIN)
        drive_ser = pd.Series(drive_window_mask, index=prices.index)

        # Per-day references, computed CAUSALLY (carry-forward, never
        # broadcast backward): the previous transform('first')/('min') put
        # the day's open and the eventual window-min on bars BEFORE they
        # existed — future data at those indices per assert_no_lookahead.
        first_rth = rth_mask_ser & (rth_mask_ser.groupby(et_date).cumsum() == 1)
        day_open = prices["open"].where(first_rth).groupby(et_date).ffill().to_numpy()
        # Running min of the low over the drive window; after the window it
        # carries the window's final min forward for the rest of the day.
        drive_low = (
            prices["low"].where(drive_ser).groupby(et_date).cummin()
            .groupby(et_date).ffill().to_numpy()
        )

        # DAILY ATR over the prior ATR_DAYS sessions (RTH aggregates,
        # shifted so today's still-forming range never contributes — the
        # same causal per-day pattern LateDayMomentum uses).
        day_high = prices["high"].where(rth_mask_ser).groupby(et_date).max()
        day_low_full = prices["low"].where(rth_mask_ser).groupby(et_date).min()
        day_close = prices["close"].where(rth_mask_ser).groupby(et_date).last()
        prev_close_daily = day_close.shift(1)
        tr_daily = pd.concat([
            (day_high - day_low_full),
            (day_high - prev_close_daily).abs(),
            (day_low_full - prev_close_daily).abs(),
        ], axis=1).max(axis=1)
        atr_daily = tr_daily.shift(1).rolling(self.ATR_DAYS, min_periods=5).mean()
        atr = et_date.map(atr_daily).to_numpy(dtype=float)

        # Drive magnitude: (day_open - drive_low)
        drive_down = day_open - drive_low

        # RSI(14) standard
        delta = prices["close"].diff()
        gain = delta.clip(lower=0).rolling(self.RSI_PERIOD).mean()
        loss = (-delta.clip(upper=0)).rolling(self.RSI_PERIOD).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100.0 - (100.0 / (1.0 + rs))).to_numpy()

        vol_avg = prices["volume"].rolling(self.VOL_WINDOW).mean().to_numpy()

        # Entry window: after drive window ends, up to ENTRY_WINDOW_END_MIN
        entry_mask = rth_mask & \
            (minutes_since_open >= self.DRIVE_WINDOW_MIN) & \
            (minutes_since_open < self.ENTRY_WINDOW_END_MIN)

        return {
            "close": close,
            "low": low,
            "vwap": vwap,
            "day_open": day_open,
            "drive_low": drive_low,
            "drive_down": drive_down,
            "atr": atr,
            "rsi": rsi,
            "vol": volume,
            "vol_avg": vol_avg,
            "entry_window": entry_mask,
        }

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        if not state or index < self.MIN_BARS:
            return None
        if not state["entry_window"][index]:
            return None

        close = state["close"][index]
        low = state["low"][index]
        vwap = state["vwap"][index]
        drive_low = state["drive_low"][index]
        drive_down = state["drive_down"][index]
        atr = state["atr"][index]
        rsi = state["rsi"][index]
        vol = state["vol"][index]
        vol_avg = state["vol_avg"][index]

        if any(np.isnan(x) for x in (vwap, drive_low, drive_down, atr, rsi, vol_avg)) or vol_avg <= 0 or atr <= 0:
            return None

        # Must be a notable down-drive — drive magnitude >= DRIVE_ATR_MULT × ATR
        if drive_down < atr * self.DRIVE_ATR_MULT:
            return None

        # Price must still be below VWAP (i.e. the fade target hasn't been hit yet)
        if close >= vwap:
            return None

        # Exhaustion: RSI oversold AND volume declining
        if rsi > self.RSI_OVERSOLD:
            return None
        if vol > vol_avg * self.VOL_EXHAUST_MULT:
            return None

        # And price must be near the drive low (within 0.2 ATR) — the reversion setup
        if low - drive_low > 0.2 * atr:
            return None

        # Edge-trigger: the exhaustion conditions (RSI oversold + volume
        # drying up + near the low) are states that can persist for several
        # bars — emit only on the bar where the full setup FORMS, so the
        # backtester doesn't pyramid per bar and the bridge doesn't spam.
        if index >= 1:
            p = index - 1
            prev_formed = (
                bool(state["entry_window"][p])
                and not np.isnan(state["rsi"][p])
                and not np.isnan(state["vol_avg"][p]) and state["vol_avg"][p] > 0
                and not np.isnan(state["drive_down"][p]) and not np.isnan(state["atr"][p])
                and state["atr"][p] > 0
                and state["drive_down"][p] >= state["atr"][p] * self.DRIVE_ATR_MULT
                and state["close"][p] < state["vwap"][p]
                and state["rsi"][p] <= self.RSI_OVERSOLD
                and state["vol"][p] <= state["vol_avg"][p] * self.VOL_EXHAUST_MULT
                and (state["low"][p] - state["drive_low"][p]) <= 0.2 * state["atr"][p]
            )
            if prev_formed:
                return None

        return Signal(
            source_name=self.name, action=Action.BUY,
            probability=0.62, risk=0.38,
            close_by_time=dtime(self.EOD_HOUR, self.EOD_MINUTE),
            close_by_tz="America/New_York",
            max_hold_bars=self.MAX_HOLD_BARS,
        )
