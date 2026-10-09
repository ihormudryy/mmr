"""Nightly shadow replay (SP2c spec 7): every judged strategy, on the bar size its judgment is bound to, with the
same backtester, cost model and live rules. Warm-up bars feed state only. Each session's row is the change since
the previous row of one continuous run that starts on the first session after the judgment.

A row is final once the trader stored it: a session sent INCOMPLETE is never sent again with numbers. A row the
trader refuses for good is kept in ``shadow_failures`` and never resent. A case or judgment that can never be
replayed (a tampered judgment, a bad case file) is parked loudly; the worker keeps running for the others.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from trader.data.data_access import TickStorage
from trader.data.store import DateRange
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.objects import BarSize
from trader.research.evaluation import EvaluationError, _costs_config_digest, _point_key
from trader.research.evaluation_case import CaseRefused, load_verified_case
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_window_job
from trader.research.judgment_attest import is_loud_trader_code
from trader.research.shadow_window import (TRACKED_VERDICTS, session_close_utc, session_open_utc, sessions_before,
                                           shadow_window, xnys_sessions)
from trader.research.strategy_key import split_strategy_key
from trader.research.trader_port import TraderUnavailable

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
JOIN_LOOKBACK = dt.timedelta(days=30)
TICK_SECONDS = 300.0
# Sent only this long after the close, so a trader clock a little behind ours never sees an open session.
SEND_AFTER_CLOSE = dt.timedelta(minutes=30)
# The typed RPC server's answer when the request body fails the wire model: this row, not the service, is bad.
REJECTED_BODY = ("VALIDATION_ERROR", "invalid request body")
# A trader without ai_paper registers no judgment or shadow methods: an operator matter, not a research bug.
NOT_SERVED_CODE = "METHOD_NOT_ALLOWED"
REASON_MAX = 200
# Inside a session a row tolerates one missing bar, not a longer hole.
MAX_GAP_BARS = 2
_BAR = re.compile(r"^(\d+) (sec|secs|min|mins|hour|hours)$")
_UNIT_SECONDS = {"sec": 1, "secs": 1, "min": 60, "mins": 60, "hour": 3600, "hours": 3600}


def bar_seconds(bar_size: str) -> int:
    match = _BAR.match(bar_size)
    if match is None:
        raise ValueError(f"unknown bar size {bar_size!r}")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def ny_day_start(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=NEW_YORK).astimezone(dt.timezone.utc)


def before_close(session: dt.date) -> dt.datetime:
    """The last instant of the regular session. Reads include their end, and a bar stamped at the close (bars
    are stamped at their start) is the first after-hours bar, which no row may use."""
    return session_close_utc(session) - dt.timedelta(microseconds=1)


def _ny_dates(stamps) -> list[dt.date]:
    index = pd.DatetimeIndex(stamps)
    index = index.tz_localize("UTC") if index.tz is None else index
    return list(index.tz_convert(NEW_YORK).date)


def _is_rejected_body(remote: TypedRpcRemoteError) -> bool:
    code, prefix = REJECTED_BODY
    return remote.code == code and remote.message.startswith(prefix)


def _aware(raw: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{raw!r} has no offset")
    return value


def _utc_stamps(frame: Optional[pd.DataFrame]) -> pd.DatetimeIndex:
    if frame is None or len(frame) == 0:
        return pd.DatetimeIndex([], tz="UTC")
    stamps = pd.DatetimeIndex(frame.index)
    return stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")


def _session_bar_problem(stamps: pd.DatetimeIndex, conid, bar_size: str, spacing: pd.Timedelta,
                         session: dt.date) -> Optional[str]:
    session_open = pd.Timestamp(session_open_utc(session))
    session_close = pd.Timestamp(session_close_utc(session))
    # From New York midnight to the close; a bar stamped at the close is the first after-hours bar.
    day = stamps[(stamps >= pd.Timestamp(ny_day_start(session))) & (stamps < session_close)]
    if len(day) == 0:
        return f"BARS_MISSING: no {bar_size} bars for conid {conid} on {session}"
    if (day.to_series().diff().dropna() < spacing).any():
        return f"BAR_SIZE_MISMATCH: conid {conid} has bars closer than {bar_size} on {session}"
    regular = day[day >= session_open]                    # pre-market bars neither open a gap nor fill one
    if len(regular) == 0 or regular.min() > session_open + spacing:
        first = "none" if len(regular) == 0 else regular.min()
        return f"BARS_MISSING: conid {conid} has its first {bar_size} bar at {first} on {session}"
    steps = regular.to_series().diff().dropna()
    if (steps > MAX_GAP_BARS * spacing).any():
        return f"BARS_MISSING: conid {conid} has a gap of {steps.max()} in its {bar_size} bars on {session}"
    if regular.max() < session_close - spacing:
        return f"BARS_MISSING: conid {conid} has no {bar_size} bar up to the close on {session}"
    return None


class _Wait(Exception):
    """The row is not final yet (an INCOMPLETE row waits for its deadline, ruling 11)."""


class _Unusable(Exception):
    """This case or judgment can never be replayed; the operator must look."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class ShadowReplay:
    def __init__(self, *, store: Any, trader: Any, signer: Any, artifacts_root: Path, paths: Any, registry: Any,
                 config: Any, judge: Any, now: Callable[[], dt.datetime], run_job=run_window_job):
        self._store, self._trader, self._signer = store, trader, signer
        self._cases_dir = Path(artifacts_root) / "cases"
        self._paths, self._registry, self._config, self._judge = paths, registry, config, judge
        self._now, self._run_job = now, run_job
        self._parked: dict[str, str] = {}               # case digest -> "CODE: detail"; again after a restart

    def serve_forever(self, stop: threading.Event) -> None:
        """Any error other than an unreachable trader or a trader without ai_paper ends the worker, and with it
        the service (Task 8)."""
        while not stop.is_set():
            try:
                self.tick()
            except TraderUnavailable as exc:
                logger.warning("shadow replay waits for the trader: %s", exc)
            except TypedRpcRemoteError as remote:
                if remote.code != NOT_SERVED_CODE:
                    raise
                logger.error("shadow replay: the trader does not serve this call (%s: %s); is ai_paper enabled "
                             "on the trader? Retrying next tick", remote.code, remote.message)
            stop.wait(TICK_SECONDS)

    def status(self) -> dict:
        """Ruling 25: refused rows and parked cases are counted, never hidden among the sent ones."""
        return {"members": len(self._store.shadow_members()), "failed_rows": len(self._store.shadow_failures()),
                "parked": dict(self._parked)}

    def tick(self) -> None:
        self._join_new_members()
        for member in self._store.shadow_members():
            if member["case_digest"] in self._parked:
                continue
            with self._parking(member["case_digest"]):
                self._replay(member)

    # -- parking -------------------------------------------------------------------------

    @contextmanager
    def _parking(self, case_digest: str) -> Iterator[None]:
        try:
            yield
        except (CaseRefused, _Unusable) as refused:
            self._park(case_digest, refused.code, refused.detail)
        except TypedRpcRemoteError as remote:
            if not is_loud_trader_code(remote.code):
                raise
            self._park(case_digest, remote.code, remote.message)

    def _park(self, case_digest: str, code: str, detail: str) -> None:
        logger.error("shadow replay parked case %s: %s: %s; it is skipped until the operator fixes it",
                     case_digest, code, detail)
        self._parked[case_digest] = f"{code}: {detail}"[:REASON_MAX]

    # -- joining -------------------------------------------------------------------------

    def _join_new_members(self) -> None:
        for row in self._store.cases_without_member(self._now() - JOIN_LOOKBACK):
            if row["case_digest"] in self._parked:
                continue
            with self._parking(row["case_digest"]):
                self._join(row["case_digest"])

    def _join(self, case_digest: str) -> None:
        judgment = self._trader.judgment(case_digest=case_digest)
        if judgment is None or judgment["verdict"] not in TRACKED_VERDICTS:
            return                                                   # unjudged yet, or NO_VERDICT (ruling 8)
        if judgment["case_digest"] != case_digest:
            raise _Unusable("SHADOW_JUDGMENT_MISMATCH", f"the judgment names case {judgment['case_digest']}")
        case = self._load_case(case_digest)
        bound_bar_size = (judgment.get("binding") or {}).get("bar_size")
        if bound_bar_size != case.bar_size:
            raise _Unusable("SHADOW_BAR_SIZE_MISMATCH",
                            f"judgment bound to {bound_bar_size!r}, case uses {case.bar_size!r}")
        try:
            recorded_at = _aware(judgment["recorded_at"])
            cooldown = judgment.get("cooldown_until_session")
            first, last = shadow_window(
                recorded_at, judgment["verdict"], deploy_expiry_sessions=self._judge.deploy_expiry_sessions,
                cooldown_until_session=None if cooldown is None else dt.date.fromisoformat(cooldown))
        except (KeyError, TypeError, ValueError) as exc:
            raise _Unusable("SHADOW_WINDOW_INVALID", str(exc)) from None
        self._store.add_member(judgment["judgment_id"], case_digest, judgment["verdict"], recorded_at, first, last,
                               self._now())

    def _load_case(self, case_digest: str) -> Any:
        return load_verified_case(self._cases_dir, case_digest, {self._signer.public_key_id: self._signer.public_key})

    # -- replaying -----------------------------------------------------------------------

    def _due_sessions(self, member: dict) -> list[dt.date]:
        closed_by = self._now() - SEND_AFTER_CLOSE
        done = self._store.sent_sessions(member["judgment_id"]) | self._store.failed_sessions(member["judgment_id"])
        return [session for session in xnys_sessions(member["first_session"], member["last_session"])
                if session_close_utc(session) <= closed_by and session not in done]

    def _replay(self, member: dict) -> None:
        """Sessions go in order; a session that waits holds back the later ones."""
        case = None
        for session in self._due_sessions(member):
            body = self._store.queued_row(member["judgment_id"], session)
            if body is None:
                case = case or self._load_case(member["case_digest"])
                try:
                    body = self._row(member, case, session)
                except _Wait:
                    return
                self._store.queue_row(member["judgment_id"], session, body, self._now())
            if not self._send(member["judgment_id"], session, body):
                return

    def _send(self, judgment_id: str, session: dt.date, body: dict) -> bool:
        """True when the next session may go: this one was stored, or refused for good."""
        try:
            reply = self._trader.record_shadow(body)
        except TypedRpcRemoteError as remote:
            if not _is_rejected_body(remote):
                raise
            reply = {"status": "REFUSED", "code": remote.code, "detail": remote.message, "retryable": False}
        if reply.get("status") in ("INSERTED", "DUPLICATE"):
            self._store.mark_sent(judgment_id, session, body["status"], reply["status"], self._now())
            return True
        code = reply.get("code") or "REFUSED_WITHOUT_CODE"
        if reply.get("retryable"):
            logger.warning("shadow row %s %s refused for now (%s); sent again next tick", judgment_id, session, code)
            return False
        logger.error("shadow row %s %s refused for good: %s: %s; kept in shadow_failures and never resent",
                     judgment_id, session, code, reply.get("detail"))
        self._store.mark_failed(judgment_id, session, code, reply.get("detail"), self._now())
        return True

    def _row(self, member: dict, case: Any, session: dt.date) -> dict:
        family = self._family(case)
        evidence = case.evidence
        first = member["first_session"]
        warm_start = sessions_before(first, int(evidence["warmup_sessions"]))
        problem = (self._source_problem(case) or self._family_problem(case, family) or self._costs_problem(family)
                   or self._bar_problem(case.conids, case.bar_size, session, inputs_from=warm_start))
        if problem is not None:
            return self._incomplete(member, case, session, problem)
        replay_index = evidence["replay_index"]
        points = evidence.get("points") or []
        params = points[replay_index]["params"] if points else case.cohort[replay_index]
        env = self._environment(case, family)
        job = WindowJob(_point_key(params), dict(params), "shadow", 0, ny_day_start(warm_start),
                        before_close(session), 1.0, trading_start=ny_day_start(first))
        try:
            outcome = self._run_job(env, job)
        except Exception as exc:                        # strategy code or data: the row says why, the log says more
            logger.exception("shadow replay of %s on %s failed", member["judgment_id"], session)
            return self._incomplete(member, case, session, f"REPLAY_ERROR: {type(exc).__name__}")
        return self._numbers(member, case, session, env, outcome)

    def _numbers(self, member: dict, case: Any, session: dt.date, env: RunEnvironment, outcome: Any) -> dict:
        curve = outcome.equity_series()
        dates = _ny_dates(curve.index)
        today = [float(value) for day, value in zip(dates, curve.values) if day == session]
        if not today:                                   # every bar before trading_start: never a zero-P&L row
            return self._incomplete(member, case, session, f"NO_TRADING_BARS: no equity on {session}")
        before = [float(value) for day, value in zip(dates, curve.values) if day < session]
        start_equity = before[-1] if before else env.account_equity
        trades = [t for t in outcome.trades if _ny_dates([t["timestamp"]])[0] == session]
        end_equity = today[-1]
        pnl, fees = end_equity - start_equity, float(sum(t["commission"] for t in trades))
        if not all(math.isfinite(x) for x in (end_equity, pnl, fees)):
            return self._incomplete(member, case, session, "REPLAY_ERROR: a non-finite equity or fee")
        return self._body(member, case, session, pnl_usd=pnl, fees_usd=fees, trades=len(trades),
                          end_equity_usd=end_equity)

    def _incomplete(self, member: dict, case: Any, session: dt.date, reason: str) -> dict:
        deadline = session_close_utc(session) + dt.timedelta(hours=self._config.shadow_incomplete_after_hours)
        if self._now() < deadline:
            raise _Wait(reason)
        return self._body(member, case, session, status="INCOMPLETE", reason=reason[:REASON_MAX])

    @staticmethod
    def _body(member: dict, case: Any, session: dt.date, **values: Any) -> dict:
        row = {"judgment_id": member["judgment_id"], "case_digest": member["case_digest"],
               "verdict": member["verdict"], "session_date": session.isoformat(), "bar_size": case.bar_size,
               "status": "COMPLETE", "reason": None, "pnl_usd": None, "fees_usd": None, "trades": None,
               "end_equity_usd": None}
        row.update(values)
        return row

    def _source_problem(self, case: Any) -> Optional[str]:
        path, _ = split_strategy_key(case.strategy_key)
        try:
            source = (Path(self._paths.repo_root) / path).read_bytes()
        except FileNotFoundError:
            return "STRATEGY_SOURCE_CHANGED: the strategy file is gone"
        if "sha256:" + hashlib.sha256(source).hexdigest() != case.strategy_file_hash:
            return "STRATEGY_SOURCE_CHANGED: the strategy file differs from the judged one"
        return None

    @staticmethod
    def _family_problem(case: Any, family: Any) -> Optional[str]:
        """Sizing and the judged cost model come from the family; without it the row cannot be the judged run."""
        if family is not None:
            return None
        if case.family_id is None:
            return "FAMILY_UNKNOWN: the case names no family"
        return f"FAMILY_UNKNOWN: family {case.family_id} is not in the research registry"

    def _costs_problem(self, family: Any) -> Optional[str]:
        """The replay must use the cost model the judgment saw (the family's digest of execution_costs.yaml)."""
        try:
            current = _costs_config_digest(Path(self._paths.execution_costs))
        except (EvaluationError, OSError) as exc:
            return f"COSTS_CHANGED: the cost config does not load ({type(exc).__name__})"
        if current != family.cost_model.get("config_digest"):
            return "COSTS_CHANGED: execution_costs.yaml differs from the judged one"
        return None

    def _bar_problem(self, conids, bar_size: str, session: dt.date, *,
                     inputs_from: Optional[dt.date] = None) -> Optional[str]:
        """Every session the replay reads, ``inputs_from`` (the warm-up start) through ``session``: one continuous
        run carries a hole in an earlier session into every later row. ``session`` itself is checked first."""
        tickdata = TickStorage(self._paths.history_db).get_tickdata(BarSize.parse_str(bar_size))
        spacing = pd.Timedelta(seconds=bar_seconds(bar_size))
        earlier = xnys_sessions(inputs_from, session)[:-1] if inputs_from is not None else []
        stamps_of = {conid: _utc_stamps(tickdata.read(conid, date_range=DateRange(
            start=ny_day_start(earlier[0] if earlier else session), end=before_close(session)))) for conid in conids}
        for day in [session, *earlier]:
            for conid in conids:
                problem = _session_bar_problem(stamps_of[conid], conid, bar_size, spacing, day)
                if problem is not None:
                    return problem
        return None

    def _family(self, case: Any) -> Any:
        return None if case.family_id is None else self._registry.get_family(case.family_id)

    def _environment(self, case: Any, family: Any) -> RunEnvironment:
        cost = family.cost_model
        path, class_name = split_strategy_key(case.strategy_key)
        return RunEnvironment(
            history_db=self._paths.history_db, universe_db=self._paths.universe_db,
            universe_library=self._paths.universe_library, execution_costs_path=self._paths.execution_costs,
            strategy_file=str(Path(self._paths.repo_root) / path), class_name=class_name,
            conids=tuple(case.conids), bar_size=case.bar_size, order_notional=float(cost["order_notional"]),
            account_equity=float(cost["account_equity"]),
            max_gross_allocation=float(cost["max_gross_allocation"]))
