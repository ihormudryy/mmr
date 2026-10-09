"""Plan 3 fills Plan 1's EvaluationCase (SP2c spec 5.1): the code-built record of one evaluation.

The header is what the trader reads; ``evidence`` carries the detail (every point's rule results, cost stress,
deflated statistic, trial counts, previously revealed sessions, warm-up). A case never authorizes trading.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping, Optional

from trader.research.evaluation_case import (
    CASE_DOMAIN, FULL_MENU, EvaluationCase, offered_menu, renewal_forward_complete,
)
from trader.research.strategy_key import split_strategy_key

STAGE_NAMES = {"pre_holdout": "PRE_HOLDOUT_FAILED", "holdout_failed": "HOLDOUT_FAILED", "complete": "COMPLETE"}


def json_safe(value: Any) -> Any:
    """Numpy scalars to Python, non-finite floats to None: the canonical bytes refuse NaN."""
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)


def _rule_outcomes(decision: Any) -> list[dict]:
    return [{"code": r.code, "passed": bool(r.passed)} for r in decision.results]


def _point(verdict: Any, decision: Any) -> dict:
    evidence = verdict.evidence
    return {"index": verdict.index, "params": json_safe(verdict.point.params), "trial_id": verdict.point.trial_id,
            "neighbour_trial_ids": [n.trial_id for n in verdict.neighbours],
            "pre_holdout_passed": bool(verdict.gate.passed), "failed_rules": list(verdict.gate.failed),
            "missing_rules": list(verdict.gate.missing),
            "rules": [{**rule, "observed": json_safe(r.observed)}
                      for rule, r in zip(_rule_outcomes(decision), decision.results, strict=True)],
            "expectancy_bps": {"1x": json_safe(evidence.expectancy_bps_baseline),
                               "1.5x": json_safe(evidence.expectancy_bps_1_5x),
                               "2x": json_safe(evidence.expectancy_bps_2x)},
            "selection_statistic": json_safe(evidence.selection_adjusted_confidence)}


def holdout_evidence(outcome: Any) -> dict | None:
    """The evidence and the case header both come from this one holdout result."""
    if outcome is None:
        return None
    return {**json_safe(dict(outcome.window)), "passed": bool(outcome.passed)}


def _decision_of(result: Any, verdict: Any) -> Any:
    """The selected point shows the final decision once its holdout opened; every other point its own."""
    selected = result.selected
    opened = result.holdout is not None and selected is not None
    return result.holdout.decision if opened and verdict.index == selected.index else verdict.decision


def initial_evidence(result: Any, *, warmup_sessions: int) -> dict:
    selected = result.selected
    return {"points": [_point(v, _decision_of(result, v)) for v in result.verdicts],
            "strategy_trials": result.strategy_trials,
            "selected_index": None if selected is None else selected.index, "replay_index": result.replay_index,
            "holdout": holdout_evidence(result.holdout),
            "previously_revealed": list(result.previously_revealed),
            "holdouts_opened_before": result.holdouts_opened_before, "warmup_sessions": warmup_sessions,
            "error": None}


def build_initial_case(spec: Any, claim_day: str, result: Any, *, created_at: dt.datetime,
                       warmup_sessions: int) -> EvaluationCase:
    outcome, selected = result.holdout, result.selected
    decision = None if outcome is None else outcome.decision          # named even when a failed holdout records none
    return EvaluationCase(
        schema_version=CASE_DOMAIN, kind="INITIAL", request_id=spec.request_id, claim_day=claim_day,
        strategy_key=spec.strategy_key, strategy_file_hash=spec.file_hash,
        cohort=[dict(p) for p in spec.cohort], conids=list(spec.body.conids), bar_size=spec.body.bar_size,
        stage=STAGE_NAMES[result.stage], holdout_passed=None if outcome is None else bool(outcome.passed),
        selected_params=None if selected is None else dict(selected.point.params),
        family_id=result.family_id, selected_trial_id=None if selected is None else selected.point.trial_id,
        artifact_id=None if outcome is None else outcome.artifact_id,
        eligibility_decision_digest=None if decision is None else decision.digest,
        decision_state=None if decision is None else decision.state,
        ruleset_digest=None if decision is None else decision.ruleset_digest,
        final_rule_results=[] if decision is None else _rule_outcomes(decision),
        renewal=None, created_at=created_at.isoformat(),
        evidence=initial_evidence(result, warmup_sessions=warmup_sessions))


def build_failed_case(body: Any, *, request_id: str, claim_day: str, file_hash: str, error: str,
                      created_at: dt.datetime, warmup_sessions: int) -> EvaluationCase:
    return EvaluationCase(
        schema_version=CASE_DOMAIN, kind="INITIAL", request_id=request_id, claim_day=claim_day,
        strategy_key=body.strategy_key, strategy_file_hash=file_hash, cohort=[dict(p) for p in body.cohort],
        conids=list(body.conids), bar_size=body.bar_size, stage="FAILED", holdout_passed=None,
        selected_params=None, family_id=None, selected_trial_id=None, artifact_id=None,
        eligibility_decision_digest=None, decision_state=None, ruleset_digest=None, final_rule_results=[],
        renewal=None, created_at=created_at.isoformat(),
        evidence={"points": [], "strategy_trials": None, "selected_index": None, "replay_index": 0,
                  "holdout": None, "previously_revealed": [], "holdouts_opened_before": None,
                  "warmup_sessions": warmup_sessions, "error": error[:300]})


def _point_metrics(point: Mapping[str, Any]) -> dict:
    return {"expectancy_bps_1x": point["expectancy_bps"]["1x"], "expectancy_bps_1_5x": point["expectancy_bps"]["1.5x"],
            "expectancy_bps_2x": point["expectancy_bps"]["2x"], "selection_statistic": point["selection_statistic"]}


def _point_summary(point: Mapping[str, Any]) -> dict:
    """Per cohort point, code-computed only (Plan 4 builds Jev's case from it)."""
    return {"index": point["index"], "params": point["params"], "pre_holdout_passed": point["pre_holdout_passed"],
            "failed_rules": point["failed_rules"], "missing_rules": point["missing_rules"],
            "metrics": _point_metrics(point)}


def _replayed_point(evidence: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    points = evidence.get("points") or []
    index = evidence.get("replay_index")
    return points[index] if type(index) is int and 0 <= index < len(points) else None


def _renewal_order_notional(case: EvaluationCase) -> float:
    """A RENEWAL case carries the deployed line's own notional."""
    notional = case.evidence.get("order_notional")
    if notional is None:
        prior = None if case.renewal is None else case.renewal.prior_deployment_version
        raise ValueError(f"the RENEWAL case of {case.strategy_key} (renewing {prior}) has no evidence "
                         f"order_notional")
    return notional


def evaluation_summary(case: EvaluationCase, *, order_notional: float) -> dict:
    """Plan 4's EvaluationSummary: the code-computed view Jev's BacktestCase is built from."""
    evidence = case.evidence
    path, class_name = split_strategy_key(case.strategy_key)
    points = evidence.get("points") or []
    shown = _replayed_point(evidence)
    return {
        "kind": case.kind, "strategy_key": case.strategy_key, "strategy_path": path, "class_name": class_name,
        "file_hash": case.strategy_file_hash, "params": case.selected_params, "conids": list(case.conids),
        "bar_size": case.bar_size, "stage": case.stage, "rules_passed": offered_menu(case) == FULL_MENU,
        "holdout_passed": case.holdout_passed,                      # the typed header
        "eligibility": case.decision_state,
        "renewal_checks_passed": renewal_forward_complete(case) if case.kind == "RENEWAL" else None,
        "prior_version_digest": None if case.renewal is None else case.renewal.prior_deployment_version,
        "order_notional": float(_renewal_order_notional(case) if case.kind == "RENEWAL" else order_notional),
        "strategy_trials": evidence.get("strategy_trials") or 0,
        "prior_holdouts": evidence.get("holdouts_opened_before") or 0,
        "previously_revealed_sessions": len(evidence.get("previously_revealed") or []),
        "metrics": {} if shown is None else _point_metrics(shown),
        "rule_results": [{"point": p["index"], "rule": r["code"], "passed": r["passed"]}
                         for p in points for r in p["rules"]],
        "selected_index": evidence.get("selected_index"), "error": evidence.get("error"),
        "points": [_point_summary(p) for p in points],
        "forward": evidence.get("forward")}
