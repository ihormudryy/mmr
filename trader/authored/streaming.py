"""Bars to a sandbox session and decisions back (SP3 spec 7.6), shared by the research and the live caller.

Both callers send the same fields the same way, so a strategy sees the same bars in a backtest and live. Every
send goes through BarStream: it is what keeps a caller inside the protocol (Plan 4 ruling 16).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Optional, Sequence

import pandas as pd

from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.objects import Action
from trader.sandbox.protocol import PROTOCOL_VERSION, FrameError, parse_request
from trader.trading.strategy import Signal

logger = logging.getLogger(__name__)
OHLCV = ("open", "high", "low", "close", "volume")
# A Decision carries no risk; Jev and the risk gate size the trade (Plan 4 ruling 15).
AUTHORED_SIGNAL_RISK = 0.0
# Only the bar rules matter to bar_fields; the session and conid of the checked frame are placeholders.
_PLACEHOLDER_SESSION = "s0"
_PLACEHOLDER_CONID = 1


def utc_time(stamp: Any) -> dt.datetime:
    moment = pd.Timestamp(stamp)
    moment = moment.tz_localize("UTC") if moment.tzinfo is None else moment.tz_convert("UTC")
    return moment.to_pydatetime()


def bar_fields(stamp: Any, values: Sequence[Any]) -> Optional[dict]:
    """The BAR fields of one completed bar, or None when the sandbox protocol would refuse the bar.

    The protocol's own parser decides (finite numbers, high >= low, volume >= 0, a time from 1970 to 2100), so a
    bar sent here is never refused by the client (Plan 4 pre-flight F1)."""
    if any(isinstance(value, (bool, str)) for value in values):   # the client sends numbers only
        return None
    try:
        numbers = [float(value) for value in values]
        moment = utc_time(stamp)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(numbers) != len(OHLCV) or not all(math.isfinite(number) for number in numbers):
        return None
    fields = {"t": moment, **dict(zip(OHLCV, numbers))}
    try:
        parse_request({"v": PROTOCOL_VERSION, "type": "BAR", "session": _PLACEHOLDER_SESSION,
                       "conid": _PLACEHOLDER_CONID, **fields, "t": moment.isoformat()})
    except FrameError:
        return None
    return fields


class BarStream:
    """One conid of a sandbox session, fed strictly in time order. A bar that cannot be sent decides nothing.

    A live session holds every conid of its deployment, so several streams may share one session; a research
    session holds one (Plan 2 ruling 26)."""

    def __init__(self, session: Any, conid: int):
        self.session = session
        self.conid = conid
        self.last_sent: Optional[dt.datetime] = None

    def send(self, stamp: Any, values: Sequence[Any]) -> Optional[Decision]:
        fields = bar_fields(stamp, values)
        if fields is None or (self.last_sent is not None and fields["t"] <= self.last_sent):
            return None
        decision = self.session.bar(self.conid, **fields)
        self.last_sent = fields["t"]
        return decision

    def send_frame(self, frame: pd.DataFrame) -> tuple[Optional[Decision], ...]:
        """One decision per row of ``frame``, in row order."""
        rows = frame.loc[:, list(OHLCV)].itertuples(index=False, name=None)
        return tuple(self.send(stamp, values) for stamp, values in zip(frame.index, rows))

    def close(self) -> None:
        """Best effort: closes the whole session (every conid); a session whose child already died has nothing
        left to close."""
        try:
            self.session.close()
        except (SandboxFault, SandboxUnavailable) as exc:
            logger.debug("sandbox session close: %s", exc)


def decision_signal(decision: Optional[Decision], source_name: str) -> Optional[Signal]:
    if decision is None:
        return None
    action = Action.BUY if decision.action == "BUY" else Action.SELL
    return Signal(source_name=source_name, action=action, probability=float(decision.probability),
                  risk=AUTHORED_SIGNAL_RISK)
