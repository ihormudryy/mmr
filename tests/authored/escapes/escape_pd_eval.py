import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        pd.eval("1 + 1")
        return Decision(action="BUY", probability=0.5)
