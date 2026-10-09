"""Checks of one `submit_evaluation` request, by code, before any claim (SP2c spec 5.1 step 2, 6.2)."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import exchange_calendars as xcals
import pandas as pd
from pydantic import ValidationError

from trader.research.evaluation_data import BarsMissing, EvaluationDataError, require_bars_available
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.evaluation_spec import (MIN_INSTRUMENTS, EvaluationSpec, EvaluationSpecError,
                                             build_evaluation_spec, declared_tunables)
from trader.research.neighbours import NEIGHBOUR_SHARE, neighbours_of  # noqa: F401  re-exported
from trader.research.strategy_key import split_strategy_key
from trader.research.validation import generate_walk_forward


class RequestRefused(Exception):
    """``retryable``: nothing was decided; the same body may succeed later (BARS_MISSING after a refresh)."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.retryable = code, detail, retryable


def request_body(raw: Mapping[str, Any], research_day: dt.date) -> EvaluationRequestBody:
    """Plan 1's canonical claim body: the caller's four fields plus the service's New York day (ruling 1)."""
    if not isinstance(raw, Mapping) or "research_day" in raw:
        raise RequestRefused("REQUEST_INVALID", "the research service sets research_day")
    try:
        return EvaluationRequestBody.model_validate({**raw, "research_day": research_day.isoformat()})
    except ValidationError as exc:
        raise RequestRefused("REQUEST_INVALID", exc.errors()[0]["msg"]) from None


def evaluation_period(research_day: dt.date, config: Any) -> tuple[dt.date, dt.date]:
    """The last complete XNYS session before ``research_day`` closes the period."""
    calendar = xcals.get_calendar("XNYS")
    end = calendar.date_to_session(pd.Timestamp(research_day), direction="previous")
    if end.date() >= research_day:
        end = calendar.previous_session(end)
    start = calendar.session_offset(end, -(config.period_sessions - 1))
    return start.date(), end.date()


def require_fresh_holdout(windows: Sequence[Mapping[str, Any]], holdout_start: dt.date) -> None:
    """Spec 6.2: a new holdout lies after every revealed session; shifting by a day does not help."""
    if not windows:
        return
    last_revealed = max(w["end"] for w in windows)
    if holdout_start <= last_revealed:
        raise RequestRefused("HOLDOUT_NOT_AVAILABLE",
                             f"the next holdout must start after {last_revealed}; it would start {holdout_start}")


def previously_revealed(windows: Sequence[Mapping[str, Any]], sessions: Sequence[dt.date]) -> list[str]:
    return [d.isoformat() for d in sessions if any(w["start"] <= d <= w["end"] for w in windows)]


@dataclass(frozen=True)
class CohortSpec:
    request_id: str
    body: EvaluationRequestBody               # the exact claim body
    strategy_key: str
    base: EvaluationSpec                       # params = cohort[0]; carries period, folds, costs, conids
    cohort: tuple[Mapping[str, Any], ...]
    neighbours: tuple[tuple[dict, ...], ...]   # per cohort point; never selectable
    file_hash: str


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _require_bars(history_db: str, base: EvaluationSpec) -> None:
    try:
        require_bars_available(history_db, base)
    except BarsMissing as missing:
        raise RequestRefused("BARS_MISSING", str(missing), retryable=True) from None
    except EvaluationDataError as exc:              # the SPY lookback reaches before the calendar
        raise RequestRefused("SPEC_INVALID", str(exc)) from None


def build_cohort_spec(body: EvaluationRequestBody, *, config: Any, judge: Any, universe_accessor: Any,
                      costs_config: Any, repo_root: Path, registry: Any, history_db: str) -> CohortSpec:
    if not judge.allows(body.strategy_key):
        raise RequestRefused("STRATEGY_NOT_ALLOWED", f"{body.strategy_key} is not on the allowlist")
    cohort = [dict(point) for point in body.cohort]
    if len(cohort) > judge.max_cohort_points:
        raise RequestRefused("COHORT_TOO_LARGE", f"a cohort has at most {judge.max_cohort_points} points")
    if len(body.conids) < MIN_INSTRUMENTS:
        raise RequestRefused("CONIDS_OUT_OF_SCOPE", f"at least {MIN_INSTRUMENTS} distinct conids required")
    neighbours = tuple(neighbours_of(point) for point in cohort)
    if not all(neighbours):
        raise RequestRefused("COHORT_POINT_INVALID", "every point needs a numeric tunable for its neighbours")
    path, class_name = split_strategy_key(body.strategy_key)
    try:
        start, end = evaluation_period(dt.date.fromisoformat(body.research_day), config)
    except ValueError as exc:                    # the XNYS calendar does not reach back that far
        raise RequestRefused("SPEC_INVALID", f"period: {exc}") from None
    first = cohort[0]
    raw = {"name": f"ai-{evaluation_request_id(body)[7:19]}", "strategy": path, "class": class_name,
           "params": first,
           "neighbourhood": {key: [n[key] for n in neighbours[0] if n[key] != first[key]]
                             for key in first if any(n[key] != first[key] for n in neighbours[0])},
           "conids": list(body.conids), "bar_size": body.bar_size, "period": {"start": start, "end": end},
           "walk_forward": {"folds": config.folds, "embargo_sessions": config.embargo_sessions,
                            "holdout_sessions": config.holdout_sessions},
           "sizing": {"order_notional": config.order_notional, "account_equity": config.account_equity},
           "max_gross_allocation": config.max_gross_allocation}
    try:
        base = build_evaluation_spec(raw, universe_accessor=universe_accessor, costs_config=costs_config,
                                     repo_root=repo_root)
    except EvaluationSpecError as exc:
        raise RequestRefused("SPEC_INVALID", str(exc)) from None
    tunables = declared_tunables(base.strategy_file, base.class_name)
    for index, point in enumerate(cohort[1:], start=1):
        if set(point) - tunables:
            raise RequestRefused("COHORT_POINT_INVALID", f"point {index}: not tunables {sorted(set(point) - tunables)}")
    try:
        plan = generate_walk_forward((base.period_start, base.period_end), n_folds=base.folds,
                                     embargo=base.embargo_sessions, holdout=base.holdout_sessions,
                                     calendar_name=base.calendar)
    except ValueError as exc:
        raise RequestRefused("SPEC_INVALID", f"walk_forward: {exc}") from None
    require_fresh_holdout(registry.opened_holdout_windows(base.strategy_path, base.class_name),
                          pd.Timestamp(plan.holdout.start).date())
    _require_bars(history_db, base)
    return CohortSpec(request_id=evaluation_request_id(body), body=body, strategy_key=body.strategy_key,
                      base=base, cohort=tuple(cohort), neighbours=neighbours, file_hash=_file_hash(base.strategy_file))
