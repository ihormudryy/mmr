"""The research service's ``evaluate`` (SP3 spec 7.6): a catalogue key exactly as SP2c; an authored key is
reported STARTED, smoke-tested, then evaluated by the same cohort evaluation through the research sandbox."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any, Callable

import pandas as pd

from trader.authored.sandbox_client import SandboxUnavailable
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research import authored_backtest            # module reference: tests replace connect_sandbox
from trader.research.authored_outcome import (SANDBOX_ERROR_EVENT, AuthoredEvaluationEnded, end_for, sandbox_cause,
                                             started_body)
from trader.research.cohort import CohortSpec
from trader.research.cohort_evaluation import CohortResult, evaluate_cohort
from trader.research.evaluation import (EvaluationPaths, _day_end, _day_start, _period_session_dates, _point_key,
                                        run_environment)
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_window_job
from trader.research.evaluation_service import ServiceStateError
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.validation import generate_walk_forward

SMOKE_SESSIONS = 20


def smoke_window(spec: CohortSpec, paths: EvaluationPaths) -> tuple[RunEnvironment, WindowJob]:
    """Plan 4 ruling 7: the first sessions of the period, all before the holdout, on the smallest conid."""
    base = spec.base
    plan = generate_walk_forward((base.period_start, base.period_end), n_folds=base.folds,
                                 embargo=base.embargo_sessions, holdout=base.holdout_sessions,
                                 calendar_name=base.calendar)
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    sessions = [day for day in _period_session_dates(base) if day < holdout_start][:SMOKE_SESSIONS]
    params = dict(spec.cohort[0])
    env = replace(run_environment(base, paths), conids=(min(base.conids),))
    job = WindowJob(_point_key(params), params, "smoke", 0, _day_start(sessions[0]), _day_end(sessions[-1]), 1.0)
    return env, job


class AuthoredEvaluator:
    def __init__(self, *, research_db: Any, paths: EvaluationPaths, now: Callable[[], dt.datetime], trader: Any,
                 ruleset=PAPER_V1, run_cohort: Callable[..., Any] = evaluate_cohort,
                 run_window: Callable[[RunEnvironment, WindowJob], Any] = run_window_job):
        self._db, self._paths, self._now, self._trader, self._ruleset = research_db, paths, now, trader, ruleset
        self._run_cohort, self._run_window = run_cohort, run_window

    def evaluate(self, spec: CohortSpec) -> CohortResult:
        if spec.base.authored_digest is None:
            return self._run_cohort(spec, research_db=self._db, paths=self._paths, now=self._now,
                                    ruleset=self._ruleset)
        image_digest, judged_count = self._start(spec)
        paths = replace(self._paths, sandbox_image_digest=image_digest)   # every session must open on this build
        self._smoke(spec, paths)
        return self._evaluate_authored(spec, paths, judged_count)

    def _start(self, spec: CohortSpec) -> tuple[str, int]:
        """Plan 4 ruling 6: STARTED names the image the research sandbox runs; the reply carries the count of
        judged authored files (spec 7.7). An unreachable trader (TraderUnavailable) ends the worker."""
        digest = spec.base.authored_digest
        try:
            image_digest = authored_backtest.connect_sandbox(self._paths.sandbox_socket).ping().image_digest
        except SandboxUnavailable as exc:
            raise AuthoredEvaluationEnded(end_for(exc)) from exc
        try:
            reply = self._trader.report_authored_evaluation(started_body(spec.request_id, digest, image_digest))
        except TypedRpcRemoteError as remote:
            raise ServiceStateError(f"STARTED for {digest}: {remote.code}: {remote.message}") from remote
        if reply.get("status") not in ("UPDATED", "UNCHANGED"):
            raise ServiceStateError(f"the trader refused STARTED for {digest}: {reply.get('code')}: "
                                    f"{reply.get('detail')}")
        judged_count = reply.get("judged_count")
        if type(judged_count) is not int or judged_count < 0:
            raise ServiceStateError(f"STARTED for {digest}: judged_count {judged_count!r} is not a count")
        return image_digest, judged_count

    def _smoke(self, spec: CohortSpec, paths: EvaluationPaths) -> None:
        """Before any family, trial or holdout exists: a fault here spends nothing (spec 7.6)."""
        env, job = smoke_window(spec, paths)
        try:
            self._run_window(env, job)
        except Exception as exc:
            cause = sandbox_cause(exc)
            if cause is None:
                raise
            raise AuthoredEvaluationEnded(end_for(cause)) from exc

    def _evaluate_authored(self, spec: CohortSpec, paths: EvaluationPaths, judged_count: int) -> CohortResult:
        base = spec.base
        registry = ExperimentRegistry(self._db)
        holdouts_before = len(registry.opened_holdout_windows(base.strategy_path, base.class_name))
        try:
            return self._run_cohort(spec, research_db=self._db, paths=paths, now=self._now,
                                    ruleset=self._ruleset, max_workers=1, authored_judged_count=judged_count)
        except Exception as exc:
            cause = sandbox_cause(exc)
            if cause is None:
                raise
            end = end_for(cause)
            holdouts_after = len(registry.opened_holdout_windows(base.strategy_path, base.class_name))
            if end.event != SANDBOX_ERROR_EVENT and holdouts_after > holdouts_before:
                raise                       # spec 8: the window counts as revealed; SP2c signs a FAILED case
            raise AuthoredEvaluationEnded(end) from exc
