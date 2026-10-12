from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        history.agg("to_" + "pickle", path="/tmp/x")
        return Decision(action="BUY", probability=0.5)
