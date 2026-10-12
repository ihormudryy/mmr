from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        call = getattr(history, "to_pickle")
        call("/tmp/x")
        return Decision(action="BUY", probability=0.5)
