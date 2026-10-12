from trader.authored.api import AuthoredStrategy, Decision


class CountsBars(AuthoredStrategy):
    LOOKBACK_BARS = 50

    def on_bar(self, history, params):
        return Decision("BUY", len(history) / 10000.0)
