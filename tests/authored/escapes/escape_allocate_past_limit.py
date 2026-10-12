import numpy as np

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        _hog = np.ones(2 ** 33)
        return Decision(action="BUY", probability=0.5) if _hog.sum() else None
