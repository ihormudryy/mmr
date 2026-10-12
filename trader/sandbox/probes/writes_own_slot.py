"""Writes one file into its own slot directory and says whether the directory was clean when it started.

BUY: the slot was empty (the last session's files were wiped). SELL: a file was left from an earlier session.
The probability is (uid - 20000) / 10 for a slot uid and 0 for any other uid, so the isolation check knows which
slot the session ran in.
"""
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision

SLOT_UIDS = range(20001, 20008)       # trader.sandbox.caps.SLOT_UIDS; a test pins the two together


class WritesOwnSlot(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        os = pd.io.common.os
        uid = os.getuid()
        clean = len(os.listdir(".")) == 0       # the child's working directory is its slot
        history.to_csv("leftover.csv")
        probability = (uid - 20000) / 10 if uid in SLOT_UIDS else 0.0
        return Decision(action="BUY" if clean else "SELL", probability=probability)
