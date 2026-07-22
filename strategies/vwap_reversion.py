"""VWAP Reversion — intraday mean-reversion to the session volume-weighted
average price, confirmed by RSI.

Logic:
  - Session VWAP resets at the start of each trading day (daily normalize).
  - Rolling std of (close − VWAP) gives an intraday price-dispersion scale.
  - BUY when price is ENTRY_STD below VWAP AND RSI < RSI_OVERSOLD (cheap +
    oversold → expect reversion up to fair value).
  - SELL mirror above VWAP + overbought.

Uses the precompute hook — all indicator arrays are computed once on the
full series. Correctness of the session-VWAP reset depends on grouping by
the date portion of the index; we assert no-lookahead in tests.
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


class VwapReversion(Strategy):
    """Intraday VWAP mean reversion with RSI confirmation — resets VWAP daily.

    RSI is Cutler's variant (SMA of gains/losses) — values differ slightly
    from Wilder's smoothed RSI on most charting platforms. RTH-only: the
    VWAP accumulates only regular-session bars and entries/exits are gated
    to the session, so extended-hours prints can't skew the baseline.
    """

    ENTRY_STD = 1.5           # σ from session VWAP to trigger entry
    RSI_PERIOD = 14
    RSI_OVERSOLD = 35
    RSI_OVERBOUGHT = 65
    STD_WINDOW = 30           # rolling std of (close − VWAP)
    MIN_BARS = 40
    SESSION_TZ = 'America/New_York'
    RTH_OPEN_MIN = 9 * 60 + 30
    RTH_CLOSE_MIN = 16 * 60
    # Intraday strategy: flatten before the close rather than holding the
    # reversion bet overnight.
    EOD_HOUR = 15
    EOD_MINUTE = 55

    def precompute(self, prices: pd.DataFrame) -> Dict[str, Any]:
        if len(prices) < self.MIN_BARS:
            return {}

        # Session VWAP: group by the TRADING day (session tz), cumsum
        # price × volume / cumsum volume within each day. Cumulative
        # arithmetic within a day only uses bars at or before the current
        # bar — position i depends only on bars [0..i]. No cross-day leakage.
        close = prices['close']
        volume = prices['volume']
        idx = prices.index
        if idx.tz is not None:
            local = idx.tz_convert(self.SESSION_TZ)
        else:
            local = idx.tz_localize('UTC').tz_convert(self.SESSION_TZ)
        day = local.normalize()
        local_minute = (local.hour * 60 + local.minute)
        rth_mask = ((local_minute >= self.RTH_OPEN_MIN)
                    & (local_minute < self.RTH_CLOSE_MIN)).to_numpy()
        rth_ser = pd.Series(rth_mask, index=prices.index)

        # RTH-anchored VWAP: extended-hours bars contribute nothing.
        pv = (close * volume).where(rth_ser, 0.0)
        v = volume.where(rth_ser, 0.0)
        cum_pv = pv.groupby(day).cumsum()
        cum_v = v.groupby(day).cumsum()
        vwap = cum_pv / cum_v.replace(0, np.nan)

        # Rolling std of (close − VWAP). NaN outside RTH (vwap is NaN
        # there); min_periods keeps the first RTH bars of a day usable
        # while the window still spans overnight NaNs.
        diff = close - vwap
        std = diff.rolling(self.STD_WINDOW,
                           min_periods=max(10, self.STD_WINDOW // 2)).std()

        rsi = _rsi(close, self.RSI_PERIOD)

        return {
            'close': close.to_numpy(),
            'vwap':  vwap.to_numpy(),
            'std':   std.to_numpy(),
            'rsi':   rsi.to_numpy(),
            'rth':   rth_mask,
        }

    def on_bar(self, prices: pd.DataFrame, state: Dict[str, Any], index: int) -> Optional[Signal]:
        if not state or index < self.MIN_BARS:
            return None
        if not state['rth'][index]:
            return None

        close = state['close'][index]
        vwap = state['vwap'][index]
        std = state['std'][index]
        rsi = state['rsi'][index]
        prev_close = state['close'][index - 1]
        prev_vwap = state['vwap'][index - 1]

        if (np.isnan(vwap) or np.isnan(std) or np.isnan(rsi)
                or np.isnan(prev_vwap) or std <= 0):
            return None

        z = (close - vwap) / std

        # BUY: meaningfully below VWAP and RSI oversold (classic dip-buy).
        # Edge-triggered: the condition is a state that persists while the
        # dip lasts; only the bar where it FORMS may emit, otherwise the
        # backtester pyramids per bar and the live bridge spams proposals.
        if z < -self.ENTRY_STD and rsi < self.RSI_OVERSOLD:
            prev_std = state['std'][index - 1]
            prev_rsi = state['rsi'][index - 1]
            prev_formed = (
                not np.isnan(prev_vwap) and not np.isnan(prev_std)
                and prev_std > 0 and not np.isnan(prev_rsi)
                and ((prev_close - prev_vwap) / prev_std) < -self.ENTRY_STD
                and prev_rsi < self.RSI_OVERSOLD
            )
            if prev_formed:
                return None
            return Signal(
                source_name=self.name, action=Action.BUY,
                probability=0.65, risk=0.35,
                close_by_time=dtime(self.EOD_HOUR, self.EOD_MINUTE),
                close_by_tz=self.SESSION_TZ,
            )

        # EXIT LONG / enter-short symmetric: fire SELL when price CROSSES
        # from below VWAP to at-or-above VWAP. This closes any open long
        # near fair value (reversion complete) rather than waiting for the
        # far-side overbought condition that rarely triggers.
        crossed_up = (prev_close < prev_vwap) and (close >= vwap)
        if crossed_up:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.60, risk=0.40,
            )

        # Far-side overbought — also emits SELL, which would go short in a
        # short-capable backtester. Our backtester rejects short sells
        # (held <= 0 → skip), so this path only takes effect if we already
        # have a long to close; usually crossed_up above closes first.
        if z > self.ENTRY_STD and rsi > self.RSI_OVERBOUGHT:
            return Signal(
                source_name=self.name, action=Action.SELL,
                probability=0.65, risk=0.35,
            )
        return None
