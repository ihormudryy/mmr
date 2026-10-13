from trader.authored.api import AuthoredStrategy, Decision


class ShortHistoryBuys(AuthoredStrategy):
    LOOKBACK_BARS = 3

    def on_bar(self, history, params):
        if len(history) < 3:
            return Decision("BUY", 0.5)
        return None
