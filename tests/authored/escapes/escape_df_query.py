from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        history.query("close > 0")
        return Decision(action="BUY", probability=0.5)
