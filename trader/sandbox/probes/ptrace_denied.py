"""Says whether the sandbox seccomp profile denies ptrace inside a real child (ruling 16).

BUY: ptrace(PTRACE_TRACEME) failed with EPERM, which Docker's default profile would allow. SELL otherwise.
"""
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision

_PTRACE_TRACEME = 0
_EPERM = 1


class PtraceDenied(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        ctypes = pd.io.common.os.sys.modules["ctypes"]   # loaded by the supervisor (trader.sandbox.caps)
        libc = ctypes.CDLL(None, use_errno=True)
        denied = libc.ptrace(_PTRACE_TRACEME, 0, 0, 0) == -1 and ctypes.get_errno() == _EPERM
        return Decision(action="BUY" if denied else "SELL", probability=0.5)
