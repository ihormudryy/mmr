from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        made = type("X", (), {})
        return Decision(action="BUY", probability=0.5) if made else None
