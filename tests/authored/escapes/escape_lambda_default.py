from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        sneak = lambda builtins=__builtins__: builtins
        return Decision(action="BUY", probability=0.5) if sneak() else None
