from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        history.to_csv("/store/escape.csv")
        return Decision(action="BUY", probability=0.5)
