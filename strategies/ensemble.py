"""Ensemble Strategy: weighted voting across RSI, MACD, and Bollinger Bands.

Combines three independent signal sources with weighted voting:
  - RSI (14-period): oversold/overbought
  - MACD (12/26/9): momentum crossover
  - Bollinger Bands (20, 2): mean reversion

Only triggers a trade when 2+ signals agree (majority vote), and only on
the bar where that majority FORMS (edge-triggered). The RSI and Bollinger
votes are zone-based (states that can persist for many consecutive bars);
without the edge trigger the strategy re-emitted the same signal every
bar while a dip lasted — which the backtester turns into uncontrolled
per-bar pyramiding (BUY adds 10% of cash each time) and the live bridge
turns into a proposal per bar. Signal probability is the weighted average
of agreeing signals.

Note: RSI here is Cutler's variant (simple moving average of gains and
losses), not Wilder's smoothed RSI — values differ slightly from most
charting platforms.
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Optional

import numpy as np
import pandas as pd


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class Ensemble(Strategy):
    """Weighted voting across RSI, MACD, and Bollinger Bands — fires only when
    2+ of the 3 signals agree; probability is the weighted average of the
    agreeing signals."""

    def __init__(self):
        super().__init__()

    @staticmethod
    def _votes_at(rsi, hist, close, lower, upper, off: int):
        """The three component votes evaluated at iloc offset ``off``
        (-1 = latest bar, -2 = previous bar). Returns (rsi, macd, bb) each
        in {-1, 0, +1}."""
        rsi_signal = 0
        v = rsi.iloc[off]
        if not np.isnan(v):
            if v < 40:
                rsi_signal = 1
            elif v > 60:
                rsi_signal = -1

        macd_signal = 0
        curr_hist, prev_hist = hist.iloc[off], hist.iloc[off - 1]
        if not np.isnan(curr_hist) and not np.isnan(prev_hist):
            if curr_hist > 0 and prev_hist <= 0:
                macd_signal = 1
            elif curr_hist < 0 and prev_hist >= 0:
                macd_signal = -1

        bb_signal = 0
        c, lo, up = close.iloc[off], lower.iloc[off], upper.iloc[off]
        if not np.isnan(lo) and not np.isnan(up):
            if c < lo:
                bb_signal = 1
            elif c > up:
                bb_signal = -1
        return rsi_signal, macd_signal, bb_signal

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        if len(prices) < 30:
            return None

        close = prices['close']

        # --- RSI: zone-based (below 40 = bullish zone, above 60 = bearish;
        # a crossover alone is too rare on daily bars) ---
        rsi = _compute_rsi(close)
        rsi_weight = 0.3

        # --- MACD ---
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        macd_weight = 0.4

        # --- Bollinger Bands (population std — the textbook definition) ---
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std(ddof=0)
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        bb_weight = 0.3

        # --- Voting, edge-triggered on the AGGREGATE ---
        # The majority condition must hold now and NOT have held on the
        # previous bar — zone votes persist for many bars, and re-emitting
        # the same signal each bar pyramids in the backtester and spams
        # proposals live.
        rsi_signal, macd_signal, bb_signal = self._votes_at(
            rsi, hist, close, lower, upper, -1)
        prev_votes = self._votes_at(rsi, hist, close, lower, upper, -2)

        buy_votes = sum(1 for s in (rsi_signal, macd_signal, bb_signal) if s == 1)
        sell_votes = sum(1 for s in (rsi_signal, macd_signal, bb_signal) if s == -1)
        prev_buy = sum(1 for s in prev_votes if s == 1)
        prev_sell = sum(1 for s in prev_votes if s == -1)

        # Need 2+ signals to agree (majority), newly formed this bar
        if buy_votes >= 2 and prev_buy < 2:
            # Weighted probability from agreeing signals
            weights = []
            if rsi_signal == 1:
                weights.append(rsi_weight)
            if macd_signal == 1:
                weights.append(macd_weight)
            if bb_signal == 1:
                weights.append(bb_weight)
            prob = min(0.85, 0.5 + sum(weights) * 0.3)
            return Signal(
                source_name=self.name,
                action=Action.BUY,
                probability=prob,
                risk=1.0 - prob,
            )

        if sell_votes >= 2 and prev_sell < 2:
            weights = []
            if rsi_signal == -1:
                weights.append(rsi_weight)
            if macd_signal == -1:
                weights.append(macd_weight)
            if bb_signal == -1:
                weights.append(bb_weight)
            prob = min(0.85, 0.5 + sum(weights) * 0.3)
            return Signal(
                source_name=self.name,
                action=Action.SELL,
                probability=prob,
                risk=1.0 - prob,
            )

        return None
