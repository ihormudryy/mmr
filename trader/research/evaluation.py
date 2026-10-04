"""`mmr research evaluate`: real backtests -> paper-v1 evidence -> decision.

The holdout opens only when every non-holdout rule already passes, so a run
that could not be deployed never spends it (spec section 2, step 5).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import pandas as pd
import yaml

from trader.data.backtest_store import compute_strategy_hash
from trader.research import evidence as ev
from trader.research.artifact import (
    ARTIFACT_STATE_RETIRED, TRIAL_FAILED, TRIAL_RUNNING, TRIAL_SUCCEEDED, ExperimentFamily,
)
from trader.research.attribution import RoundTrip, build_round_trips
from trader.research.canonical import sha256_digest
from trader.research.eligibility import (
    EligibilityDecisionRepository, EligibilityEvidence, Ruleset, evaluate_eligibility,
)
from trader.research.evaluation_data import load_bars, qualify_dataset
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, WindowOutcome, run_jobs
from trader.research.evaluation_report import write_evaluation_report
from trader.research.evaluation_spec import EvaluationSpec, neighbour_points
from trader.research.evaluation_store import (
    STAGE_COMPLETE, STAGE_HOLDOUT_FAILED, STAGE_PRE_HOLDOUT,
    EvaluationRecord, EvaluationRepository, write_evaluation_summary,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import DatasetManifestRepository
from trader.research.statistics import annualized_sharpe_ci, profit_factor
from trader.research.validation import ValidationPlan, generate_walk_forward

COST_MULTIPLIERS = (1.0, 1.5, 2.0)
# The image a run used is provenance (the report records it), not identity:
# the same strategy, data and costs in a rebuilt image are the same family.
FAMILY_CONTAINER_DIGEST = 'unpinned'


class EvaluationError(Exception):
    """The evaluation cannot continue; the message names the cause."""


@dataclass(frozen=True)
class EvaluationPaths:
    history_db: str
    universe_db: str
    universe_library: str
    execution_costs: str
    repo_root: Path
    reports_dir: Path
    summaries_dir: Path


@dataclass(frozen=True)
class EvaluationResult:
    spec_name: str
    family_id: str
    stage: str
    state: str
    artifact_id: Optional[str]
    decision_digest: Optional[str]
    failed_rules: tuple[str, ...]
    missing_rules: tuple[str, ...]
    report_path: Path


@dataclass
class PointResult:
    params: dict
    trial_id: str
    metrics: dict
    # cost multiplier -> fold outcomes in fold order; empty for a reused neighbour
    outcomes: dict = field(default_factory=dict)


def evaluate(spec: EvaluationSpec, *, research_db: Any, paths: EvaluationPaths,
             now: Callable[[], dt.datetime], max_workers: int = 1,
             ruleset: Ruleset = PAPER_V1) -> EvaluationResult:
    repository_commit = _repository_commit(paths.repo_root, spec.strategy_file)
    registry = ExperimentRegistry(research_db)
    plan = generate_walk_forward((spec.period_start, spec.period_end), n_folds=spec.folds,
                                 embargo=spec.embargo_sessions, holdout=spec.holdout_sessions,
                                 calendar_name=spec.calendar)
    bars = load_bars(paths.history_db, spec.conids, spec.bar_size,
                     _day_start(spec.period_start), _day_end(spec.period_end))
    manifest_digest = DatasetManifestRepository(research_db).seal(
        qualify_dataset(bars, spec), sealed_at=now())
    family_id = registry.create_family(_family(spec, manifest_digest, repository_commit, paths),
                                       created_at=now(), validation_folds=_fold_specs(plan))
    _refuse_if_holdout_opened(registry, family_id)

    env = RunEnvironment(
        history_db=paths.history_db, universe_db=paths.universe_db,
        universe_library=paths.universe_library, execution_costs_path=paths.execution_costs,
        strategy_file=str(spec.strategy_file), class_name=spec.class_name,
        conids=tuple(spec.conids), bar_size=spec.bar_size, order_notional=spec.order_notional,
        account_equity=spec.account_equity, max_gross_allocation=spec.max_gross_allocation)
    main = _run_point(registry, family_id, env, plan, dict(spec.params), COST_MULTIPLIERS,
                      now, max_workers, rerun_existing=True)
    neighbours = [_run_point(registry, family_id, env, plan, point, (1.0,), now, max_workers,
                             rerun_existing=False) for point in neighbour_points(spec)]
    evidence = _walk_forward_evidence(
        spec, main, neighbours, registry.strategy_trials(spec.strategy_path, spec.class_name))
    gate = ev.pre_holdout_outcome(evaluate_eligibility(ruleset, evidence))
    if not gate.passed:
        return _finish(research_db, spec, paths, now, family_id=family_id,
                       stage=STAGE_PRE_HOLDOUT, state='CANDIDATE', artifact_id=None,
                       decision_digest=None, gate=gate, evidence=evidence, main=main,
                       neighbours=neighbours)

    artifact_id = registry.seal_artifact(family_id, selected_trial_id=main.trial_id,
                                         selected_parameters=dict(spec.params), sealed_at=now())
    start, end = _day_start(plan.holdout.start), _day_end(plan.holdout.end)
    first, second = run_jobs(env, [
        WindowJob(_point_key(spec.params), dict(spec.params), 'holdout', 0, start, end, 1.0,
                  replay=replay) for replay in (0, 1)], max_workers)
    passed = ev.holdout_passes(build_round_trips(first.trades), first.max_drawdown)
    registry.open_holdout(artifact_id, opened_at=now(), passed=passed,
                          detail=f'net_pnl={first.net_pnl:.2f} max_drawdown={first.max_drawdown:.4f}')
    evidence = replace(
        evidence, scaled_holdout_drawdown=first.max_drawdown,
        deterministic_replay_ok=first.trace_signature == second.trace_signature,
        holdout_opened_once=registry.get_artifact(artifact_id).holdout_opened)
    decision = evaluate_eligibility(ruleset, evidence)
    if not passed:
        # paper-v1 has no holdout-expectancy rule, so the decision alone could still
        # read PAPER_ELIGIBLE. Record none: a retired artifact must never be attested.
        return _finish(research_db, spec, paths, now, family_id=family_id,
                       stage=STAGE_HOLDOUT_FAILED, state=ARTIFACT_STATE_RETIRED,
                       artifact_id=artifact_id, decision_digest=None,
                       gate=ev.final_outcome(decision), evidence=evidence, main=main,
                       neighbours=neighbours)
    decision_digest = EligibilityDecisionRepository(research_db).record(
        decision, artifact_id=artifact_id, recorded_at=now())
    return _finish(research_db, spec, paths, now, family_id=family_id, stage=STAGE_COMPLETE,
                   state=decision.state, artifact_id=artifact_id,
                   decision_digest=decision_digest, gate=ev.final_outcome(decision),
                   evidence=evidence, main=main, neighbours=neighbours)


def _day_start(day) -> dt.datetime:
    return dt.datetime.combine(pd.Timestamp(day).date(), dt.time.min, tzinfo=dt.timezone.utc)


def _day_end(day) -> dt.datetime:
    return dt.datetime.combine(pd.Timestamp(day).date(), dt.time.max, tzinfo=dt.timezone.utc)


def _point_key(params) -> str:
    return 'point:' + json.dumps(dict(params), sort_keys=True, separators=(',', ':'))


def _file_digest(path: Path) -> str:
    return 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _costs_config_digest(path: Path) -> str:
    """Digest of the parsed costs config, so comments and layout do not change it."""
    try:
        parsed = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise EvaluationError(f'{path} is not valid YAML: {exc}') from exc
    return 'sha256:' + sha256_digest('execution_costs_config', parsed)


def _container_digest() -> str:
    return os.environ.get('MMR_CONTAINER_DIGEST', 'local:none')


def _dependency_lock_digest(repo_root: Path) -> str:
    for name in ('uv.lock', 'requirements.txt', 'pyproject.toml'):
        if (repo_root / name).is_file():
            return _file_digest(repo_root / name)
    return 'unknown'


def _repository_commit(repo_root: Path, strategy_file: Path) -> str:
    """The last commit that touched the strategy file.

    HEAD would not do: it moves on every unrelated commit, and the commit is part
    of the family identity, so each one would hand the same spec a fresh holdout.
    """
    def git(*args):
        return subprocess.run(['git', '-C', str(repo_root), *args],
                              capture_output=True, text=True, timeout=10)

    if git('rev-parse', '--is-inside-work-tree').returncode != 0:
        return 'unknown'  # not a git checkout; the source digest still pins the file
    commit = git('log', '-1', '--format=%H', '--', str(strategy_file)).stdout.strip()
    if not commit:
        raise EvaluationError(
            f'{strategy_file.name} is not committed; commit it so the evidence names a real commit')
    if git('status', '--porcelain', '--', str(strategy_file)).stdout.strip():
        raise EvaluationError(
            f'{strategy_file.name} has uncommitted changes; commit it so the evidence names a real commit')
    return commit


def _family(spec: EvaluationSpec, manifest_digest: str, repository_commit: str,
            paths: EvaluationPaths) -> ExperimentFamily:
    return ExperimentFamily(
        strategy_path=spec.strategy_path, class_name=spec.class_name,
        repository_commit=repository_commit,
        source_tree_digest=compute_strategy_hash(str(spec.strategy_file)),
        dependency_lock_digest=_dependency_lock_digest(paths.repo_root),
        container_digest=FAMILY_CONTAINER_DIGEST,
        dataset_manifest_digest=manifest_digest,
        search_space={'params': dict(spec.params),
                      'neighbourhood': {k: list(v) for k, v in spec.neighbourhood.items()}},
        cost_model={'model': 'realistic', 'config_digest': _costs_config_digest(Path(paths.execution_costs)),
                    'order_notional': spec.order_notional, 'account_equity': spec.account_equity,
                    'max_gross_allocation': spec.max_gross_allocation},
        validation_protocol={'calendar': spec.calendar, 'bar_size': spec.bar_size,
                             'conids': sorted(spec.conids),
                             'period_start': spec.period_start.isoformat(),
                             'period_end': spec.period_end.isoformat(), 'folds': spec.folds,
                             'embargo_sessions': spec.embargo_sessions,
                             'holdout_sessions': spec.holdout_sessions})


def _fold_specs(plan: ValidationPlan) -> list[dict]:
    def day(value) -> str:
        return pd.Timestamp(value).date().isoformat()

    specs = [{'kind': 'walk_forward', 'index': f.index, 'train_start': day(f.train_start),
              'train_end': day(f.train_end), 'test_start': day(f.test_start),
              'test_end': day(f.test_end)} for f in plan.folds]
    specs.append({'kind': 'holdout', 'start': day(plan.holdout.start), 'end': day(plan.holdout.end)})
    return specs


def _refuse_if_holdout_opened(registry: ExperimentRegistry, family_id: str) -> None:
    for artifact in registry.family_artifacts(family_id):
        if artifact.holdout_opened:
            raise EvaluationError(
                f'holdout already opened for family {family_id[:12]}; '
                f'change the spec to start a new family')


def _round_trips(outcomes: Sequence[WindowOutcome]) -> list[RoundTrip]:
    return [rt for o in outcomes for rt in build_round_trips(o.trades)]


def _trial_metrics(outcomes: Sequence[WindowOutcome], starting_equity: float) -> dict:
    trips = _round_trips(outcomes)
    returns = ev.session_returns([o.equity_series() for o in outcomes],
                                 starting_equity=starting_equity)
    return {
        'oos_net_pnl': float(sum(o.net_pnl for o in outcomes)),
        'oos_expectancy_bps': ev.dollar_weighted_expectancy_bps(trips),
        'daily_sharpe': ev.per_period_sharpe(returns),
        'n_round_trips': len(trips),
        'fold_net_pnls': [float(o.net_pnl) for o in outcomes],
        'trace_signature': ev.combined_trace_signature([o.trace_signature for o in outcomes]),
    }


def _run_point(registry: ExperimentRegistry, family_id: str, env: RunEnvironment,
               plan: ValidationPlan, params: dict, multipliers: Sequence[float],
               now: Callable[[], dt.datetime], max_workers: int, *,
               rerun_existing: bool) -> PointResult:
    key = _point_key(params)
    attempts = [t for t in registry.list_trials(family_id, include_archived=True)
                if t.trial_key.split('#')[0] == key]
    for stale in (t for t in attempts if t.status == TRIAL_RUNNING):
        registry.finish_trial(stale.trial_id, status=TRIAL_FAILED, finished_at=now(),
                              safe_summary='interrupted before it finished')
    succeeded = next((t for t in attempts if t.status == TRIAL_SUCCEEDED), None)
    if succeeded is not None and not rerun_existing:
        return PointResult(params, succeeded.trial_id, dict(succeeded.metrics))

    trial_id = succeeded.trial_id if succeeded else registry.start_trial(
        family_id, trial_key=f'{key}#{len(attempts) + 1}', parameters=params, started_at=now())
    jobs = [WindowJob(key, params, 'fold', fold.index, _day_start(fold.test_start),
                      _day_end(fold.test_end), multiplier)
            for multiplier in multipliers for fold in plan.folds]
    try:
        outcomes = run_jobs(env, jobs, max_workers)
    except Exception as exc:
        if succeeded is None:
            registry.finish_trial(trial_id, status=TRIAL_FAILED, finished_at=now(),
                                  traceback=traceback.format_exc(),
                                  safe_summary=type(exc).__name__)
        raise EvaluationError(f'backtest failed for {key}: {exc}') from exc

    by_multiplier = {m: sorted((o for o in outcomes if o.job.cost_multiplier == m),
                               key=lambda o: o.job.window_index) for m in multipliers}
    metrics = _trial_metrics(by_multiplier[1.0], env.account_equity)
    if succeeded is None:
        registry.finish_trial(trial_id, status=TRIAL_SUCCEEDED, finished_at=now(), metrics=metrics)
    elif succeeded.metrics.get('trace_signature') != metrics['trace_signature']:
        raise EvaluationError(
            f'{key} produced different trades than its recorded trial; '
            f'the data or the code changed since the last run')
    return PointResult(params, trial_id, metrics, by_multiplier)


def _walk_forward_evidence(spec: EvaluationSpec, main: PointResult,
                           neighbours: Sequence[PointResult], strategy_trials) -> EligibilityEvidence:
    trips = {m: _round_trips(main.outcomes[m]) for m in COST_MULTIPLIERS}
    baseline = trips[1.0]
    returns = ev.session_returns([o.equity_series() for o in main.outcomes[1.0]],
                                 starting_equity=spec.account_equity)
    sharpe_ci = annualized_sharpe_ci(returns, periods_per_year=ev.TRADING_DAYS_PER_YEAR, seed=0)
    trial_sharpes = [t.metrics['daily_sharpe'] for t in strategy_trials
                     if isinstance(t.metrics.get('daily_sharpe'), (int, float))]
    return EligibilityEvidence(
        n_round_trips=len(baseline),
        n_instruments=len(set(spec.conids)),
        expectancy_bps_baseline=ev.dollar_weighted_expectancy_bps(baseline),
        expectancy_bps_1_5x=ev.dollar_weighted_expectancy_bps(trips[1.5]),
        expectancy_bps_2x=ev.dollar_weighted_expectancy_bps(trips[2.0]),
        selection_adjusted_confidence=ev.selection_confidence(
            returns, n_trials=len(strategy_trials), trial_sharpes=trial_sharpes),
        annualized_sharpe_ci_low=None if sharpe_ci is None else sharpe_ci.low,
        profit_factor=profit_factor([rt.pnl for rt in baseline]) if baseline else None,
        walk_forward_positive_fraction=ev.positive_fold_fraction(main.metrics['fold_net_pnls']),
        max_month_profit_share=ev.month_concentration(baseline),
        max_instrument_profit_share=ev.instrument_concentration(baseline),
        neighborhood_robust=ev.neighbourhood_robust(
            [n.metrics.get('oos_expectancy_bps') for n in neighbours]),
    )


def _finish(research_db, spec: EvaluationSpec, paths: EvaluationPaths, now, *, family_id: str,
            stage: str, state: str, artifact_id: Optional[str], decision_digest: Optional[str],
            gate: ev.GateOutcome, evidence: EligibilityEvidence, main: PointResult,
            neighbours: Sequence[PointResult]) -> EvaluationResult:
    created_at = now()
    report_path = write_evaluation_report(
        paths.reports_dir, spec=spec, family_id=family_id, stage=stage, state=state,
        artifact_id=artifact_id, failed=gate.failed, missing=gate.missing, evidence=evidence,
        main=main, neighbours=neighbours, created_at=created_at,
        container_digest=_container_digest())
    record = EvaluationRecord(
        spec_name=spec.name, family_id=family_id, strategy_path=spec.strategy_path,
        class_name=spec.class_name, stage=stage, state=state, artifact_id=artifact_id,
        decision_digest=decision_digest, failed_rules=gate.failed, missing_rules=gate.missing,
        report_path=str(report_path), created_at=created_at)
    EvaluationRepository(research_db).record(record)
    write_evaluation_summary(paths.summaries_dir, record)
    return EvaluationResult(spec.name, family_id, stage, state, artifact_id, decision_digest,
                            gate.failed, gate.missing, report_path)
