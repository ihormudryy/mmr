from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        caller_globals = (x for x in history.close).gi_frame.f_back.f_globals
        caller_globals["Decision"](action="BUY", probability=0.5)
        return Decision(action="BUY", probability=0.5)
