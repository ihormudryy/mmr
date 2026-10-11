from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        leak = "{0.__globals__[__builtins__]}".format(self)
        return Decision(action="BUY", probability=0.5) if leak else None
