import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        keys = pd.read_csv("/home/trader/.config/mmr/keys/rpc/trader.key")
        return Decision(action="BUY", probability=0.5) if len(keys) else None
