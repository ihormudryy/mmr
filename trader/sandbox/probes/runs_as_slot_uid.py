"""Says whether a real child runs as one slot uid, in group sbxchild only, with no capability (spec 7.5).

BUY: real, effective and saved uid are one slot uid, every gid is sbxchild, no supplementary group, CapEff, CapPrm,
CapInh and CapAmb are all 0, and the child has its own seccomp filter with no-new-privileges. SELL otherwise. The
probability is (uid - 20000) / 10 for a slot uid and 0 for any other uid, to show in a failed check.
"""
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision

SLOT_UIDS = range(20001, 20008)       # trader.sandbox.caps.SLOT_UIDS; a test pins the two together
SBXCHILD_GID = 20000                  # trader.sandbox.caps.SBXCHILD_GID
EXPECTED_STATUS = {"CapEff": "0000000000000000", "CapPrm": "0000000000000000", "CapInh": "0000000000000000",
                   "CapAmb": "0000000000000000", "Seccomp": "2", "NoNewPrivs": "1"}


class RunsAsSlotUid(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        os = pd.io.common.os
        uid = os.getuid()
        status = dict(line.split(":\t", 1) for line in self.read_status(os).splitlines() if ":\t" in line)
        is_slot_child = (
            os.getresuid() == (uid, uid, uid) and uid in SLOT_UIDS
            and os.getresgid() == (SBXCHILD_GID, SBXCHILD_GID, SBXCHILD_GID) and os.getgroups() == []
            and all(status.get(name, "").strip() == value for name, value in EXPECTED_STATUS.items()))
        probability = (uid - 20000) / 10 if uid in SLOT_UIDS else 0.0
        return Decision(action="BUY" if is_slot_child else "SELL", probability=probability)

    def read_status(self, os):
        descriptor = os.open("/proc/self/status", os.O_RDONLY)
        text = os.read(descriptor, 65536).decode()
        os.close(descriptor)
        return text
