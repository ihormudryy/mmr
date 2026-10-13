from trader.authored.api import AuthoredStrategy, Decision


class FaultsAboveLimit(AuthoredStrategy):
    LOOKBACK_BARS = 5
    LIMIT = 150.0

    def on_bar(self, history, params):
        if history["close"].iloc[-1] > params["LIMIT"]:
            return Decision("BUY", 2.0)
        return None
