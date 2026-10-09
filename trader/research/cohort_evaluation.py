"""Cohort evaluation (SP2c spec 5.1, 6.2): every point through the full pre-holdout gate,
a code selection on walk-forward evidence only, then one holdout for the selected point."""
from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

import pandas as pd

from trader.research import evidence as ev
from trader.research.artifact import ARTIFACT_STATE_RETIRED
from trader.research.cohort import CohortSpec, previously_revealed, require_fresh_holdout
from trader.research.eligibility import evaluate_eligibility
from trader.research.evaluation import (
    COST_MULTIPLIERS, EvaluationError, EvaluationPaths, HoldoutOutcome, PointResult, _day_end, _day_start, _family, _finish,
    _fold_specs, _period_session_dates, _point_key, _refuse_if_holdout_opened, _repository_commit, _run_point,
    _walk_forward_evidence, point_market_context, run_environment, run_holdout)
from trader.research.evaluation_data import load_bars, load_benchmark_closes, qualify_dataset
from trader.research.evaluation_store import STAGE_COMPLETE, STAGE_HOLDOUT_FAILED, STAGE_PRE_HOLDOUT
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import DatasetManifestRepository
from trader.research.validation import generate_walk_forward


@dataclass(frozen=True)
class PointVerdict:
    index: int
    point: PointResult
    neighbours: tuple[PointResult, ...]
    gate: ev.GateOutcome
    decision: Any            # the pre-holdout EligibilityDecision (every rule result)
    evidence: Any            # EligibilityEvidence
    context: Any             # PointContext


@dataclass(frozen=True)
class CohortResult:
    family_id: str
    stage: str
    verdicts: tuple[PointVerdict, ...]
    strategy_trials: int
    selected: Optional[PointVerdict]
    replay_index: int
    holdout: Optional[HoldoutOutcome]
    previously_revealed: list[str]
    holdouts_opened_before: int


def _statistic(verdict: PointVerdict) -> float:
    value = verdict.evidence.selection_adjusted_confidence
    return value if isinstance(value, (int, float)) and math.isfinite(value) else -math.inf


def select_point(verdicts: Sequence[PointVerdict]) -> Optional[PointVerdict]:
    """Ruling 5: highest deflated statistic among points that pass the full pre-holdout gate; ties by
    cohort order. Holdout data never feeds this choice (no holdout is open yet)."""
    passing = [v for v in verdicts if v.gate.passed]
    return max(passing, key=lambda v: (_statistic(v), -v.index)) if passing else None


def replay_index(verdicts: Sequence[PointVerdict], selected: Optional[PointVerdict]) -> int:
    """Ruling 7: the point the shadow replay runs."""
    if selected is not None:
        return selected.index
    scored = [v for v in verdicts if _statistic(v) > -math.inf]
    return max(scored, key=lambda v: (_statistic(v), -v.index)).index if scored else 0


def _verdict(index, base, plan, point, neighbours, trials, benchmark_closes, bars, ruleset) -> PointVerdict:
    context = point_market_context(base, plan, point, benchmark_closes, bars)
    evidence = _walk_forward_evidence(base, point, neighbours, trials, context.regimes, context.liquidity)
    decision = evaluate_eligibility(ruleset, evidence)
    return PointVerdict(index, point, tuple(neighbours), ev.pre_holdout_outcome(decision), decision, evidence,
                        context)


def _cohort_search_space(spec: CohortSpec) -> dict:
    return {"cohort": [dict(p) for p in spec.cohort],
            "neighbourhood": {_point_key(point): [dict(n) for n in near]
                              for point, near in zip(spec.cohort, spec.neighbours, strict=True)}}


def _pre_holdout_sessions(base, plan) -> list[dt.date]:
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    return [d for d in _period_session_dates(base) if d < holdout_start]


def _record_evaluation(research_db, base, paths, now, result: CohortResult, verdict: PointVerdict) -> None:
    """The existing evaluation log and report, for the selected (or replayed) point."""
    outcome = result.holdout
    if outcome is None:
        state, gate, evidence, artifact_id, decision_digest = (
            "CANDIDATE", verdict.gate, verdict.evidence, None, None)
    else:
        state = outcome.decision.state if outcome.passed else ARTIFACT_STATE_RETIRED
        gate, evidence = ev.final_outcome(outcome.decision), outcome.evidence
        artifact_id, decision_digest = outcome.artifact_id, outcome.decision_digest
    _finish(research_db, replace(base, params=dict(verdict.point.params)), paths, now,
            family_id=result.family_id, stage=result.stage, state=state, artifact_id=artifact_id,
            decision_digest=decision_digest, gate=gate, evidence=evidence, main=verdict.point,
            neighbours=list(verdict.neighbours), market_context=verdict.context.market_context,
            missing_causes=verdict.context.missing_causes)


def evaluate_cohort(spec: CohortSpec, *, research_db: Any, paths: EvaluationPaths,
                    now: Callable[[], dt.datetime], ruleset=PAPER_V1, max_workers: int = 1) -> CohortResult:
    base = spec.base
    if not spec.cohort:
        raise EvaluationError("the cohort has no points")
    registry = ExperimentRegistry(research_db)
    plan = generate_walk_forward((base.period_start, base.period_end), n_folds=base.folds,
                                 embargo=base.embargo_sessions, holdout=base.holdout_sessions,
                                 calendar_name=base.calendar)
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    windows = registry.opened_holdout_windows(base.strategy_path, base.class_name)
    require_fresh_holdout(windows, holdout_start)
    repository_commit = _repository_commit(paths.repo_root, base.strategy_file)
    bars = load_bars(paths.history_db, base.conids, base.bar_size, _day_start(base.period_start),
                     _day_end(base.period_end))
    benchmark_closes = load_benchmark_closes(paths.history_db, base)
    manifest_digest = DatasetManifestRepository(research_db).seal(
        qualify_dataset(bars, base, benchmark_closes=benchmark_closes), sealed_at=now())
    family = replace(_family(base, manifest_digest, repository_commit, paths),
                     search_space=_cohort_search_space(spec))
    family_id = registry.create_family(family, created_at=now(), validation_folds=_fold_specs(plan))
    _refuse_if_holdout_opened(registry, family_id)
    registry.close_interrupted_strategy_trials(base.strategy_path, base.class_name, finished_at=now())
    env = run_environment(base, paths)
    # Cohort points first, so a neighbour equal to a cohort point reuses that trial.
    points = [_run_point(registry, family_id, env, plan, dict(p), COST_MULTIPLIERS, now, max_workers,
                         rerun_existing=True) for p in spec.cohort]
    neighbours = [[_run_point(registry, family_id, env, plan, dict(n), (1.0,), now, max_workers,
                              rerun_existing=False) for n in ns] for ns in spec.neighbours]
    trials = registry.strategy_trials(base.strategy_path, base.class_name)   # one denominator for all points
    verdicts = tuple(_verdict(i, base, plan, p, ns, trials, benchmark_closes, bars, ruleset)
                     for i, (p, ns) in enumerate(zip(points, neighbours, strict=True)))
    selected = select_point(verdicts)
    common = dict(family_id=family_id, verdicts=verdicts, strategy_trials=len(trials),
                  replay_index=replay_index(verdicts, selected),
                  previously_revealed=previously_revealed(windows, _pre_holdout_sessions(base, plan)),
                  holdouts_opened_before=len(windows))
    if selected is None:
        result = CohortResult(stage=STAGE_PRE_HOLDOUT, selected=None, holdout=None, **common)
        recorded = verdicts[result.replay_index]
    else:
        require_fresh_holdout(registry.opened_holdout_windows(base.strategy_path, base.class_name), holdout_start)
        # run_holdout adds holdout-derived keys to the context it gets; the verdict keeps its own.
        recorded = replace(selected, context=copy.deepcopy(selected.context))
        outcome = run_holdout(research_db, registry, replace(base, params=dict(selected.point.params)), env, plan,
                              family_id, selected.point, selected.evidence, benchmark_closes, recorded.context,
                              now, max_workers, ruleset)
        stage = STAGE_COMPLETE if outcome.passed else STAGE_HOLDOUT_FAILED
        result = CohortResult(stage=stage, selected=selected, holdout=outcome, **common)
    _record_evaluation(research_db, base, paths, now, result, recorded)
    return result
