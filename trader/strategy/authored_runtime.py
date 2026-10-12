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
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from trader.authored.api import Decision
from trader.authored.sandbox_client import SandboxFault, SandboxProtocolError, SandboxUnavailable
from trader.authored.streaming import OHLCV, BarStream, bar_fields, decision_signal
from trader.messaging.typed_rpc import TypedRpcError, TypedRpcRemoteError
from trader.objects import BarSize
from trader.research.strategy_key import authored_digest_of_path
from trader.sandbox.protocol import MAX_SESSION_CONIDS, SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR
from trader.strategy.ai_deployment_source import AI_HISTORY_DAYS
from trader.trading.strategy import Signal, Strategy, StrategyContext

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
# The largest LOOKBACK_BARS an authored file may declare (spec 7.1): what a new session is warmed with.
WARMUP_BARS = 2000
SANDBOX_UNAVAILABLE_ALARM = "AUTHORED_SANDBOX_UNAVAILABLE"
SANDBOX_FAULT_ALARM = "AUTHORED_SANDBOX_FAULT"
ISOLATION_ALARM = "AUTHORED_ISOLATION_UNPROVEN"
DIGEST_ALARM = "AUTHORED_DIGEST_MISMATCH"
# The SIGNAL_GAP reason for a decided signal whose session build has no passing proof at dispatch, or no session.
AUTHORED_UNPROVEN = "AUTHORED_BUILD_UNPROVEN"
# Only these are the strategy's fault and retire it; every other sandbox error is an alarm and a retry.
STRATEGY_FAULT_CODES = frozenset({SANDBOX_ERROR, SANDBOX_DIGEST_MISMATCH})
# Errors that end the session and are retried with a new session at a later bar. A protocol error reaching this
# far is a session the sandbox already dropped, not a bad bar: BarStream skips those (pre-flight F1).
_RETRYABLE_ERRORS = (SandboxUnavailable, SandboxProtocolError, OSError)
# Task 10 ruling 3: an instance that raised an unexpected error is re-enabled only after this wait, doubled per
# consecutive crash, so a broken instance does not reopen a session and replay its warm-up every reconcile.
CRASH_BACKOFF_BASE = dt.timedelta(seconds=60)
CRASH_BACKOFF_CAP = dt.timedelta(minutes=30)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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


class _BarWorker:
    """One thread per authored instance that runs its bars in order, off the service loop (final review I2): a
    bar can wait seconds on the sandbox (a session's warm-up, the per-bar proof read). Started at the first bar."""

    def __init__(self) -> None:
        self._jobs: deque[Callable[[], None]] = deque()
        self._wake = threading.Condition()
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    def submit(self, job: Callable[[], None], thread_name: str) -> bool:
        """False once stopped: the job never runs."""
        with self._wake:
            if self._stopped:
                return False
            self._jobs.append(job)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
                self._thread.start()
            self._wake.notify()
        return True

    def stop(self) -> None:
        """Drops the queued jobs and ends the thread after the running one. Does not wait for it: the caller
        may be that job (a fault callback that closes the sessions)."""
        with self._wake:
            self._stopped = True
            self._jobs.clear()
            self._wake.notify()

    def _run(self) -> None:
        while True:
            with self._wake:
                while not self._jobs and not self._stopped:
                    self._wake.wait()
                if self._stopped:
                    return
                job = self._jobs.popleft()
            try:
                job()
            except BaseException:                 # the next bar still runs
                logger.exception("an authored bar failed on its worker thread %s", threading.current_thread().name)


def crash_backoff(consecutive_crashes: int) -> dt.timedelta:
    doublings = max(0, min(consecutive_crashes - 1, 16))
    return min(CRASH_BACKOFF_BASE * 2 ** doublings, CRASH_BACKOFF_CAP)


def _bound_conids(conids: Sequence[int]) -> tuple[int, ...]:
    bound = tuple(int(conid) for conid in conids)
    if not 1 <= len(bound) <= MAX_SESSION_CONIDS or len(set(bound)) != len(bound):
        raise ValueError(f"a live session takes 1 to {MAX_SESSION_CONIDS} distinct conids, got {list(conids)}")
    return bound


class AuthoredLiveStrategy(Strategy):
    def __init__(self, *, client: Any, digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
                 conids: Sequence[int], on_fault: Callable[[str, str], None], proof_gate: Callable[[str], bool],
                 now: Callable[[], dt.datetime] = _utc_now):
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
        self._now = now
        self._current: Optional[_DaySession] = None
        # The worker sends; the reconcile thread may close. One bar at a time on the session (Plan 2 ruling 25).
        self._lock = threading.Lock()
        self._worker = _BarWorker()
        self._faulted = False
        self._closed = False             # close_sessions() is final: no PING, OPEN or BAR after it
        self._future_warned: set[tuple[int, dt.date]] = set()
        self._in_outage = False
        self._in_unproven = False
        self._consecutive_crashes = 0
        self._recover_after: Optional[dt.datetime] = None

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
            if self._faulted or self._closed:
                return None
            day = self._newest_bar_day(conid, prices)
            if day is None:                    # nothing reaches the sandbox: the alarm flags stay as they are
                return None
            try:
                decision = self._decide(conid, prices, day)
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
                self._note_crash()
                raise
            else:
                self._clear_outage_alarm()
                self._in_unproven = False
                self._consecutive_crashes, self._recover_after = 0, None
                return decision_signal(decision, self.name)
        # Outside the lock: the callback may close the sessions or queue work for the reconcile thread.
        self._report_fault(strategy_fault)
        return None

    def authored_binding_intact(self) -> bool:
        """The dispatch check (Plan 4 ruling 20): never after a fault, and only for the digest the deployment bound."""
        bound = getattr(self, "ai_source_digest", None)
        return not (self._faulted or self._closed) and isinstance(bound, str) and hmac.compare_digest(self.digest, bound)

    def session_build_proven(self) -> bool:
        """The dispatch check for a decided signal, held or not (final review minor): the build of the open
        session still has a passing proof. No open session is unproven: fail closed. Not the reconcile check
        (``authored_binding_intact``): an instance without a session is not to be unloaded.

        Never takes the instance lock: the service loop asks this while a bar may hold the lock."""
        current = self._current
        if current is None or self._faulted or self._closed:
            return False
        try:
            return self._proof_gate(current.session.image_digest) is True
        except Exception as exc:
            logger.warning("authored strategy %s: the isolation proof check at dispatch failed: %s", self.name, exc)
            return False

    def may_recover(self) -> bool:
        """False while the wait after an unexpected error runs (ruling 3): the runtime does not re-enable it yet."""
        return self._recover_after is None or self._now() >= self._recover_after

    def run_off_loop(self, job: Callable[[], None]) -> bool:
        """Runs ``job`` on this instance's worker thread after the jobs before it; False once the sessions are
        closed (the job never runs)."""
        return self._worker.submit(job, f"authored-bars-{self.name}")

    def close_sessions(self) -> None:
        """Final: the reconcile thread unloads the instance; no queued bar starts after this, and a bar already
        running (it holds the lock) opens nothing more."""
        self._worker.stop()
        with self._lock:
            self._closed = True
            self._end_session()

    def _newest_bar_day(self, conid: int, prices: pd.DataFrame) -> Optional[dt.date]:
        """The New York date of the newest bar, or None when that bar decides nothing: one the protocol refuses
        (ruling 16), or one dated after today, which would pin the session to a future day."""
        newest = bar_fields(prices.index[-1], prices[list(OHLCV)].iloc[-1].tolist())
        if newest is None:
            return None
        day = newest["t"].astimezone(NEW_YORK).date()
        today = self._now().astimezone(NEW_YORK).date()
        if day <= today:
            return day
        if (conid, day) not in self._future_warned:
            self._future_warned.add((conid, day))
            logger.warning("authored strategy %s: conid %s has a bar dated %s, after today (%s) in New York; "
                           "skipped, no signal", self.name, conid, day, today)
        return None

    def _decide(self, conid: int, prices: pd.DataFrame, day: dt.date) -> Optional[Decision]:
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
        build = self._ping_build()
        self._require_proven(build)                                     # no OPEN (no class load) on an unproven build
        try:
            session = self._client.open_session(self.digest, self._class_name, self._params, self._bar_size,
                                                list(self._bound_conids))
        except SandboxFault as fault:
            self._require_same_build(build, fault)
            raise
        return _DaySession(session, {conid: BarStream(session, conid) for conid in self._bound_conids}, day)

    def _ping_build(self) -> str:
        """A fault answered to PING is not the strategy's: no session of it exists yet."""
        try:
            return self._client.ping().image_digest
        except SandboxFault as fault:
            raise SandboxUnavailable(f"PING answered {fault.code}") from fault

    def _require_same_build(self, build: str, fault: SandboxFault) -> None:
        """Final review I1 (research ruling F7b): a fault answered to OPEN came before any session reported its
        build. A sandbox restarted onto another build must not retire the file, so the build is read again and a
        change, or no answer, is an outage."""
        if fault.code not in STRATEGY_FAULT_CODES:
            return
        try:
            build_now = self._ping_build()
        except _RETRYABLE_ERRORS as exc:
            raise SandboxUnavailable(f"OPEN answered {fault.code} and the build cannot be read again: "
                                     f"{type(exc).__name__}: {exc}") from fault
        if build_now != build:
            raise SandboxUnavailable(f"OPEN answered {fault.code} while the sandbox moved from build {build} "
                                     f"to {build_now}") from fault

    def _require_proven(self, build: Any) -> None:
        """A gate that raises counts as unproven: fail closed."""
        try:
            proven = self._proof_gate(build) is True
        except Exception as exc:
            if not self._in_unproven:                          # once per episode, like the isolation alarm
                logger.warning("authored strategy %s: the isolation proof check for build %s failed: %s",
                               self.name, build, exc)
            proven = False
        if not proven:
            raise _UnprovenBuild(build)

    def _end_session(self) -> None:
        current, self._current = self._current, None
        if current is None:
            return
        try:
            next(iter(current.streams.values())).close()                   # closes the session for every conid
        except Exception as exc:                                           # the session is dropped here either way
            logger.warning("authored strategy %s: closing its live session failed: %s", self.name, exc)

    def _note_crash(self) -> None:
        self._consecutive_crashes += 1
        wait = crash_backoff(self._consecutive_crashes)
        self._recover_after = self._now() + wait
        logger.warning("authored strategy %s raised an unexpected error (%d in a row); not re-enabled for %s",
                     self.name, self._consecutive_crashes, wait)

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
        self.disable()
        self._end_session()                                                # a fault ends the session for every conid

    def _report_fault(self, fault: SandboxFault) -> None:
        alarm = DIGEST_ALARM if fault.code == SANDBOX_DIGEST_MISMATCH else SANDBOX_FAULT_ALARM
        logger.error("%s: authored strategy %s (%s) failed in the sandbox (%s); it stops and is retired, open "
                     "positions keep their stops", alarm, self.name, self.digest, fault.code)
        self._on_fault(self.digest, str(fault.code))


# The proof read runs on an instance's worker before every bar, and on the service loop at each signal's dispatch:
# a short deadline bounds one wait (ruling 1).
PROOF_TIMEOUT_SECONDS = 2.0
RETIRE_TIMEOUT_SECONDS = 15.0
# After a proof read that failed for want of an answer (the trader down, slow or erroring), the gate stays shut
# this long without asking: a 2 s wait per bar per conid would stall the instance's bars and each dispatch.
PROOF_OUTAGE_HOLD = dt.timedelta(seconds=20)
# Ruling 2: a retryable refusal (AUTHORED_WITHDRAWAL_FAILED, WITHDRAWAL_ENTRY_IN_FLIGHT) or a remote error is sent
# again at most this often per digest.
RETIRE_RESEND_CAP = 20
# The trader did not get the request: always sent again, never counted.
UNREACHABLE_ERRORS = (ConnectionError, TimeoutError, OSError)
# The trader's journal was unavailable before the record moved (trader.messaging.authored_surface, not imported:
# it brings the trader's stores into this process). The record is still DEPLOYED: always sent again, never counted.
STORE_UNAVAILABLE = "AUTHORED_STORE_UNAVAILABLE"
RETIRE_DETAILS = {
    SANDBOX_ERROR: "the live session failed in the sandbox",
    SANDBOX_DIGEST_MISMATCH: "the deployment or the stored file is not the authored digest",
}


def proof_passes(reply: Any, image_digest: str) -> bool:
    """Plan 3's get_isolation_proof reply; its shape wins and this is the one place that reads it."""
    if not isinstance(reply, Mapping):
        return False
    proof = reply.get("proof")
    return (reply.get("found") is True and isinstance(proof, Mapping) and proof.get("passed") is True
            and proof.get("image_digest") == image_digest)


class AuthoredDeployments:
    """Builds the live proxy for an authored deployment behind the digest and isolation gates, and sends the
    retirements its faults queue. No authored file is read or run in this process (SP3 spec 4)."""

    def __init__(self, *, client: Any, trader_query: Any, trader_command: Any,
                 now: Callable[[], dt.datetime] = _utc_now):
        self._client, self._query, self._command, self._now = client, trader_query, trader_command, now
        self._lock = threading.Lock()
        # In memory only (Plan 4 ruling 18 accepts it): after a restart before delivery the record is still
        # DEPLOYED, so the file loads again, and a deterministic fault fires and queues the retirement again.
        self._retiring: dict[str, str] = {}           # digest -> fault code, until the trader answers
        self._resends: dict[str, int] = {}            # digest -> refusals and remote errors so far
        self._retired: set[str] = set()               # answered or given up: never loaded again here
        self._gate_episode: Optional[tuple[str, str]] = None   # the failing (kind, build) last alarmed
        self._gate_shut_until: Optional[dt.datetime] = None     # no proof read before this, after a failed read

    def build(self, name: str, deployment: Any, *, storage: Any,
              universe_accessor: Any) -> Optional[AuthoredLiveStrategy]:
        """The instance for one active authored deployment, or None (logged) when a gate refuses it or it cannot
        be built. Never raises: one authored row must not stop the other deployments' reconcile."""
        try:
            return self._build(name, deployment, storage=storage, universe_accessor=universe_accessor)
        except Exception as exc:
            logger.error("authored deployment %s (%s) is not loaded: %s: %s; the next reconcile retries", name,
                         getattr(deployment, "version_digest", "?"), type(exc).__name__, exc, exc_info=True)
            return None

    def _build(self, name: str, deployment: Any, *, storage: Any,
               universe_accessor: Any) -> Optional[AuthoredLiveStrategy]:
        digest = authored_digest_of_path(deployment.strategy_path)
        if self._stopped(digest):
            logger.info("authored deployment %s is not loaded: %s was retired in this process", name, digest)
            return None
        if not hmac.compare_digest(digest, deployment.strategy_digest):
            logger.error("%s: deployment %s binds %s to %s; not loaded, the file is retired", DIGEST_ALARM,
                         deployment.version_digest, deployment.strategy_digest, deployment.strategy_path)
            self.request_retirement(digest, SANDBOX_DIGEST_MISMATCH)
            return None
        try:
            build = self._client.ping().image_digest
        except _RETRYABLE_ERRORS as exc:
            logger.error("%s: the live sandbox does not answer (%s: %s); authored deployment %s waits",
                         SANDBOX_UNAVAILABLE_ALARM, type(exc).__name__, exc, name)
            return None
        if not self.is_proven(build):                 # a fresh trader read; no cache (review r2 #4)
            return None
        instance = AuthoredLiveStrategy(client=self._client, digest=digest, class_name=deployment.class_name,
                                        params=deployment.params, bar_size=deployment.bar_size,
                                        conids=deployment.conids, on_fault=self.request_retirement,
                                        proof_gate=self.is_proven, now=self._now)
        instance.install(StrategyContext(
            name=name, bar_size=BarSize.parse_str(deployment.bar_size), conids=list(deployment.conids),
            universe=None, historical_days_prior=AI_HISTORY_DAYS, paper_only=True, storage=storage,
            universe_accessor=universe_accessor, logger=logger, module=deployment.strategy_path,
            class_name=deployment.class_name, description=f"authored AI deployment {deployment.version_digest}",
            params=dict(deployment.params)))
        instance.ai_deployment_version = deployment.version_digest
        instance.ai_deployment_digest = deployment.base_digest
        instance.ai_source_digest = deployment.strategy_digest
        instance.ai_source_path = deployment.strategy_path
        instance.enable()
        return instance

    def request_retirement(self, digest: str, code: str) -> None:
        """Called on an instance's worker thread: record only, never raise; the reconcile thread sends (Plan 4 ruling 18)."""
        with self._lock:
            if digest not in self._retired:
                self._retiring.setdefault(digest, code)

    def send_retirements(self) -> None:
        with self._lock:
            pending = list(self._retiring.items())
        for digest, code in pending:
            if self._send_retirement(digest, code):
                with self._lock:
                    self._retiring.pop(digest, None)
                    self._resends.pop(digest, None)
                    self._retired.add(digest)

    def _stopped(self, digest: str) -> bool:
        with self._lock:
            return digest in self._retiring or digest in self._retired

    def _send_retirement(self, digest: str, code: str) -> bool:
        """Plan 3's retire_authored_strategy; True when no resend is due."""
        body = {"digest": digest, "code": code, "detail": RETIRE_DETAILS[code]}
        try:
            reply = self._command.call("retire_authored_strategy", body, dict, timeout=RETIRE_TIMEOUT_SECONDS)
        except UNREACHABLE_ERRORS as exc:
            logger.warning("retire_authored_strategy for %s not delivered (%s); sent again next reconcile",
                           digest, type(exc).__name__)
            return False
        except TypedRpcRemoteError as remote:
            if remote.code == STORE_UNAVAILABLE:
                logger.warning("retire_authored_strategy for %s: the trader's store is unavailable (%s); sent "
                               "again next reconcile", digest, remote.message)
                return False
            return self._resend_due(digest, remote.code, remote.message)
        except TypedRpcError as exc:
            return self._resend_due(digest, type(exc).__name__, str(exc))
        if not isinstance(reply, Mapping):
            return self._resend_due(digest, "MALFORMED_REPLY", repr(reply)[:200])
        if reply.get("status") in ("UPDATED", "UNCHANGED"):
            logger.info("authored strategy %s is retired at the trader (%s)", digest, reply.get("status"))
            return True
        if reply.get("retryable") is True:
            return self._resend_due(digest, reply.get("code"), reply.get("detail"))
        logger.error("%s: the trader refused to retire %s (%s: %s); not sent again", SANDBOX_FAULT_ALARM,
                     digest, reply.get("code"), reply.get("detail"))
        return True

    def _resend_due(self, digest: str, code: Any, detail: Any) -> bool:
        """Counts one answer that asks for a resend; True (give up) once the cap is reached."""
        with self._lock:
            sent = self._resends[digest] = self._resends.get(digest, 0) + 1
        if sent < RETIRE_RESEND_CAP:
            logger.warning("retire_authored_strategy for %s answered %s (%s); sent again next reconcile "
                           "(%d of %d)", digest, code, detail, sent, RETIRE_RESEND_CAP)
            return False
        logger.error("%s: retire_authored_strategy for %s still answers %s (%s) after %d sends; not sent again "
                     "and not loaded again by this process. Check the record at the trader (`mmr ai-author "
                     "list`): it may not be RETIRED or its version may still be active", SANDBOX_FAULT_ALARM,
                     digest, code, detail, sent)
        return True

    def is_proven(self, build: Optional[str]) -> bool:
        """Review r2 #4: the authoritative latest proof for ``build``, read from the trader on every call (no
        cache of a pass or of any answer), so a proof withdrawn for a build already running stops the next OPEN
        and the next bar. A read failure, a refusal or a timeout is fail-closed (False), never raised, alarms once
        per episode, and shuts the gate for PROOF_OUTAGE_HOLD without asking again. The live proxy holds this
        method as its gate: before every new session and on every bar."""
        if build is None or self._gate_shut():
            return False
        try:
            reply = self._query.call("get_isolation_proof", {"image_digest": build}, dict,
                                     timeout=PROOF_TIMEOUT_SECONDS)
        except TypedRpcRemoteError as remote:
            self._shut_gate()
            self._gate_failed(("refused", build), "get_isolation_proof refused (%s: %s)", remote.code,
                              remote.message)
            return False
        except Exception as exc:                      # a timeout, an unreachable trader or a client bug
            self._shut_gate()
            self._gate_failed(("unreadable", build), "the isolation proof is not readable (%s: %s)",
                              type(exc).__name__, exc)
            return False
        with self._lock:
            self._gate_shut_until = None              # the trader answered: every next call reads again
        if not proof_passes(reply, build):
            self._gate_failed(("unproven", build), "no passing isolation proof for sandbox build %s; run "
                              "`mmr ai-author prove-isolation`", build)
            return False
        with self._lock:
            self._gate_episode = None
        return True

    def _gate_shut(self) -> bool:
        with self._lock:
            return self._gate_shut_until is not None and self._now() < self._gate_shut_until

    def _shut_gate(self) -> None:
        with self._lock:
            self._gate_shut_until = self._now() + PROOF_OUTAGE_HOLD

    def _gate_failed(self, episode: tuple[str, str], message: str, *args: Any) -> None:
        with self._lock:
            first = self._gate_episode != episode
            self._gate_episode = episode
        if first:
            logger.error("%s: " + message + "; authored deployments give no signal", ISOLATION_ALARM, *args)
