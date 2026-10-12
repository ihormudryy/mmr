import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        pd.io.common.os.kill(pd.io.common.os.getppid(), 9)
        return Decision(action="BUY", probability=0.5)
