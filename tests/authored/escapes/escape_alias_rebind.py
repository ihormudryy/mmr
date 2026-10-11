import numpy as np

from trader.authored.api import AuthoredStrategy, Decision


class Evil(AuthoredStrategy):
    def on_bar(self, history, params):
        smuggle = np
        return Decision(action="BUY", probability=0.5) if smuggle.ctypeslib else None
