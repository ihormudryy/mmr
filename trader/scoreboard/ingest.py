"""Idempotent ingestion of AI costs and simulated baseline decisions (spec 6.3, 6.8; SP2 Plan 2)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

from trader.scoreboard.ingest_models import (MATCHED_ENTRY, TRADER_SIZED, RecordAiCostRequest,
                                             RecordSimulatedDecisionRequest, canonical_json, parse_utc)
from trader.scoreboard.ports import SizingUnavailable, StoreTripFacts, session_date_et
from trader.scoreboard.store import IngestRefused, ScoreboardConflict, ScoreboardStore

logger = logging.getLogger(__name__)

BASELINES = {
    "follow_signal.v1": "strategy_signal",
    "fixed_rule.v1": "self_found",
    "no_trade.v1": "self_found",
    "matched_entry_bracket_exit.v1": "model_close",
}
STATUS_RANK = {"unknown": 0, "estimated": 1, "confirmed": 2}
NO_TRADE = "no_trade.v1"
BAR_SOURCE_NONE = "none"
SIZING_UNAVAILABLE = "sizing_unavailable"
SIZING_MAX_LAG = dt.timedelta(seconds=120)


def _reply(status: str, record_id: str, code: Optional[str] = None, detail: Optional[str] = None,
           retryable: bool = False) -> dict:
    return {"status": status, "record_id": record_id, "code": code, "detail": detail, "retryable": retryable}


class AiIngest:
    def __init__(self, *, store: ScoreboardStore, experiments: Any, decisions: Any, calendar: Any,
                 now: Callable[[], dt.datetime], sizer: Any = None, trips: Any = None):
        self._store = store
        self._experiments = experiments
        self._decisions = decisions
        self._calendar = calendar
        self._now = now
        self._sizer = sizer          # BaselineSizer; None (no ai_paper stack) sizes nothing: sizing_unavailable
        self._trips = trips or StoreTripFacts(store)   # round_trips of this journal (Ruling 21)

    # -- the two commands ----------------------------------------------------

    def record_cost(self, req: RecordAiCostRequest) -> dict:
        return self._guarded(req.record_id, lambda: self._cost(req))

    def record_simulated(self, req: RecordSimulatedDecisionRequest) -> dict:
        return self._guarded(req.record_id, lambda: self._simulated(req))

    @staticmethod
    def _guarded(record_id: str, work: Callable[[], str]) -> dict:
        try:
            return _reply(work(), record_id)
        except IngestRefused as refusal:
            return _reply("REFUSED", record_id, refusal.code, refusal.detail, refusal.retryable)
        except ScoreboardConflict as conflict:
            return _reply("REFUSED", record_id, "CONFLICTING_DUPLICATE", str(conflict))

    # -- links -----------------------------------------------------------------

    def _experiment(self, experiment_id: str) -> Any:
        experiment = self._experiments.get(experiment_id)
        if experiment is None:
            raise IngestRefused("EXPERIMENT_UNKNOWN", f"no experiment {experiment_id}")
        return experiment

    @staticmethod
    def _inside(experiment: Any, moment: dt.datetime, code: str, *, upper_bound: bool = True) -> None:
        stopped_at = getattr(experiment, "stopped_at", None)
        too_late = upper_bound and stopped_at is not None and moment > stopped_at
        if moment < experiment.started_at or too_late:
            raise IngestRefused(code, f"{moment.isoformat()} is outside experiment {experiment.experiment_id}")

    def _decision_link(self, decision_id: str, experiment: Any, *, conid: Optional[int], must_enter: bool) -> Any:
        fact = self._decisions.get(decision_id)
        if fact is None:
            raise IngestRefused("DECISION_LINK_UNKNOWN", f"the trader has no decision {decision_id}", retryable=True)
        if fact.account_id != experiment.account_id:
            raise IngestRefused("DECISION_LINK_WRONG_ACCOUNT", f"decision {decision_id} is of another account")
        if fact.experiment_id != experiment.experiment_id:
            raise IngestRefused("DECISION_LINK_OTHER_EXPERIMENT",
                                f"decision {decision_id} belongs to experiment {fact.experiment_id}")
        if fact.received_at < experiment.started_at:
            raise IngestRefused("DECISION_LINK_OUTSIDE_EXPERIMENT", f"decision {decision_id} predates the experiment")
        if must_enter and fact.action != "ENTER":
            raise IngestRefused("DECISION_LINK_NOT_ENTER", f"decision {decision_id} is a {fact.action}")
        if conid is not None and fact.conid != conid:
            raise IngestRefused("DECISION_LINK_CONID_MISMATCH", f"decision {decision_id} is on conid {fact.conid}")
        return fact

    # -- costs -------------------------------------------------------------------

    def _cost(self, req: RecordAiCostRequest) -> str:
        experiment = self._experiment(req.experiment_id)
        called_at = parse_utc(req.called_at)
        self._inside(experiment, called_at, "CALL_OUTSIDE_EXPERIMENT", upper_bound=False)
        if req.decision_id is not None:
            self._decision_link(req.decision_id, experiment, conid=None, must_enter=False)
        row = {**req.model_dump(), "called_at": called_at, "correction_seq": 0, "body_digest": req.digest(),
               "recorded_at": self._now()}
        return self._store.ingest_sealed_many([("ai_costs", row)], extend=lambda conn: self._cost_checks(conn, req))

    @staticmethod
    def _cost_checks(conn, req: RecordAiCostRequest) -> dict:
        if req.corrects_record_id is None:
            clash = conn.execute(
                "SELECT record_id FROM ai_costs WHERE experiment_id = ? AND attempt_id = ? "
                "AND corrects_record_id IS NULL", [req.experiment_id, req.attempt_id]).fetchone()
            if clash is not None:
                raise ScoreboardConflict(f"attempt {req.attempt_id} already has the original cost {clash[0]}")
            return {"correction_seq": 0}
        target = req.corrects_record_id
        original = conn.execute(
            "SELECT experiment_id, role, provider, model, attempt_id, called_at, corrects_record_id "
            "FROM ai_costs WHERE record_id = ?", [target]).fetchone()
        if original is None:
            raise IngestRefused("CORRECTION_TARGET_UNKNOWN", f"no cost record {target}", retryable=True)
        if original[6] is not None:
            raise IngestRefused("CORRECTION_OF_CORRECTION", f"{target} is itself a correction")
        same = (req.experiment_id, req.role, req.provider, req.model, req.attempt_id, parse_utc(req.called_at))
        if tuple(original[:6]) != same:
            raise IngestRefused("CORRECTION_IDENTITY_MISMATCH", f"{target} is a different call")
        status, seq = conn.execute(
            "SELECT cost_status, correction_seq FROM ai_costs WHERE record_id = ? OR corrects_record_id = ? "
            "ORDER BY correction_seq DESC LIMIT 1", [target, target]).fetchone()
        if STATUS_RANK[req.cost_status] < STATUS_RANK[status]:
            raise IngestRefused("CORRECTION_DOWNGRADE", f"{target} is {status}; {req.cost_status} is weaker")
        return {"correction_seq": int(seq) + 1}

    # -- simulated decisions -------------------------------------------------------

    def _simulated(self, req: RecordSimulatedDecisionRequest) -> str:
        experiment = self._experiment(req.experiment_id)
        cohort = BASELINES.get(req.baseline_id)
        if cohort is None:
            raise IngestRefused("UNKNOWN_BASELINE", f"{req.baseline_id} is not a known baseline id")
        if req.cohort != cohort:
            raise IngestRefused("COHORT_NOT_ALLOWED", f"{req.baseline_id} belongs to cohort {cohort}")
        decided_at = parse_utc(req.decided_at)
        self._inside(experiment, decided_at, "DECIDED_OUTSIDE_EXPERIMENT")
        if req.baseline_id != NO_TRADE:
            schedule = self._calendar.resolve(decided_at)
            if schedule is None or not schedule.open_utc <= decided_at < schedule.flatten_start_utc:
                raise IngestRefused("DECIDED_OUTSIDE_ENTRY_WINDOW", f"{decided_at.isoformat()} is not before the flatten start")
        linked = None
        if req.linked_decision_id is not None:
            linked = self._decision_link(req.linked_decision_id, experiment, conid=req.conid, must_enter=True)
        now = self._now()
        session_date = session_date_et(decided_at)
        decision = {**req.model_dump(), "decided_at": decided_at, "session_date": session_date,
                    "quantity_source": None if req.quantity is None else "client", "sizing_json": None,
                    "body_digest": req.digest(), "recorded_at": now}
        if req.baseline_id == MATCHED_ENTRY and req.incomplete_reason is None:
            decision["linked_round_trip_id"] = self._verified_trip(req, experiment, linked)
        incomplete_reason = req.incomplete_reason
        if incomplete_reason is None and req.baseline_id in TRADER_SIZED and not self._known(req.record_id):
            if linked is not None and linked.entry_quantity is not None:
                # Jev took it: the real ENTER's own size (re-sizing would count that entry against itself).
                decision.update({"quantity": linked.entry_quantity, "quantity_source": "linked_entry",
                                 "sizing_json": canonical_json({"code": "LINKED_ENTER",
                                                                "decision_id": linked.decision_id})})
            else:
                # I/O outside the write transaction (the database lock is not reentrant); a redelivery skips it.
                sized, incomplete_reason = self._size(req, experiment, decided_at, now)
                decision.update(sized)
        items = [("simulated_decisions", decision)]
        if req.baseline_id == NO_TRADE:
            items.append(self._outcome(req, session_date, now, status="COMPLETE", reason=None, pnl_usd=0.0, trades=0))
        elif incomplete_reason is not None:
            items.append(self._outcome(req, session_date, now, status="INCOMPLETE", reason=incomplete_reason,
                                       pnl_usd=None, trades=None))
        return self._store.ingest_sealed_many(items, extend=lambda conn: self._opportunity_check(conn, req))

    def _known(self, record_id: str) -> bool:
        return bool(self._store.fetch("simulated_decisions", {"record_id": record_id}))

    def _size(self, req, experiment, decided_at, now) -> tuple[dict, Optional[str]]:
        """Ruling 19: the size a real ENTER of this deployment gets now, or sizing_unavailable with the code."""
        try:
            if now - decided_at > SIZING_MAX_LAG:
                raise SizingUnavailable("SIZING_TOO_LATE", {"lag_seconds": (now - decided_at).total_seconds()})
            if self._sizer is None:
                raise SizingUnavailable("NO_SIZER", {})
            sized = self._sizer.size(account_id=experiment.account_id, deployment_digest=req.deployment_digest,
                                     conid=req.conid, reference_price=req.reference_price,
                                     stop_price=req.stop_price)
        except SizingUnavailable as failure:
            logger.warning("baseline %s not sized: %s", req.record_id, failure.code)
            return {"sizing_json": canonical_json({"code": failure.code, **failure.inputs})}, failure.reason
        except Exception as exc:                  # provider text may carry details: keep the class name only
            logger.error("baseline %s sizer failed: %s", req.record_id, type(exc).__name__)
            return {"sizing_json": canonical_json({"code": f"SIZER_{type(exc).__name__}"})}, SIZING_UNAVAILABLE
        return {"quantity": sized.quantity, "quantity_source": "trader_sizing",
                "sizing_json": canonical_json(dict(sized.inputs))}, None

    @staticmethod
    def _outcome(req, session_date, now, *, status, reason, pnl_usd, trades) -> tuple[str, dict]:
        return ("simulated_outcomes", {
            "record_id": req.record_id, "experiment_id": req.experiment_id, "baseline_id": req.baseline_id,
            "cohort": req.cohort, "session_date": session_date, "status": status, "reason": reason,
            "exit_kind": "NONE", "exit_at": None, "exit_price": None, "pnl_usd": pnl_usd, "trades": trades,
            "bar_source": BAR_SOURCE_NONE, "bars_digest": None, "computed_at": now})

    def _verified_trip(self, req: RecordSimulatedDecisionRequest, experiment: Any, entry: Any) -> str:
        """Ruling 21: the trip comes from the trader's own facts (the ENTER and the close), never the caller."""
        close = self._decisions.get(req.opportunity_id)
        if close is None:
            raise IngestRefused("DECISION_LINK_UNKNOWN", f"the trader has no close {req.opportunity_id}", retryable=True)
        for fact in (entry, close):
            if fact.account_id != experiment.account_id:
                raise IngestRefused("DECISION_LINK_WRONG_ACCOUNT", f"decision {fact.decision_id} is of another account")
        trip = self._trips.opened_by(experiment.experiment_id, entry.decision_id)
        if trip is None:
            raise IngestRefused("MATCHED_ENTRY_TRIP_UNKNOWN", f"no round trip opened by {entry.decision_id} yet",
                                retryable=True)
        if (close.action not in ("CLOSE", "PARTIAL_CLOSE") or close.experiment_id != experiment.experiment_id
                or close.conid != entry.conid or close.received_at < trip.opened_at):
            raise IngestRefused("MATCHED_CLOSE_INVALID", f"{req.opportunity_id} is not a close of trip {trip.round_trip_id}")
        if req.linked_round_trip_id is not None and req.linked_round_trip_id != trip.round_trip_id:
            raise IngestRefused("MATCHED_ENTRY_TRIP_MISMATCH",
                                f"{req.linked_decision_id} opened {trip.round_trip_id}, not {req.linked_round_trip_id}")
        return trip.round_trip_id

    @staticmethod
    def _opportunity_check(conn, req: RecordSimulatedDecisionRequest) -> dict:
        clash = conn.execute(
            "SELECT record_id FROM simulated_decisions WHERE experiment_id = ? AND baseline_id = ? "
            "AND opportunity_id = ?", [req.experiment_id, req.baseline_id, req.opportunity_id]).fetchone()
        if clash is not None:
            raise ScoreboardConflict(f"opportunity {req.opportunity_id} is already recorded as {clash[0]}")
        return {}
