"""Writes one file into its own slot directory and says whether the directory was clean when it started.

BUY: the slot was empty (the last session's files were wiped). SELL: a file was left from an earlier session.
"""
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class WritesOwnSlot(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        clean = len(pd.io.common.os.listdir(".")) == 0       # the child's working directory is its slot
        history.to_csv("leftover.csv")
        return Decision(action="BUY" if clean else "SELL", probability=0.5)
