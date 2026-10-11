from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    @property
    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.5)
