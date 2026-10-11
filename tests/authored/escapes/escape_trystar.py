from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        try:
            return Decision(action="BUY", probability=0.5)
        except* ValueError:
            return None
