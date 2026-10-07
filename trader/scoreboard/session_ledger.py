"""``equity_daily``: one sealed row per session end, plus peaks, late commissions and recovery.

Rulings 2-6, 9 and 10 of Plan 5. A value that cannot be captured is stored as
NULL with an incident; equity, P&L and an end state are never invented.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable, Optional

from trader.automation.session_controller import SessionStateStore
from trader.scoreboard.ports import SESSION_END_STATES, SessionEnd, session_date_et
from trader.scoreboard.round_trips import FillFact, Projection, fills_digest, load_fill_facts, project_round_trips
from trader.scoreboard.store import ScoreboardConflict, ScoreboardStore

logger = logging.getLogger(__name__)

CONTROLLER_END_STATES = {"FLAT": "FLAT", "INCIDENT": "FAILED_SAFE"}
OBSERVED_STATES = frozenset({"ARMED", "PAUSED", "KILLED"})
_TOLERANCE = 1e-9


def _decimal_sum(values) -> Optional[Decimal]:
    values = list(values)
    if any(value is None for value in values):
        return None
    return sum(values, Decimal(0))


def _float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def _multiply(value: Optional[float], rate: Optional[float]) -> Optional[float]:
    return None if value is None or rate is None else value * rate


class SessionFacts:
    """What the fills say about one session: facts, stored even when the money is unknown."""

    def __init__(self, projection: Projection, facts: list[FillFact], session_date: dt.date):
        pieces = [p for p in projection.pieces if p.session_date == session_date]
        by_exec: dict[str, list] = defaultdict(list)
        for piece in pieces:
            by_exec[piece.exec_id].append(piece.fee)
        self.commissions = {exec_id: _decimal_sum(fees) for exec_id, fees in by_exec.items()}
        self.realized = sum((p.realized for p in pieces), Decimal(0))
        self.trade_count = sum(1 for t in projection.trips
                               if t.status == "CLOSED" and t.closed_session == session_date)
        self.fill_count = len(by_exec)
        self.fills_digest = fills_digest([f for f in facts if session_date_et(f.fill_time) == session_date])

    def commission_json(self) -> str:
        return json.dumps({k: _float(v) for k, v in sorted(self.commissions.items())}, sort_keys=True)

    def commissions_usd(self) -> Optional[float]:
        return _float(_decimal_sum(self.commissions.values()))


class SessionLedger:
    def __init__(self, store: ScoreboardStore, db: Any, experiments: Any, broker: Any, fx: Any, calendar: Any,
                 links: Any, now: Callable[[], dt.datetime],
                 on_row_written: Optional[Callable[[str, dt.date], Any]] = None):
        self.store = store
        self.db = db
        self.experiments = experiments
        self.broker = broker
        self.fx = fx
        self.calendar = calendar
        self.links = links
        self._now = now
        self.on_row_written = on_row_written

    # -- experiment window ---------------------------------------------------

    @staticmethod
    def covers(experiment: Any, account_id: str, session_date: dt.date) -> bool:
        if experiment is None or experiment.account_id != account_id:
            return False
        if session_date < session_date_et(experiment.started_at):
            return False
        stopped_at = getattr(experiment, "stopped_at", None)
        return stopped_at is None or session_date <= session_date_et(stopped_at)

    @staticmethod
    def _end_state(experiment: Any, session_date: dt.date, state: str) -> str:
        killed_at = getattr(experiment, "killed_at", None)
        if killed_at is not None and session_date_et(killed_at) == session_date:
            return "KILLED"
        return state

    # -- session ends --------------------------------------------------------

    def on_controller_terminal(self, state: Any) -> None:
        """``SessionController`` listener: FLAT -> FLAT, INCIDENT -> FAILED_SAFE (ruling 4)."""
        mapped = CONTROLLER_END_STATES.get(state.state)
        if mapped is None:
            return
        self.record_session_end(SessionEnd(state.account_id, state.session_date, mapped, self._now()))

    def record_session_end(self, end: Any) -> Optional[dict]:
        """The written (or existing) row; None when no experiment covers the date. Idempotent.

        ``end`` is Plan 5's ``SessionEnd`` or Plan 4's ``KillSessionEnd`` (same field names).
        """
        if end.state not in SESSION_END_STATES:
            raise ValueError(f"session end state must be one of {SESSION_END_STATES}, got {end.state!r}")
        experiment = self.experiments.latest()
        if not self.covers(experiment, end.account_id, end.session_date):
            return None
        existing = self._row(experiment.experiment_id, end.session_date)
        if existing is not None:
            return existing
        ended_at = end.ended_at or self._now()
        row = self._captured_row(experiment, end.session_date,
                                 self._end_state(experiment, end.session_date, end.state), ended_at)
        return self._insert(experiment.experiment_id, end.session_date, row, notify=True)

    def _insert(self, experiment_id: str, session_date: dt.date, row: dict, *, notify: bool) -> dict:
        try:
            self.store.insert_sealed("equity_daily", row)
        except ScoreboardConflict:
            return self._row(experiment_id, session_date)
        if notify and self.on_row_written is not None:
            try:
                self.on_row_written(experiment_id, session_date)
            except Exception:
                logger.exception("equity_daily %s %s written; the row-written hook failed",
                                 experiment_id, session_date)
        return self._row(experiment_id, session_date)

    def _row(self, experiment_id: str, session_date: dt.date) -> Optional[dict]:
        rows = self.store.fetch("equity_daily", {"experiment_id": experiment_id, "session_date": session_date})
        return rows[0] if rows else None

    # -- row building --------------------------------------------------------

    def _session_facts(self, experiment: Any, session_date: dt.date) -> SessionFacts:
        facts = load_fill_facts(self.db, experiment.account_id, experiment.started_at)
        projection = project_round_trips(facts, links_for=self.links.links_for_order_ref,
                                         account_id=experiment.account_id)
        return SessionFacts(projection, facts, session_date)

    def _incident_key(self, experiment: Any, session_date: dt.date) -> str:
        return f"{experiment.experiment_id}:{session_date.isoformat()}"

    def _capture(self, experiment: Any, session_date: dt.date) -> Any:
        try:
            return self.broker.capture(experiment.account_id)
        except Exception as exc:
            self.store.record_incident("SNAPSHOT_UNAVAILABLE", self._incident_key(experiment, session_date),
                                       f"no broker snapshot at session end: {exc}")
            return None

    def _usd_rate(self, experiment: Any, session_date: dt.date) -> tuple[Optional[float], Any]:
        key = self._incident_key(experiment, session_date)
        try:
            evidence = self.fx.evidence()
        except Exception as exc:
            self.store.record_incident("FX_EVIDENCE_MISSING", key, f"FX evidence unreadable: {exc}")
            return None, None
        if evidence.base_currency != experiment.base_currency:
            self.store.record_incident("FX_EVIDENCE_MISSING", key,
                                       f"account base currency {evidence.base_currency} differs from the "
                                       f"experiment's {experiment.base_currency}")
            return None, evidence
        if evidence.usd_per_base is None:
            self.store.record_incident("FX_EVIDENCE_MISSING", key,
                                       f"no USD rate for base {evidence.base_currency}; USD values not written")
        return evidence.usd_per_base, evidence

    def _start(self, experiment: Any, session_date: dt.date) -> dict:
        """Ruling 2: the previous known end value, else the experiment start, else unknown."""
        earlier = [r for r in self.store.fetch("equity_daily", {"experiment_id": experiment.experiment_id})
                   if r["session_date"] < session_date]
        start_date = session_date_et(experiment.started_at)
        if earlier:
            missing = self.calendar.sessions_between(earlier[-1]["session_date"], session_date)
        else:
            missing = len([d for d in self.calendar.sessions_in_range(start_date, session_date) if d < session_date])
        known = [r for r in earlier if r["end_nlv_base"] is not None]
        if known:
            return {"start_nlv_base": known[-1]["end_nlv_base"], "start_nlv_usd": known[-1]["end_nlv_usd"],
                    "start_source": "prev_end", "missing_sessions_before": missing}
        if experiment.start_net_liquidation is not None:
            return {"start_nlv_base": experiment.start_net_liquidation,
                    "start_nlv_usd": _multiply(experiment.start_net_liquidation, experiment.start_usd_per_base),
                    "start_source": "experiment_start", "missing_sessions_before": missing}
        return {"start_nlv_base": None, "start_nlv_usd": None, "start_source": "unknown",
                "missing_sessions_before": missing}

    def _peak(self, experiment_id: str, session_date: dt.date) -> Optional[float]:
        rows = self.store.fetch("equity_session_peak", {"experiment_id": experiment_id, "session_date": session_date})
        if not rows or rows[0]["incomplete"]:
            return None
        return rows[0]["peak_gross_usd"]

    def _base_row(self, experiment: Any, session_date: dt.date, state: str, ended_at: dt.datetime,
                  facts: SessionFacts) -> dict:
        return {
            "experiment_id": experiment.experiment_id, "session_date": session_date,
            "account_id": experiment.account_id, "session_end_state": state,
            "base_currency": experiment.base_currency, "commission_json": facts.commission_json(),
            "trade_count": facts.trade_count, "fill_count": facts.fill_count, "fills_digest": facts.fills_digest,
            "ended_at": ended_at, "written_at": self._now(),
        }

    def _captured_row(self, experiment: Any, session_date: dt.date, state: str, ended_at: dt.datetime) -> dict:
        facts = self._session_facts(experiment, session_date)
        snapshot = self._capture(experiment, session_date)
        rate, evidence = self._usd_rate(experiment, session_date)
        start = self._start(experiment, session_date)
        end_base = None if snapshot is None else float(snapshot.net_liquidation)
        return {
            **self._base_row(experiment, session_date, state, ended_at, facts), **start,
            "end_nlv_base": end_base, "end_nlv_usd": _multiply(end_base, rate),
            "fx_usd_per_base": rate,
            "fx_source": None if evidence is None else evidence.source,
            "fx_as_of": None if evidence is None or rate is None else evidence.as_of,
            "realized_pnl_usd": float(facts.realized), "commissions_usd": facts.commissions_usd(),
            "peak_gross_exposure_usd": self._peak(experiment.experiment_id, session_date),
            "open_positions": None if snapshot is None else sum(1 for p in snapshot.positions if p.quantity != 0),
        }

    def _uncaptured_row(self, experiment: Any, session_date: dt.date, state: str) -> dict:
        """Recovery of a past session: facts only, every money column NULL (ruling 6)."""
        facts = self._session_facts(experiment, session_date)
        start = self._start(experiment, session_date)
        return {
            **self._base_row(experiment, session_date, state, self._now(), facts),
            "start_source": "unknown", "missing_sessions_before": start["missing_sessions_before"],
        }

    # -- peaks ---------------------------------------------------------------

    def observe_snapshot(self, snapshot: Any) -> None:
        """Ruling 9: the highest gross exposure seen in the session; any unknown makes it unknown."""
        experiment = self.experiments.latest()
        now = self._now()
        today = session_date_et(now)
        if (experiment is None or experiment.state not in OBSERVED_STATES
                or not self.covers(experiment, snapshot.account_id, today) or not self.calendar.is_session(today)):
            return
        gross, incomplete = 0.0, False
        for position in snapshot.positions:
            if position.quantity == 0:
                continue
            if position.currency != "USD":
                incomplete = True
                self.store.record_incident("NON_USD_POSITION", self._incident_key(experiment, today),
                                           f"position {position.conid} is in {position.currency}")
            elif position.market_value is None:
                incomplete = True
            else:
                gross += abs(float(position.market_value))
        key = [experiment.experiment_id, today]

        def tx(conn):
            found = conn.execute("SELECT peak_gross_usd, incomplete FROM equity_session_peak "
                                 "WHERE experiment_id = ? AND session_date = ?", key).fetchone()
            sticky = incomplete or (found is not None and bool(found[1]))
            peak = None if sticky else max(gross, found[0] if found is not None else gross)
            conn.execute("DELETE FROM equity_session_peak WHERE experiment_id = ? AND session_date = ?", key)
            conn.execute("INSERT INTO equity_session_peak VALUES (?, ?, ?, ?, ?)", key + [peak, sticky, now])
        self.db.transaction(tx)

    # -- late commissions ----------------------------------------------------

    def reconcile_commissions(self) -> int:
        """Ruling 10: one adjustment per changed commission; a fill new to a written row is an incident."""
        experiment = self.experiments.latest()
        if experiment is None:
            return 0
        rows = self.store.fetch("equity_daily", {"experiment_id": experiment.experiment_id})
        if not rows:
            return 0
        facts = load_fill_facts(self.db, experiment.account_id, experiment.started_at)
        projection = project_round_trips(facts, links_for=lambda ref: None, account_id=experiment.account_id)
        adjustments = self.store.fetch("equity_adjustments", {"experiment_id": experiment.experiment_id})
        written = 0
        for row in rows:
            session_date = row["session_date"]
            current = SessionFacts(projection, facts, session_date).commissions
            stored = json.loads(row["commission_json"])
            for exec_id, fee in sorted(current.items()):
                if exec_id not in stored:
                    self.store.record_incident(
                        "LATE_FILL_AFTER_SESSION_ROW", f"{experiment.experiment_id}:{session_date}:{exec_id}",
                        f"fill {exec_id} reached broker_fills after the {session_date} row was written")
                    continue
                if fee is None:
                    continue
                earlier = [a for a in adjustments if a["session_date"] == session_date and a["exec_id"] == exec_id]
                booked = (stored[exec_id] or 0.0) + sum(a["amount_usd"] for a in earlier)
                delta = float(fee) - booked
                if abs(delta) <= _TOLERANCE:
                    continue
                seed = f"{experiment.experiment_id}|{session_date}|{exec_id}|{len(earlier)}"
                adjustment = {"adjustment_id": hashlib.sha256(seed.encode()).hexdigest()[:32],
                              "experiment_id": experiment.experiment_id, "session_date": session_date,
                              "kind": "COMMISSION", "exec_id": exec_id, "amount_usd": delta,
                              "recorded_at": self._now()}
                try:
                    self.store.insert_sealed("equity_adjustments", adjustment)
                except ScoreboardConflict:
                    continue
                adjustments.append(adjustment)
                written += 1
        return written

    # -- startup recovery ----------------------------------------------------

    def recover(self, now: dt.datetime) -> list[str]:
        """Rows for every past session of the experiment that has none (ruling 6)."""
        experiment = self.experiments.latest()
        if experiment is None:
            return []
        today = session_date_et(now)
        last = today
        stopped_at = getattr(experiment, "stopped_at", None)
        if stopped_at is not None:
            last = min(last, session_date_et(stopped_at))
        have = {r["session_date"] for r in self.store.fetch("equity_daily", {"experiment_id": experiment.experiment_id})}
        controller = SessionStateStore(self.db)
        written = []
        for session_date in self.calendar.sessions_in_range(session_date_et(experiment.started_at), last):
            if session_date in have:
                continue
            state = controller.load(experiment.account_id, session_date)
            mapped = None if state is None else CONTROLLER_END_STATES.get(state.state)
            if session_date == today:
                if mapped is not None and self.record_session_end(
                        SessionEnd(experiment.account_id, session_date, mapped, now)) is not None:
                    written.append(session_date.isoformat())
                continue
            key = self._incident_key(experiment, session_date)
            end_state = self._end_state(experiment, session_date, mapped or "UNKNOWN")
            if end_state == "UNKNOWN":
                self.store.record_incident("SESSION_MISSING", key,
                                           "no equity_daily row and no terminal session state; end state unknown")
            else:
                self.store.record_incident("SESSION_ROW_RECOVERED_WITHOUT_NLV", key,
                                           f"session ended {end_state} but its row was not written; "
                                           "net liquidation, P&L and commissions are unknown")
            self._insert(experiment.experiment_id, session_date,
                         self._uncaptured_row(experiment, session_date, end_state), notify=False)
            written.append(session_date.isoformat())
        return written
