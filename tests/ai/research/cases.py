"""Replies in the shapes the built research servers send, for the controller's tests.

tests/ai/research/test_research_wire_real_servers.py proves each of these has the key set of the real builder.
"""
CASE = "sha256:" + "c" * 64
REQUEST = "sha256:" + "a" * 64
BUNDLE, BASE = "sha256:" + "b" * 64, "sha256:" + "e" * 64
V1 = "sha256:" + "1" * 64
FILE = "sha256:" + "f" * 64
KEY = "strategies/time_of_day.py:TimeOfDay"
FAILED_ERROR = "EvaluationError: no bars"


def summary(rules_passed=True, stage="COMPLETE", failed_rules=()):
    """case_builder.evaluation_summary(case) for a one-point cohort: code-computed facts only.

    A FAILED case has no points, no metrics and no selected point, as the real builder writes it.
    """
    failed = stage == "FAILED"
    selected = stage in ("COMPLETE", "HOLDOUT_FAILED")
    point_metrics = {"expectancy_bps_1x": 12.5, "expectancy_bps_1_5x": 9.0, "expectancy_bps_2x": 5.5,
                     "selection_statistic": 0.9}
    points = [] if failed else [{
        "index": 0, "params": {"ENTRY_MINUTE": 615}, "pre_holdout_passed": not failed_rules,
        "failed_rules": list(failed_rules), "missing_rules": [], "metrics": point_metrics}]
    return {"kind": "INITIAL", "strategy_key": KEY, "strategy_path": "strategies/time_of_day.py",
            "class_name": "TimeOfDay", "file_hash": FILE, "params": {"ENTRY_MINUTE": 615} if selected else None,
            "conids": list(range(1001, 1009)), "bar_size": "15 mins", "stage": stage, "rules_passed": rules_passed,
            "holdout_passed": (stage == "COMPLETE") if selected else None,
            "eligibility": "PAPER_ELIGIBLE" if rules_passed else None, "renewal_checks_passed": None,
            "prior_version_digest": None, "order_notional": 1900.0, "strategy_trials": 0 if failed else 9,
            "prior_holdouts": 0, "previously_revealed_sessions": 0, "selected_index": 0 if selected else None,
            "error": FAILED_ERROR if failed else None, "metrics": {} if failed else point_metrics,
            "points": points,
            "rule_results": [] if failed else [
                {"point": 0, "rule": "expectancy_2x_positive", "passed": not failed_rules}],
            "forward": None}


def submitted(status="ACCEPTED", request_id=REQUEST, state="QUEUED"):
    """EvaluationService._submit_reply for an accepted or repeated request."""
    return {"status": status, "request_id": request_id, "state": state, "code": None, "detail": None,
            "retryable": False}


def refused(code, request_id=REQUEST, retryable=False):
    """A submit_evaluation refusal (EvaluationService._submit_reply)."""
    return {"status": "REFUSED", "request_id": request_id, "state": None, "code": code, "detail": code.lower(),
            "retryable": retryable}


def view(state="QUEUED", request_id=REQUEST, case=None, summary_=None):
    """EvaluationService.get for a known request."""
    return {"found": True, "request_id": request_id, "state": state, "case_digest": case, "summary": summary_}


def unknown(request_id=REQUEST):
    return {"found": False, "request_id": request_id, "state": None, "case_digest": None, "summary": None}


def done(request_id=REQUEST, **fields):
    return view("DONE", request_id=request_id, case=CASE, summary_=summary(**fields))


def binding():
    return {"strategy_path": "strategies/time_of_day.py", "class_name": "TimeOfDay", "file_hash": FILE,
            "params": {"ENTRY_MINUTE": 615}, "conids": list(range(1001, 1009)), "bar_size": "15 mins",
            "order_notional": 1900.0}


def attested(status="ATTESTED"):
    """JudgmentAttest._reply: a repeat is DUPLICATE with the same digest and binding."""
    return {"status": status, "bundle_digest": BUNDLE, "code": None, "detail": None, "retryable": False,
            "binding": binding()}


def attest_refused(code, retryable=False):
    return {"status": "REFUSED", "bundle_digest": None, "code": code, "detail": code.lower(),
            "retryable": retryable, "binding": None}


def recorded(body, status="RECORDED"):
    """BacktestJudgments._receipt."""
    return {"status": status, "judgment_id": body["judgment_id"], "code": None, "detail": None,
            "retryable": False, "verdict": body["verdict"],
            "cooldown_until_session": "2026-10-22" if body["verdict"] == "REJECT" else None}


def judgment_refused(judgment_id, code, retryable=False):
    """JudgmentRefused.reply: verdict and cooldown are present and null."""
    return {"status": "REFUSED", "judgment_id": judgment_id, "code": code, "detail": code.lower(),
            "retryable": retryable, "verdict": None, "cooldown_until_session": None}


def resolved(version=V1):
    """The ledger receipt of register_ai_deployment: asdict(CommandReceipt) around the registrar's outcome."""
    return {"command_id": "c", "correlation_id": "c", "state": "RESOLVED", "error_code": None, "retryable": False,
            "outcome": {"digest": BASE, "version_digest": version, "kind": "INITIAL", "first_session": "2026-10-09",
                        "expiry_session": "2026-11-05", "created": True,
                        "strategy_digest_provenance": "CLAIMED_NOT_VERIFIED"}}


def version(state="ACTIVE"):
    """AiPaperActions.version_view for a known version."""
    return {"found": True, "version": {
        "version_digest": V1, "base_digest": BASE, "judgment_id": "jdg-00000001", "kind": "INITIAL",
        "prior_version_digest": None, "first_session": "2026-10-09", "expiry_session": "2026-11-05",
        "state": state}}


RENEWAL_CASE, RENEWAL_REQUEST, V2 = "sha256:" + "9" * 64, "sha256:" + "8" * 64, "sha256:" + "2" * 64


def renewal_forward(complete=True):
    """renewal_case.forward_summary for three sessions, the last one INCOMPLETE unless ``complete``."""
    return {"first_session": "2026-09-10", "expiry_session": "2026-10-07", "sessions": 3,
            "complete": 3 if complete else 2, "incomplete": 0 if complete else 1,
            "incomplete_reasons": {} if complete else {"BARS_MISSING": 1},
            "pnl_usd": 15.0 if complete else None, "known_pnl_usd": 15.0 if complete else 10.0,
            "fees_usd": 3.0 if complete else None, "trades": 6 if complete else None,
            "worst_session_pnl_usd": 5.0 if complete else None, "end_equity_usd": 100_015.0 if complete else None,
            "paper_trips": 1, "paper_trips_closed": 1, "paper_trips_unpriced": 0, "paper_net_pnl_usd": 7.5,
            "paper_fees_complete": True}


def renewal_summary(complete=True, prior=V1):
    """Plan 3's evaluation_summary of a RENEWAL case (Plan 5 Task 5)."""
    return {**summary(), "kind": "RENEWAL", "stage": "FORWARD_COMPLETE" if complete else "FORWARD_INCOMPLETE",
            "rules_passed": complete, "holdout_passed": None, "eligibility": None, "renewal_checks_passed": complete,
            "prior_version_digest": prior, "strategy_trials": 0, "prior_holdouts": 0,
            "previously_revealed_sessions": 0, "selected_index": None, "error": None, "metrics": {}, "points": [],
            "rule_results": [], "forward": renewal_forward(complete)}


def renewal_done(**fields):
    return view("DONE", request_id=RENEWAL_REQUEST, case=RENEWAL_CASE, summary_=renewal_summary(**fields))


def version_reply(state, version=V1):
    return {"found": True, "version": {"version_digest": version, "base_digest": BASE, "judgment_id": "jdg-old",
                                       "kind": "INITIAL", "prior_version_digest": None,
                                       "first_session": "2026-09-10", "expiry_session": "2026-10-07",
                                       "state": state}}


def renewed():
    outcome = {**resolved(V2)["outcome"], "kind": "RENEWAL", "first_session": "2026-10-12",
               "expiry_session": "2026-11-06"}
    return {**resolved(V2), "outcome": outcome}
