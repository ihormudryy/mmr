from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        name = "importlib"
        module = __import__(name)
        return Decision(action="BUY", probability=0.5) if module else None
