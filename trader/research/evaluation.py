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

import exchange_calendars as xcals
import pandas as pd
import yaml

from trader.data.backtest_store import compute_strategy_hash
from trader.research import evidence as ev
from trader.research import market_context as mcx
from trader.research.artifact import (
    ARTIFACT_STATE_RETIRED, TRIAL_FAILED, TRIAL_RUNNING, TRIAL_SUCCEEDED, ExperimentFamily,
)
from trader.research.attribution import RoundTrip, build_round_trips, regime_taxonomy_digest
from trader.research.canonical import sha256_digest
from trader.research.eligibility import (
    EligibilityDecisionRepository, EligibilityEvidence, Ruleset, evaluate_eligibility,
)
from trader.research.evaluation_data import load_bars, load_benchmark_closes, qualify_dataset
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, WindowOutcome, run_jobs
from trader.research.evaluation_report import write_evaluation_report
from trader.research.evaluation_spec import EvaluationSpec, neighbour_points
from trader.research.evaluation_store import (
    STAGE_COMPLETE, STAGE_HOLDOUT_FAILED, STAGE_PRE_HOLDOUT,
    EvaluationRecord, EvaluationRepository, write_evaluation_summary,
)
from trader.research.experiment_registry import INTERRUPTED_SUMMARY, ExperimentRegistry
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
    benchmark_closes = load_benchmark_closes(paths.history_db, spec)
    manifest_digest = DatasetManifestRepository(research_db).seal(
        qualify_dataset(bars, spec, benchmark_closes=benchmark_closes), sealed_at=now())
    family_id = registry.create_family(_family(spec, manifest_digest, repository_commit, paths),
                                       created_at=now(), validation_folds=_fold_specs(plan))
    _refuse_if_holdout_opened(registry, family_id)
    _refuse_if_strategy_holdout_overlaps(registry, spec, plan)

    registry.close_interrupted_strategy_trials(spec.strategy_path, spec.class_name, finished_at=now())
    env = run_environment(spec, paths)
    main = _run_point(registry, family_id, env, plan, dict(spec.params), COST_MULTIPLIERS,
                      now, max_workers, rerun_existing=True)
    neighbours = [_run_point(registry, family_id, env, plan, point, (1.0,), now, max_workers,
                             rerun_existing=False) for point in neighbour_points(spec)]
    context = point_market_context(spec, plan, main, benchmark_closes, bars)
    evidence = _walk_forward_evidence(
        spec, main, neighbours, registry.strategy_trials(spec.strategy_path, spec.class_name),
        context.regimes, context.liquidity)
    gate = ev.pre_holdout_outcome(evaluate_eligibility(ruleset, evidence))
    finish = dict(family_id=family_id, main=main, neighbours=neighbours,
                  market_context=context.market_context, missing_causes=context.missing_causes)
    if not gate.passed:
        return _finish(research_db, spec, paths, now, stage=STAGE_PRE_HOLDOUT, state='CANDIDATE',
                       artifact_id=None, decision_digest=None, gate=gate, evidence=evidence, **finish)
    outcome = run_holdout(research_db, registry, spec, env, plan, family_id, main, evidence,
                          benchmark_closes, context, now, max_workers, ruleset)
    if not outcome.passed:
        # paper-v1 has no holdout-expectancy rule, so the decision alone could still
        # read PAPER_ELIGIBLE. Record none: a retired artifact must never be attested.
        return _finish(research_db, spec, paths, now, stage=STAGE_HOLDOUT_FAILED,
                       state=ARTIFACT_STATE_RETIRED, artifact_id=outcome.artifact_id, decision_digest=None,
                       gate=ev.final_outcome(outcome.decision), evidence=outcome.evidence, **finish)
    return _finish(research_db, spec, paths, now, stage=STAGE_COMPLETE, state=outcome.decision.state,
                   artifact_id=outcome.artifact_id, decision_digest=outcome.decision_digest,
                   gate=ev.final_outcome(outcome.decision), evidence=outcome.evidence, **finish)


def run_environment(spec: EvaluationSpec, paths: EvaluationPaths) -> RunEnvironment:
    return RunEnvironment(
        history_db=paths.history_db, universe_db=paths.universe_db,
        universe_library=paths.universe_library, execution_costs_path=paths.execution_costs,
        strategy_file=str(spec.strategy_file), class_name=spec.class_name,
        conids=tuple(spec.conids), bar_size=spec.bar_size, order_notional=spec.order_notional,
        account_equity=spec.account_equity, max_gross_allocation=spec.max_gross_allocation)


@dataclass(frozen=True)
class PointContext:
    sessions: list
    regimes: Any
    liquidity: Any
    missing_causes: dict
    market_context: dict


def point_market_context(spec, plan, point: PointResult, benchmark_closes, bars) -> PointContext:
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    try:
        sessions = _period_session_dates(spec)
        labels = mcx.regime_labels(benchmark_closes, sessions)
        walk_trips = mcx.annotate_regimes(_round_trips(point.outcomes[1.0]), labels)
        regimes = mcx.regime_evidence(
            walk_trips, labels.loc[[d for d in sessions if d < holdout_start]])
        liquidity = mcx.liquidity_envelope(bars, order_notional=spec.order_notional,
                                           before=holdout_start)
    except mcx.MarketContextError as exc:
        raise EvaluationError(f'market context unavailable: {exc}') from exc
    missing_causes = _missing_causes({
        'liquidity_capacity_envelope': liquidity.within,
        'regime_positive_expectancy_fraction': regimes.positive_fraction,
        'regime_loss_tolerance': regimes.worst_loss,
        'regime_transition_stability': regimes.transitions_stable})
    return PointContext(sessions, regimes, liquidity, missing_causes,
                        {'regimes': _regime_context(regimes),
                         'liquidity': _liquidity_context(liquidity)})


@dataclass(frozen=True)
class HoldoutOutcome:
    artifact_id: str
    passed: bool
    evidence: EligibilityEvidence
    decision: Any
    decision_digest: Optional[str]
    window: dict


def run_holdout(research_db, registry: ExperimentRegistry, spec: EvaluationSpec, env: RunEnvironment,
                plan: ValidationPlan, family_id: str, point: PointResult, evidence: EligibilityEvidence,
                benchmark_closes: pd.Series, context: PointContext, now, max_workers: int,
                ruleset: Ruleset) -> HoldoutOutcome:
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    holdout_end = pd.Timestamp(plan.holdout.end).date()
    artifact_id = registry.seal_artifact(family_id, selected_trial_id=point.trial_id,
                                         selected_parameters=dict(point.params), sealed_at=now())
    # The window counts as revealed from here on: a run that dies below must not leave it fresh.
    registry.begin_holdout(artifact_id, opened_at=now())
    try:
        start, end = _day_start(plan.holdout.start), _day_end(plan.holdout.end)
        first, second = run_jobs(env, [
            WindowJob(_point_key(point.params), dict(point.params), 'holdout', 0, start, end, 1.0,
                      replay=replay) for replay in (0, 1)], max_workers)
        benchmark, time_in_market = _holdout_market_evidence(
            spec, benchmark_closes, first, holdout_start, holdout_end, context.sessions)
        passed = ev.holdout_passes(build_round_trips(first.trades), first.max_drawdown)
    except Exception as exc:
        registry.finish_holdout(artifact_id, passed=False,
                                detail=f'interrupted before a result: {type(exc).__name__}')
        raise
    detail = f'net_pnl={first.net_pnl:.2f} max_drawdown={first.max_drawdown:.4f}'
    registry.finish_holdout(artifact_id, passed=passed, detail=detail)
    evidence = replace(
        evidence, scaled_holdout_drawdown=first.max_drawdown,
        deterministic_replay_ok=first.trace_signature == second.trace_signature,
        holdout_opened_once=registry.get_artifact(artifact_id).holdout_opened)
    evidence = replace(
        evidence, benchmark_drawdown_ratio=benchmark.ratio.value,
        benchmark_return=benchmark.benchmark_return,
        benchmark_downside_deviation=benchmark.benchmark_downside_deviation,
        benchmark_recovery_time=benchmark.benchmark_recovery_time,
        strategy_time_in_market=time_in_market)
    context.missing_causes.update(_missing_causes({'benchmark_relative_drawdown': benchmark.ratio}))
    context.market_context['benchmark'] = _benchmark_context(benchmark, time_in_market)
    decision = evaluate_eligibility(ruleset, evidence)
    decision_digest = None
    if passed:
        decision_digest = EligibilityDecisionRepository(research_db).record(
            decision, artifact_id=artifact_id, recorded_at=now())
    return HoldoutOutcome(artifact_id, passed, evidence, decision, decision_digest,
                          {'start': holdout_start.isoformat(), 'end': holdout_end.isoformat(),
                           'passed': passed, 'detail': detail})


def _period_session_dates(spec: EvaluationSpec) -> list[dt.date]:
    calendar = xcals.get_calendar(spec.calendar)
    return [s.date() for s in calendar.sessions_in_range(str(spec.period_start),
                                                         str(spec.period_end))]


def _missing_causes(measured: dict) -> dict[str, str]:
    return {code: m.cause for code, m in measured.items() if m.cause}


def _holdout_market_evidence(spec, benchmark_closes: pd.Series, holdout: WindowOutcome,
                             holdout_start: dt.date, holdout_end: dt.date,
                             sessions: Sequence[dt.date]):
    # One close before the holdout, so pct_change yields a return for holdout session 1.
    prior = [d for d in sessions if d < holdout_start]
    window_start = prior[-1] if prior else holdout_start
    spy = benchmark_closes[(benchmark_closes.index >= window_start)
                           & (benchmark_closes.index <= holdout_end)]
    try:
        benchmark = mcx.vol_matched_benchmark(holdout.equity_series(), spy,
                                              account_equity=spec.account_equity)
    except mcx.MarketContextError as exc:
        raise EvaluationError(f'market context unavailable: {exc}') from exc
    time_in_market = mcx.time_in_market(
        build_round_trips(holdout.trades), calendar_name=spec.calendar,
        start=holdout_start, end=holdout_end)
    return benchmark, time_in_market


def _regime_context(regimes: mcx.RegimeEvidence) -> dict:
    total = regimes.table.total_pnl
    return {
        'table': [{'regime': b.key, 'trades': b.n_trades, 'net_pnl': b.pnl, 'share': b.share,
                   'adequate': b.adequate} for b in regimes.table.buckets],
        'total_pnl': total, 'n_changes': regimes.n_changes,
        'transition_group_pnl': regimes.transition_group_pnl,
        'transition_group_trades': regimes.transition_group_trades}


def _liquidity_context(liquidity: mcx.LiquidityEnvelope) -> dict:
    return {
        'capacity_estimate': liquidity.capacity_estimate,
        'rows': [{'conid': r.conid, 'floor_median': r.floor_median,
                  'floor_date': None if r.floor_date is None else r.floor_date.isoformat(),
                  'share': r.share} for r in liquidity.rows]}


def _benchmark_context(benchmark: mcx.BenchmarkEvidence, time_in_market) -> dict:
    return {
        'ratio': benchmark.ratio.value, 'scale': benchmark.scale,
        'benchmark_return': benchmark.benchmark_return,
        'benchmark_downside_deviation': benchmark.benchmark_downside_deviation,
        'benchmark_recovery_time': benchmark.benchmark_recovery_time,
        'raw_spy_return': benchmark.raw_spy_return,
        'raw_spy_drawdown': benchmark.raw_spy_drawdown,
        'strategy_time_in_market': time_in_market}


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
                             'holdout_sessions': spec.holdout_sessions,
                             'benchmark_conid': mcx.BENCHMARK_CONID,
                             'regime_definition': {
                                 'taxonomy_digest': regime_taxonomy_digest(),
                                 'trend_sma_sessions': mcx.TREND_SMA_SESSIONS,
                                 'volatility_sessions': mcx.VOLATILITY_SESSIONS,
                                 'hold_sessions': mcx.REGIME_HOLD_SESSIONS,
                                 'transition_window_sessions': mcx.TRANSITION_WINDOW_SESSIONS,
                                 'min_samples': mcx.REGIME_MIN_SAMPLES,
                                 'labelling': 'entry_session'},
                             'liquidity': {'adv_sessions': mcx.LIQUIDITY_ADV_SESSIONS,
                                           'max_adv_share': mcx.LIQUIDITY_MAX_ADV_SHARE}})


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


def _refuse_if_strategy_holdout_overlaps(registry: ExperimentRegistry, spec: EvaluationSpec,
                                         plan: ValidationPlan) -> None:
    start = pd.Timestamp(plan.holdout.start).date()
    end = pd.Timestamp(plan.holdout.end).date()
    for w in registry.opened_holdout_windows(spec.strategy_path, spec.class_name):
        if start <= w['end'] and w['start'] <= end:
            raise EvaluationError(
                f"strategy {spec.strategy_path} class {spec.class_name} already opened a "
                f"holdout over {w['start']}\u2013{w['end']} (artifact {w['artifact_id']}); "
                f"the next holdout must start after {w['end']}")


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
                              safe_summary=INTERRUPTED_SUMMARY)
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
                           neighbours: Sequence[PointResult], strategy_trials,
                           regimes: mcx.RegimeEvidence,
                           liquidity: mcx.LiquidityEnvelope) -> EligibilityEvidence:
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
        order_within_envelope=liquidity.within.value,
        capacity_estimate=liquidity.capacity_estimate,
        eligible_regime_positive_fraction=regimes.positive_fraction.value,
        worst_eligible_regime_loss=regimes.worst_loss.value,
        regime_transitions_stable=regimes.transitions_stable.value,
    )


def _finish(research_db, spec: EvaluationSpec, paths: EvaluationPaths, now, *, family_id: str,
            stage: str, state: str, artifact_id: Optional[str], decision_digest: Optional[str],
            gate: ev.GateOutcome, evidence: EligibilityEvidence, main: PointResult,
            neighbours: Sequence[PointResult], market_context: dict,
            missing_causes: dict) -> EvaluationResult:
    created_at = now()
    report_path = write_evaluation_report(
        paths.reports_dir, spec=spec, family_id=family_id, stage=stage, state=state,
        artifact_id=artifact_id, failed=gate.failed, missing=gate.missing, evidence=evidence,
        main=main, neighbours=neighbours, created_at=created_at,
        container_digest=_container_digest(), market_context=market_context,
        missing_causes=missing_causes)
    record = EvaluationRecord(
        spec_name=spec.name, family_id=family_id, strategy_path=spec.strategy_path,
        class_name=spec.class_name, stage=stage, state=state, artifact_id=artifact_id,
        decision_digest=decision_digest, failed_rules=gate.failed, missing_rules=gate.missing,
        report_path=str(report_path), created_at=created_at)
    EvaluationRepository(research_db).record(record)
    write_evaluation_summary(paths.summaries_dir, record)
    return EvaluationResult(spec.name, family_id, stage, state, artifact_id, decision_digest,
                            gate.failed, gate.missing, report_path)
