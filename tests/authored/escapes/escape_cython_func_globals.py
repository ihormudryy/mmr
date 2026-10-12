import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        leak = pd.Timedelta(1).ceil.func_globals
        return Decision(action="BUY", probability=0.5) if leak else None
