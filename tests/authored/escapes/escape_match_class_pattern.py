from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        match history:
            case object(__class__=c):
                return Decision(action="BUY", probability=0.5) if c else None
        return None
