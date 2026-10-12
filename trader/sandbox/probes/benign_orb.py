"""A tiny, honest opening-range breakout. Used by the isolation checks and the tests as the benign case."""
from trader.authored.api import AuthoredStrategy, Decision


class BenignOrb(AuthoredStrategy):
    LOOKBACK_BARS = 30

    def on_bar(self, history, params):
        if len(history) < 3:
            return None
        opening_high = history["high"].iloc[:2].max()
        last_close = history["close"].iloc[-1]
        if last_close > opening_high:
            return Decision(action="BUY", probability=0.55)
        return None
