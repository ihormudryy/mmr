from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        blob = history["close"].to_numpy().dumps()
        return Decision(action="BUY", probability=0.5) if blob else None
