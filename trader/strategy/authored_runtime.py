"""Authored strategies in the strategy service (SP3 spec 7.6, 7.9, 8). No authored code runs in this process.

``AuthoredLiveStrategy`` is the instance the runtime dispatches for one deployment: it holds one session on the live
sandbox socket for the New York trading day, opened with every conid the deployment binds, and streams each
completed bar of each conid to it (decision D1; spec 7.5 as amended). ``AuthoredDeployments`` builds
it only when the running sandbox image has a passing isolation proof and the deployment's digest is the file's,
and asks the trader to retire it after a sandbox fault. Positions are never touched here: they keep their stops.
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxProtocolError, SandboxUnavailable
from trader.authored.streaming import OHLCV, BarStream, bar_fields, decision_signal
from trader.sandbox.protocol import MAX_SESSION_CONIDS, SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR
from trader.trading.strategy import Signal, Strategy

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
# The largest LOOKBACK_BARS an authored file may declare (spec 7.1): what a new session is warmed with.
WARMUP_BARS = 2000
SANDBOX_UNAVAILABLE_ALARM = "AUTHORED_SANDBOX_UNAVAILABLE"
SANDBOX_FAULT_ALARM = "AUTHORED_SANDBOX_FAULT"
ISOLATION_ALARM = "AUTHORED_ISOLATION_UNPROVEN"
DIGEST_ALARM = "AUTHORED_DIGEST_MISMATCH"
# Only these are the strategy's fault and retire it; every other sandbox error is an alarm and a retry.
STRATEGY_FAULT_CODES = frozenset({SANDBOX_ERROR, SANDBOX_DIGEST_MISMATCH})
# Errors that end the session and are retried with a new session at a later bar. A protocol error reaching this
# far is a session the sandbox already dropped, not a bad bar: BarStream skips those (pre-flight F1).
_RETRYABLE_ERRORS = (SandboxUnavailable, SandboxProtocolError, OSError)


class _UnprovenBuild(Exception):
    def __init__(self, build: Any):
        super().__init__(build)
        self.build = build


@dataclass
class _DaySession:
    """One live session for one New York day: one BarStream per conid of the deployment, all on one session."""
    session: Any
    streams: dict[int, BarStream]
    day: dt.date


def _unsent(prices: pd.DataFrame, last_sent: Optional[dt.datetime]) -> pd.DataFrame:
    """The bars a conid's stream has not had yet; its first bar in a session brings the trailing WARMUP_BARS
    (Plan 4 ruling 14)."""
    if last_sent is None:
        return prices.tail(WARMUP_BARS)
    index = pd.DatetimeIndex(prices.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return prices[index > pd.Timestamp(last_sent)]


def _bound_conids(conids: Sequence[int]) -> tuple[int, ...]:
    bound = tuple(int(conid) for conid in conids)
    if not 1 <= len(bound) <= MAX_SESSION_CONIDS or len(set(bound)) != len(bound):
        raise ValueError(f"a live session takes 1 to {MAX_SESSION_CONIDS} distinct conids, got {list(conids)}")
    return bound


class AuthoredLiveStrategy(Strategy):
    def __init__(self, *, client: Any, digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
                 conids: Sequence[int], on_fault: Callable[[str, str], None], proof_gate: Callable[[str], bool]):
        super().__init__()
        # Plan 4 ruling 19 (review r1 #6): True only for a build with a passing isolation proof. Asked before
        # every OPEN for the build PING reported, and on every bar for the build the session's OPENED reported,
        # so a first OPEN, a reconnect, a day rollover or a proof withdrawn mid-session never sends a bar to an
        # unproven build.
        self._proof_gate = proof_gate
        self.digest = digest
        # Not the base class's ``conids`` property: its setter drops the value until install (pre-flight F3).
        self._bound_conids = _bound_conids(conids)
        self._client, self._class_name, self._params, self._bar_size = client, class_name, dict(params), bar_size
        self._on_fault = on_fault
        self._current: Optional[_DaySession] = None
        # The tick thread sends; the reconcile thread may close. One bar at a time on the session (Plan 2 ruling 25).
        self._lock = threading.Lock()
        self._faulted = False
        self._in_outage = False
        self._in_unproven = False

    @property
    def bound_conids(self) -> tuple[int, ...]:
        return self._bound_conids

    def on_prices(self, prices: pd.DataFrame) -> Optional[Signal]:
        conid = self.dispatch_conid
        if conid is None or prices is None or prices.empty:
            return None
        if conid not in self._bound_conids:
            logger.warning("authored strategy %s was dispatched conid %s, which its deployment does not bind; "
                           "no signal", self.name, conid)
            return None
        with self._lock:
            if self._faulted:
                return None
            try:
                decision = self._decide(conid, prices)
            except SandboxFault as fault:
                if fault.code not in STRATEGY_FAULT_CODES:
                    self._end_session_with_outage(fault)
                    return None
                self._stop_after_fault()
                strategy_fault = fault          # ``fault`` is unbound once the except block ends
            except _UnprovenBuild as unproven:
                self._end_session()
                self._raise_unproven_alarm(unproven.build)
                return None
            except _RETRYABLE_ERRORS as exc:
                self._end_session_with_outage(exc)
                return None
            except BaseException:
                self._end_session()        # an unexpected error must not keep one of the 3 live slots
                raise
            else:
                self._clear_outage_alarm()
                self._in_unproven = False
                return decision_signal(decision, self.name)
        # Outside the lock: the callback may close the sessions or queue work for the reconcile thread.
        self._report_fault(strategy_fault)
        return None

    def authored_binding_intact(self) -> bool:
        """The dispatch check (Plan 4 ruling 20): never after a fault, and only for the digest the deployment bound."""
        bound = getattr(self, "ai_source_digest", None)
        return not self._faulted and isinstance(bound, str) and hmac.compare_digest(self.digest, bound)

    def close_sessions(self) -> None:
        with self._lock:
            self._end_session()

    def _decide(self, conid: int, prices: pd.DataFrame) -> Optional[Decision]:
        newest = bar_fields(prices.index[-1], prices[list(OHLCV)].iloc[-1].tolist())
        if newest is None:              # it decides nothing, and its time must not roll the session (ruling 16)
            return None
        day = newest["t"].astimezone(NEW_YORK).date()
        if self._current is not None and day > self._current.day:      # a conid still on yesterday never rolls back
            self._end_session()
        if self._current is None:
            self._current = self._open_day_session(day)
        # The OPENED build, checked on every bar: a restart between PING and OPEN, or a proof withdrawn
        # mid-session, sends nothing (fail closed).
        self._require_proven(self._current.session.image_digest)
        stream = self._current.streams[conid]
        decisions = stream.send_frame(_unsent(prices, stream.last_sent))
        return decisions[-1] if decisions else None

    def _open_day_session(self, day: dt.date) -> _DaySession:
        self._require_proven(self._client.ping().image_digest)          # no OPEN (no class load) on an unproven build
        session = self._client.open_session(self.digest, self._class_name, self._params, self._bar_size,
                                            list(self._bound_conids))
        return _DaySession(session, {conid: BarStream(session, conid) for conid in self._bound_conids}, day)

    def _require_proven(self, build: Any) -> None:
        """A gate that raises counts as unproven: fail closed."""
        try:
            proven = self._proof_gate(build) is True
        except Exception as exc:
            logger.warning("authored strategy %s: the isolation proof check for build %s failed: %s",
                           self.name, build, exc)
            proven = False
        if not proven:
            raise _UnprovenBuild(build)

    def _end_session(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            next(iter(current.streams.values())).close()                   # closes the session for every conid

    def _end_session_with_outage(self, exc: BaseException) -> None:
        self._end_session()
        self._raise_outage_alarm(exc)

    def _raise_unproven_alarm(self, build: Any) -> None:
        if not self._in_unproven:
            self._in_unproven = True
            logger.error("%s: authored strategy %s: the live sandbox runs build %s, which has no passing isolation "
                         "proof; no signal until `mmr ai-author prove-isolation` passes for it", ISOLATION_ALARM,
                         self.name, build)

    def _raise_outage_alarm(self, exc: BaseException) -> None:
        if not self._in_outage:
            self._in_outage = True
            logger.error("%s: authored strategy %s gets no decision from the live sandbox (%s: %s); no signal, it "
                         "reconnects on the next bar and stays deployed", SANDBOX_UNAVAILABLE_ALARM, self.name,
                         type(exc).__name__, exc)

    def _clear_outage_alarm(self) -> None:
        if self._in_outage:
            self._in_outage = False
            logger.info("authored strategy %s: the live sandbox answers again", self.name)

    def _stop_after_fault(self) -> None:
        self._faulted = True
        self._end_session()                                                # a fault ends the session for every conid
        self.disable()

    def _report_fault(self, fault: SandboxFault) -> None:
        alarm = DIGEST_ALARM if fault.code == SANDBOX_DIGEST_MISMATCH else SANDBOX_FAULT_ALARM
        logger.error("%s: authored strategy %s (%s) failed in the sandbox (%s); it stops and is retired, open "
                     "positions keep their stops", alarm, self.name, self.digest, fault.code)
        self._on_fault(self.digest, str(fault.code))
