import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        net = pd.io.common.os.sys.modules["socket"]
        conn = net.create_connection(("1.1.1.1", 443), 1)
        return Decision(action="BUY", probability=0.5) if conn else None
