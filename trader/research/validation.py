"""Leakage-safe validation protocol (P2 Task 5, design §8.2 / §8.3 / §8.4).

Assembles the evidence the ``PAPER_ELIGIBLE`` gate consumes without ever forking
the execution model: cost stress and fold runs go THROUGH ``Backtester.run`` --
this module only varies the ``BacktestConfig`` (dates, slippage, commission) and
reads the ``BacktestResult``.

Pieces:

* ``generate_walk_forward`` -> ``ValidationPlan``: chronological, non-overlapping,
  embargoed folds landed on real XNYS sessions, plus ONE final untouched
  holdout. ``validate_plan`` fails loud on any overlap, misordering, missing
  embargo, or holdout-before-a-fold -- it is what rejects random shuffles and
  fold leakage.
* ``run_window`` / ``cost_stress``: deterministic backtester replays; higher
  cost can never improve net P&L (monotone non-increasing).
* ``benchmark_metrics``: exposure/volatility-matched SPY comparison (drawdown,
  return, downside deviation, recovery, time in market) for BOTH series; ``None``
  benchmark yields explicit ``None`` fields (never assumed).
* ``ValidationResult``: the assembled record with a content ``digest`` so it can
  be frozen into the evidence chain.

Deterministic + offline: no RNG here, no wall-clock, no operational DB access.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from trader.objects import BarSize
from trader.research.attribution import AttributionTable
from trader.research.canonical import sha256_digest
from trader.research.statistics import BootstrapCI
from trader.simulation.backtester import Backtester, BacktestConfig, BacktestResult

VALIDATION_RESULT_PREFIX = "validation_result"


class ValidationError(ValueError):
    """A structurally invalid validation plan (fail loudly)."""


# --------------------------------------------------------------------------- #
# Plan dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    """A closed [start, end] time window (session dates or datetimes)."""

    start: Any
    end: Any


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold. Expanding train, embargoed gap, forward test."""

    index: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any


@dataclass(frozen=True)
class ValidationPlan:
    """A full validation protocol: development window, ordered folds, embargo
    (in sessions), and one final chronological holdout."""

    training: Window
    folds: tuple[Fold, ...]
    embargo: int
    holdout: Window


# --------------------------------------------------------------------------- #
# Walk-forward generation
# --------------------------------------------------------------------------- #
def _resolve_sessions(sessions_or_range: Any, calendar_name: str) -> list[pd.Timestamp]:
    if isinstance(sessions_or_range, tuple) and len(sessions_or_range) == 2:
        import exchange_calendars as xcals

        cal = xcals.get_calendar(calendar_name)
        idx = cal.sessions_in_range(str(sessions_or_range[0]),
                                    str(sessions_or_range[1]))
        return [pd.Timestamp(s) for s in idx]
    return [pd.Timestamp(s) for s in sessions_or_range]


def generate_walk_forward(sessions_or_range: Any, *, n_folds: int, embargo: int,
                          holdout: int, calendar_name: str = "XNYS") -> ValidationPlan:
    """Build a chronological, embargoed walk-forward plan on real sessions.

    ``sessions_or_range`` is either a ``(start, end)`` tuple (resolved to XNYS
    sessions) or an explicit sequence of session dates. ``n_folds`` forward test
    windows partition the pre-holdout pool; each fold's train is expanding (from
    the very first session) and ends ``embargo`` sessions before its test window.
    The last ``holdout`` sessions form a single untouched final block.

    Fails loud (``ValueError``) when there are not enough sessions to honor the
    requested fold/embargo/holdout structure.
    """
    if n_folds < 1:
        raise ValueError(f"n_folds must be >= 1, got {n_folds}")
    if embargo < 0:
        raise ValueError(f"embargo must be >= 0, got {embargo}")
    if holdout < 1:
        raise ValueError(f"holdout must be >= 1, got {holdout}")

    sessions = _resolve_sessions(sessions_or_range, calendar_name)
    total = len(sessions)
    if holdout >= total:
        raise ValueError(f"holdout ({holdout}) consumes all {total} sessions")

    pool = sessions[: total - holdout]
    holdout_sessions = sessions[total - holdout:]
    n_seg = n_folds + 1
    if len(pool) < n_seg:
        raise ValueError(
            f"not enough sessions: {len(pool)} in pool but need >= {n_seg} "
            f"for {n_folds} folds + development segment")
    base = len(pool) // n_seg
    if base < embargo + 1:
        raise ValueError(
            f"not enough sessions: each segment holds {base} sessions but "
            f"embargo={embargo} needs at least {embargo + 1} per segment")

    folds: list[Fold] = []
    for i in range(n_folds):
        test_start_idx = (i + 1) * base
        test_end_idx = (i + 2) * base - 1 if i < n_folds - 1 else len(pool) - 1
        train_end_idx = test_start_idx - embargo - 1
        folds.append(Fold(
            index=i,
            train_start=pool[0],
            train_end=pool[train_end_idx],
            test_start=pool[test_start_idx],
            test_end=pool[test_end_idx]))

    plan = ValidationPlan(
        training=Window(start=pool[0], end=pool[base - 1]),
        folds=tuple(folds),
        embargo=embargo,
        holdout=Window(start=holdout_sessions[0], end=holdout_sessions[-1]))
    validate_plan(plan)  # a generated plan is always structurally valid
    return plan


def validate_plan(plan: ValidationPlan) -> None:
    """Reject any leak or misordering. Raises ``ValidationError`` on:

    * no folds, or fold indices out of order;
    * a train window that ends at/after its own test window (embargo leak);
    * an inverted train/test/holdout window;
    * overlapping or non-advancing test windows;
    * a holdout that is not strictly after every fold.
    """
    folds = plan.folds
    if not folds:
        raise ValidationError("plan has no folds")

    for i, f in enumerate(folds):
        if f.index != i:
            raise ValidationError(
                f"fold at position {i} has index {f.index} (must be chronological)")
        if not (f.train_start <= f.train_end):
            raise ValidationError(f"fold {i}: inverted train window "
                                  f"{f.train_start} > {f.train_end}")
        if not (f.train_end < f.test_start):
            raise ValidationError(
                f"fold {i}: leakage/missing embargo -- train_end {f.train_end} "
                f"is not strictly before test_start {f.test_start}")
        if not (f.test_start <= f.test_end):
            raise ValidationError(f"fold {i}: inverted test window "
                                  f"{f.test_start} > {f.test_end}")

    for a, b in zip(folds, folds[1:]):
        if not (a.test_end < b.test_start):
            raise ValidationError(
                f"folds {a.index}->{b.index}: test windows overlap or do not "
                f"advance ({a.test_end} !< {b.test_start})")
        if not (a.train_end <= b.train_end):
            raise ValidationError(
                f"folds {a.index}->{b.index}: train windows do not expand forward")

    last_test_end = max(f.test_end for f in folds)
    if not (plan.holdout.start > last_test_end):
        raise ValidationError(
            f"holdout starts {plan.holdout.start} which is not strictly after the "
            f"last fold test end {last_test_end}")
    if not (plan.holdout.start <= plan.holdout.end):
        raise ValidationError("inverted holdout window")
    if not (plan.training.start <= plan.training.end):
        raise ValidationError("inverted training window")


# --------------------------------------------------------------------------- #
# Cost model + deterministic backtester adapter
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CostModel:
    """Execution-cost knobs the backtester already understands."""

    slippage_bps: float = 1.0
    commission_per_share: float = 0.005

    def scaled(self, multiplier: float) -> "CostModel":
        return CostModel(slippage_bps=self.slippage_bps * multiplier,
                         commission_per_share=self.commission_per_share * multiplier)


def _as_datetime(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def run_window(strategy_factory: Callable[[], Any], conids: Sequence[int], *,
               storage: Any, window: Window, cost: CostModel,
               bar_size: BarSize = BarSize.Mins1,
               initial_capital: float = 100_000.0,
               risk_limits: Any = None) -> BacktestResult:
    """Deterministically replay one window at a given cost THROUGH the backtester.

    Builds a ``BacktestConfig`` with explicit dates + the supplied cost and calls
    ``Backtester.run`` -- no RNG, no forked fill/commission math. ``strategy_factory``
    returns a fresh, installed, ``RUNNING`` strategy for each call so repeat runs
    do not share mutable state. Two calls with identical inputs yield identical
    ``trace_signature`` results.
    """
    config = BacktestConfig(
        start_date=_as_datetime(window.start),
        end_date=_as_datetime(window.end),
        initial_capital=initial_capital,
        bar_size=bar_size,
        slippage_bps=cost.slippage_bps,
        commission_per_share=cost.commission_per_share)
    backtester = Backtester(storage=storage, config=config, risk_limits=risk_limits)
    strategy = strategy_factory()
    return backtester.run(strategy, list(conids))


def _net_pnl(result: BacktestResult) -> float:
    eq = result.equity_curve
    if eq is None or len(eq) < 1:
        return 0.0
    return float(eq.iloc[-1] - eq.iloc[0])


def cost_stress(run_fn: Callable[[float], BacktestResult], *,
                multipliers: Sequence[float] = (1.0, 1.5, 2.0)) -> dict[float, float]:
    """Run the SAME strategy/window at scaled slippage+commission.

    ``run_fn(multiplier)`` returns a ``BacktestResult``; the return maps each
    multiplier to net P&L (final equity - starting equity). Because costs only
    subtract, net P&L is monotonically NON-INCREASING as the multiplier rises --
    a property callers (and the eligibility gate) rely on.
    """
    return {float(m): _net_pnl(run_fn(m)) for m in multipliers}


# --------------------------------------------------------------------------- #
# Benchmark comparison
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SeriesMetrics:
    total_return: float
    max_drawdown: float          # <= 0
    downside_deviation: float    # annualized
    recovery_time: int           # longest underwater span, in periods
    time_in_market: Optional[float]


@dataclass(frozen=True)
class BenchmarkComparison:
    strategy: SeriesMetrics
    benchmark: Optional[SeriesMetrics]
    drawdown_ratio: Optional[float]  # |strategy_dd| / |benchmark_dd|


def _longest_underwater(eq: np.ndarray, running_max: np.ndarray) -> int:
    longest = cur = 0
    for e, p in zip(eq, running_max):
        if e < p:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    return longest


def _series_metrics(equity: Any, *, periods_per_year: float,
                    time_in_market: Optional[float]) -> SeriesMetrics:
    eq = np.asarray(getattr(equity, "values", equity), dtype=float)
    eq = eq[np.isfinite(eq)]
    if len(eq) < 2 or eq[0] == 0.0:
        return SeriesMetrics(0.0, 0.0, 0.0, 0, time_in_market)
    total_return = float(eq[-1] / eq[0] - 1.0)
    running_max = np.maximum.accumulate(eq)
    dd = (eq - running_max) / running_max
    max_drawdown = float(dd.min())
    rets = np.diff(eq) / eq[:-1]
    downside = np.minimum(rets, 0.0)
    downside_dev = float(np.sqrt(np.mean(downside ** 2)) * math.sqrt(periods_per_year))
    recovery_time = _longest_underwater(eq, running_max)
    return SeriesMetrics(total_return, max_drawdown, downside_dev, recovery_time,
                         time_in_market)


def benchmark_metrics(strategy_equity: Any, benchmark_equity: Optional[Any], *,
                      periods_per_year: float,
                      strategy_time_in_market: Optional[float] = None,
                      benchmark_time_in_market: Optional[float] = 1.0
                      ) -> BenchmarkComparison:
    """Exposure/volatility-matched SPY-style comparison.

    Reports drawdown, return, downside deviation, recovery time, and time in
    market for BOTH series so trivially-low exposure cannot game the benchmark
    (§8.4). When ``benchmark_equity`` is ``None`` the benchmark fields and the
    drawdown ratio are explicitly ``None`` -- never assumed.
    """
    strat = _series_metrics(strategy_equity, periods_per_year=periods_per_year,
                            time_in_market=strategy_time_in_market)
    if benchmark_equity is None:
        return BenchmarkComparison(strategy=strat, benchmark=None, drawdown_ratio=None)
    bench = _series_metrics(benchmark_equity, periods_per_year=periods_per_year,
                            time_in_market=benchmark_time_in_market)
    ratio: Optional[float] = None
    if bench.max_drawdown != 0.0:
        ratio = abs(strat.max_drawdown) / abs(bench.max_drawdown)
    return BenchmarkComparison(strategy=strat, benchmark=bench, drawdown_ratio=ratio)


# --------------------------------------------------------------------------- #
# Assembled result + digest
# --------------------------------------------------------------------------- #
def _num(x: Any) -> Optional[float]:
    """Finite float, or ``None`` -- never let a non-finite value into the digest."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ci_body(ci: Optional[BootstrapCI]) -> Optional[dict]:
    if ci is None:
        return None
    return {"low": _num(ci.low), "point": _num(ci.point), "high": _num(ci.high)}


def _attr_body(table: Optional[AttributionTable]) -> Optional[dict]:
    if table is None:
        return None
    return {
        "by": table.by,
        "min_samples": table.min_samples,
        "total_pnl": _num(table.total_pnl),
        "buckets": [
            {"key": str(b.key), "pnl": _num(b.pnl), "n_trades": b.n_trades,
             "share": _num(b.share), "adequate": b.adequate}
            for b in table.buckets
        ],
    }


def _series_body(sm: Optional[SeriesMetrics]) -> Optional[dict]:
    if sm is None:
        return None
    return {
        "total_return": _num(sm.total_return),
        "max_drawdown": _num(sm.max_drawdown),
        "downside_deviation": _num(sm.downside_deviation),
        "recovery_time": sm.recovery_time,
        "time_in_market": _num(sm.time_in_market),
    }


@dataclass(frozen=True)
class ValidationResult:
    """The assembled validation record for one artifact candidate.

    Everything the ``PAPER_ELIGIBLE`` gate reads: cost-stressed net P&L, the
    bootstrap interval on mean P&L, the annualized-Sharpe interval, the deflated
    & selection-adjusted Sharpe, per-fold results, month/instrument/regime
    attribution, the benchmark comparison, a capacity estimate, and the
    deterministic-replay ``trace_signature``. ``digest`` freezes it into the
    evidence chain.
    """

    trace_signature: str
    cost_stress: Mapping[float, float]
    mean_pnl_ci: Optional[BootstrapCI]
    sharpe_ci: Optional[BootstrapCI]
    deflated_sharpe: Optional[float]
    selection_adjusted_confidence: Optional[float]
    profit_factor: float
    n_round_trips: int
    n_instruments: int
    fold_net_pnls: tuple[float, ...]
    month_attribution: Optional[AttributionTable]
    instrument_attribution: Optional[AttributionTable]
    regime_attribution: Optional[AttributionTable]
    benchmark: Optional[BenchmarkComparison]
    capacity_estimate: Optional[float] = None

    def _digest_body(self) -> dict:
        return {
            "trace_signature": self.trace_signature,
            # sort keys deterministically; profit_factor inf -> None (not digestable)
            "cost_stress": {str(float(k)): _num(v)
                            for k, v in sorted(self.cost_stress.items())},
            "mean_pnl_ci": _ci_body(self.mean_pnl_ci),
            "sharpe_ci": _ci_body(self.sharpe_ci),
            "deflated_sharpe": _num(self.deflated_sharpe),
            "selection_adjusted_confidence": _num(self.selection_adjusted_confidence),
            "profit_factor": _num(self.profit_factor),
            "n_round_trips": self.n_round_trips,
            "n_instruments": self.n_instruments,
            "fold_net_pnls": [_num(p) for p in self.fold_net_pnls],
            "month_attribution": _attr_body(self.month_attribution),
            "instrument_attribution": _attr_body(self.instrument_attribution),
            "regime_attribution": _attr_body(self.regime_attribution),
            "benchmark": None if self.benchmark is None else {
                "strategy": _series_body(self.benchmark.strategy),
                "benchmark": _series_body(self.benchmark.benchmark),
                "drawdown_ratio": _num(self.benchmark.drawdown_ratio),
            },
            "capacity_estimate": _num(self.capacity_estimate),
        }

    @property
    def digest(self) -> str:
        return sha256_digest(VALIDATION_RESULT_PREFIX, self._digest_body())
