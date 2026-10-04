"""Pairs Trading / Statistical Arbitrage via Z-Score of price ratio.

Tracks the log price ratio between two instruments, computes a rolling
z-score, and generates buy/sell signals when the spread diverges beyond
a threshold.  Trades the second conid (the one being priced) as the
"cheap leg" — buys when the z-score says it's undervalued relative to
the first conid, sells when the spread reverts.

LONG-ONLY: this system's backtester and signal→proposal bridge reject
short entries, so only the z > ENTRY_Z side (conid[1] undervalued → BUY
conid[1]) is traded; the mirror side is simply not entered — it no
longer parks the state machine in a phantom "short" that blocked real
longs until the spread reverted.

Requires exactly 2 conids in the strategy config. Instrument identity
comes from ``self.dispatch_conid`` (stamped by the live runtime before
each on_prices call), with a length-growth heuristic as fallback for
callers that don't stamp it. The two legs are aligned BY TIMESTAMP
before the ratio — positional tail-alignment paired prices from
different minutes whenever one feed lagged, which for a spread strategy
is the whole signal.
"""

from trader.trading.strategy import Signal, Strategy
from trader.objects import Action
from typing import Dict, Optional

import numpy as np
import pandas as pd


class PairsZScore(Strategy):
    """Trade the z-score of the log price ratio between two instruments."""

    LOOKBACK = 60       # rolling window for mean/std of the spread
    ENTRY_Z = 2.0       # open position when |z| > ENTRY_Z
    EXIT_Z = 0.5        # close position when |z| < EXIT_Z
    # Bars of aligned history required before trading. Must cover the full
    # LOOKBACK — the previous default (30) let the strategy trade while the
    # rolling window was still truncated to whatever data existed, so early
    # z-scores were computed on an unintended, shorter lookback.
    MIN_BARS = LOOKBACK + 5

    def __init__(self):
        super().__init__()
        self._prices: Dict[int, pd.Series] = {}  # conid -> close series
        self._position_side: Optional[str] = None  # 'long' while holding conid[1]

    def _identify_conid(self, close: pd.Series, conids) -> Optional[int]:
        """Which leg is this callback for? Prefer the runtime's stamp."""
        stamped = self.dispatch_conid
        if stamped is not None and stamped in conids:
            return int(stamped)
        # Fallback heuristic (unstamped callers): the series that grew.
        for cid in conids:
            if cid not in self._prices:
                return cid
            if len(close) > len(self._prices[cid]):
                return cid
        other = self._prices.get(conids[1])
        if other is None or len(other) == 0:
            return conids[0]
        return conids[0] if close.iloc[-1] != other.iloc[-1] else conids[1]

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        if prices is None or len(prices) < 2:
            return None

        close = prices['close']
        conids = self.conids or []
        if len(conids) < 2:
            return None

        current_conid = self._identify_conid(close, conids)
        if current_conid is None:
            return None
        self._prices[current_conid] = close.copy()

        # Only generate signals on the second conid, when both series exist
        if current_conid != conids[1]:
            return None

        s0 = self._prices.get(conids[0])
        s1 = self._prices.get(conids[1])
        if s0 is None or s1 is None:
            return None

        # Align the legs BY TIMESTAMP (inner join), then take the tail.
        pair = pd.concat([s0, s1], axis=1, join='inner', keys=['p0', 'p1']).dropna()
        if len(pair) < self.MIN_BARS:
            return None
        pair = pair.iloc[-(self.LOOKBACK + 5):]

        p0 = pair['p0'].to_numpy()
        p1 = pair['p1'].to_numpy()

        # Log price ratio
        with np.errstate(divide='ignore', invalid='ignore'):
            log_ratio = np.log(p0 / p1)

        if np.any(~np.isfinite(log_ratio)):
            return None

        # Rolling z-score over the FULL configured lookback (guaranteed
        # available by the MIN_BARS gate above).
        roll = pd.Series(log_ratio).rolling(self.LOOKBACK)
        roll_mean = roll.mean().iloc[-1]
        roll_std = roll.std().iloc[-1]

        if np.isnan(roll_mean) or np.isnan(roll_std) or roll_std < 1e-8:
            return None

        z = (log_ratio[-1] - roll_mean) / roll_std

        # Signal logic (long-only, see module docstring):
        # High z → conid[0] expensive relative to conid[1] → BUY conid[1];
        # revert (z < EXIT_Z) closes it.
        if self._position_side is None:
            if z > self.ENTRY_Z:
                self._position_side = 'long'
                prob = min(0.9, 0.5 + abs(z) * 0.1)
                return Signal(
                    source_name=self.name,
                    action=Action.BUY,
                    probability=prob,
                    risk=1.0 - prob,
                    metadata={'z_score': float(z), 'spread_mean': float(roll_mean)},
                )
        elif self._position_side == 'long' and z < self.EXIT_Z:
            self._position_side = None
            return Signal(
                source_name=self.name,
                action=Action.SELL,
                probability=0.7,
                risk=0.3,
                metadata={'z_score': float(z), 'exit': True},
            )

        return None
