"""The RENEWAL evaluation case (SP2c spec 6.2 "Renewal after expiry"): code-built from the trader's forward
evidence, signed like any case. No holdout is opened and no parameter trial is written.

Nothing from a model reaches the case or its summary. A session outside the judgment's replayed window is
NOT_REPLAYED and counts as incomplete; there is no forward-performance threshold beyond complete data."""
from __future__ import annotations

import datetime as dt
from collections import Counter

from trader.research.canonical import sha256_digest
from trader.research.evaluation_case import CASE_DOMAIN, EvaluationCase
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.research.shadow_window import session_close_utc

RENEWAL_REQUEST_DOMAIN = "mmr.research.renewal-request.v1"
ROW_GRACE_HOURS = 1


def renewal_request_id(prior_version_digest: str) -> str:
    """One renewal request per version; the caller cannot choose the id."""
    return "sha256:" + sha256_digest(RENEWAL_REQUEST_DOMAIN,
                                     {"kind": "RENEWAL", "prior_version_digest": prior_version_digest})


def pending_sessions(view: ForwardEvidenceView, *, now: dt.datetime, incomplete_after_hours: float) -> list[str]:
    """A MISSING row is still on its way until the replay's INCOMPLETE deadline plus one hour."""
    grace = dt.timedelta(hours=incomplete_after_hours + ROW_GRACE_HOURS)
    return [session.session_date for session in view.sessions
            if session.state == "MISSING"
            and now < session_close_utc(dt.date.fromisoformat(session.session_date)) + grace]


def _complete_session_numbers(view: ForwardEvidenceView) -> list:
    complete = [session for session in view.sessions if session.state == "COMPLETE"]
    for session in complete:
        if None in (session.pnl_usd, session.fees_usd, session.trades, session.end_equity_usd):
            raise ValueError(f"a COMPLETE session {session.session_date} must carry its numbers")
    return complete


def _window_numbers(complete: list) -> dict:
    """The sums of a window whose every session is COMPLETE."""
    if not complete:
        return {"pnl_usd": None, "fees_usd": None, "trades": None, "worst_session_pnl_usd": None,
                "end_equity_usd": None}
    return {"pnl_usd": float(sum(session.pnl_usd for session in complete)),
            "fees_usd": float(sum(session.fees_usd for session in complete)),
            "trades": sum(session.trades for session in complete),
            "worst_session_pnl_usd": min(session.pnl_usd for session in complete),
            "end_equity_usd": complete[-1].end_equity_usd}


def forward_summary(view: ForwardEvidenceView) -> dict:
    """What Jev reads about the forward window: code-computed sums. The window sums are None unless every
    session is complete; ``known_pnl_usd`` sums only the complete ones."""
    complete = _complete_session_numbers(view)
    other = [session for session in view.sessions if session.state != "COMPLETE"]
    closed = [trip for trip in view.trips if trip.status == "CLOSED"]
    priced = [trip.net_pnl_usd for trip in closed if trip.net_pnl_usd is not None]
    return {"first_session": view.first_session, "expiry_session": view.expiry_session,
            "sessions": len(view.sessions), "complete": len(complete), "incomplete": len(other),
            "incomplete_reasons": dict(Counter(session.reason or session.state for session in other)),
            **_window_numbers([] if other else complete),
            "known_pnl_usd": float(sum(session.pnl_usd for session in complete)) if complete else None,
            "paper_trips": len(view.trips), "paper_trips_closed": len(closed),
            "paper_trips_unpriced": len(closed) - len(priced),
            "paper_net_pnl_usd": float(sum(priced)) if priced else None,
            "paper_fees_complete": all(trip.fees_complete for trip in view.trips)}


def build_renewal_case(view: ForwardEvidenceView, *, created_at: dt.datetime, warmup_sessions: int) -> EvaluationCase:
    """The deployed point only, with the line's artifact facts. FORWARD_COMPLETE only when every session is
    COMPLETE. The evidence keeps the replay keys so the renewal judgment joins the shadow cohort."""
    binding, line = view.binding, view.line
    summary = forward_summary(view)
    params = dict(binding.params)
    return EvaluationCase(
        schema_version=CASE_DOMAIN, kind="RENEWAL", request_id=None, claim_day=None,
        strategy_key=binding.strategy_key, strategy_file_hash=binding.strategy_file_hash, cohort=[params],
        conids=list(binding.conids), bar_size=binding.bar_size,
        stage="FORWARD_COMPLETE" if view.sessions and summary["incomplete"] == 0 else "FORWARD_INCOMPLETE",
        holdout_passed=None, selected_params=params, family_id=line.family_id,
        selected_trial_id=line.selected_trial_id, artifact_id=line.artifact_id,
        eligibility_decision_digest=line.eligibility_decision_digest, decision_state=None, ruleset_digest=None,
        final_rule_results=[],
        renewal={"prior_deployment_version": view.version_digest, "forward_sessions": len(view.sessions),
                 "incomplete_sessions": summary["incomplete"]},
        created_at=created_at.isoformat(),
        evidence={"points": [], "strategy_trials": None, "selected_index": None, "replay_index": 0,
                  "holdout": None, "previously_revealed": [], "holdouts_opened_before": None,
                  "warmup_sessions": warmup_sessions, "error": None, "forward": summary,
                  "forward_sessions": [session.model_dump(mode="json") for session in view.sessions],
                  "paper_trips": [trip.model_dump(mode="json") for trip in view.trips],
                  "order_notional": binding.order_notional, "bundle_digest": binding.bundle_digest,
                  "initial_judgment_id": line.initial_judgment_id})
