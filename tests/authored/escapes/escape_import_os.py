import os

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        os.system("true")
        return Decision(action="BUY", probability=0.5)
