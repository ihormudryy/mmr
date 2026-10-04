"""Pure evidence maths for `research evaluate` (spec section 3).

Every function takes plain values and returns a number, a bool, or ``None``
when the evidence cannot be computed. ``None`` fails its paper-v1 rule closed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from trader.research.attribution import RoundTrip
from trader.research.rulesets.paper_v1 import CANARY_DRAWDOWN_LIMIT
from trader.research.statistics import profit_concentration, selection_adjusted_confidence

HOLDOUT_STAGE_RULES = frozenset({
    'holdout_drawdown_within_canary',
    'deterministic_replay',
    'holdout_opened_once',
    'benchmark_relative_drawdown',
})
NEIGHBOURHOOD_MIN_POSITIVE_SHARE = 2 / 3
TRADING_DAYS_PER_YEAR = 252


def dollar_weighted_expectancy_bps(round_trips: Sequence[RoundTrip]) -> Optional[float]:
    notional = sum(rt.quantity * rt.entry_price for rt in round_trips)
    if notional <= 0:
        return None
    return sum(rt.pnl for rt in round_trips) / notional * 10_000


def session_returns(equity_curves: Sequence[pd.Series], *, starting_equity: float) -> np.ndarray:
    """Close-to-close returns per session, each fold starting from ``starting_equity``."""
    parts = []
    for curve in equity_curves:
        if curve is None or len(curve) == 0:
            continue
        closes = curve.groupby(curve.index.date).last().to_numpy(dtype=float)
        levels = np.concatenate([[starting_equity], closes])
        parts.append(np.diff(levels) / levels[:-1])
    return np.concatenate(parts) if parts else np.array([])


def _finite(values) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def per_period_sharpe(returns) -> Optional[float]:
    r = _finite(returns)
    if len(r) < 2:
        return None
    sd = r.std(ddof=1)
    return 0.0 if sd == 0 else float(r.mean() / sd)


def positive_fold_fraction(fold_net_pnls: Sequence[float]) -> Optional[float]:
    if not fold_net_pnls:
        return None
    return sum(1 for pnl in fold_net_pnls if pnl > 0) / len(fold_net_pnls)


def _concentration(round_trips: Sequence[RoundTrip], bucket) -> Optional[float]:
    pnl_by_bucket: dict = {}
    for rt in round_trips:
        key = bucket(rt)
        pnl_by_bucket[key] = pnl_by_bucket.get(key, 0.0) + rt.pnl
    return profit_concentration(pnl_by_bucket).max_share


def month_concentration(round_trips: Sequence[RoundTrip]) -> Optional[float]:
    return _concentration(round_trips, lambda rt: f'{rt.close_time:%Y-%m}')


def instrument_concentration(round_trips: Sequence[RoundTrip]) -> Optional[float]:
    return _concentration(round_trips, lambda rt: int(rt.conid))


def neighbourhood_robust(neighbour_expectancies: Sequence[Optional[float]]) -> Optional[bool]:
    if len(neighbour_expectancies) < 2:
        return None
    positive = sum(1 for e in neighbour_expectancies if e is not None and e > 0)
    return positive / len(neighbour_expectancies) >= NEIGHBOURHOOD_MIN_POSITIVE_SHARE


def combined_trace_signature(signatures: Sequence[str]) -> str:
    payload = json.dumps(list(signatures), separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def selection_confidence(returns, *, n_trials: int,
                         trial_sharpes: Sequence[float]) -> Optional[float]:
    r = _finite(returns)
    sharpe = per_period_sharpe(r)
    spread = _finite(trial_sharpes)
    if sharpe is None or len(spread) < 2 or n_trials < 1:
        return None
    trial_sharpe_std = float(spread.std(ddof=1))
    if not np.isfinite(trial_sharpe_std) or trial_sharpe_std == 0:
        return None
    return selection_adjusted_confidence(
        sharpe, n_trials=n_trials, trial_sharpe_std=trial_sharpe_std,
        n_obs=len(r), skew=float(scipy_stats.skew(r)),
        excess_kurtosis=float(scipy_stats.kurtosis(r)))


def holdout_passes(round_trips: Sequence[RoundTrip], max_drawdown: float) -> bool:
    expectancy = dollar_weighted_expectancy_bps(round_trips)
    return (bool(round_trips) and expectancy is not None and expectancy > 0
            and abs(max_drawdown) <= CANARY_DRAWDOWN_LIMIT)


@dataclass(frozen=True)
class GateOutcome:
    passed: bool
    failed: tuple[str, ...]
    missing: tuple[str, ...]


def _outcome(decision, *, skip: frozenset) -> GateOutcome:
    failed, missing = [], []
    for result in decision.results:
        if result.code in skip or result.passed:
            continue
        (missing if result.observed is None else failed).append(result.code)
    return GateOutcome(passed=not failed and not missing, failed=tuple(failed), missing=tuple(missing))


def pre_holdout_outcome(decision) -> GateOutcome:
    """Every rule except the holdout-stage ones must pass before the holdout opens."""
    return _outcome(decision, skip=HOLDOUT_STAGE_RULES)


def final_outcome(decision) -> GateOutcome:
    return _outcome(decision, skip=frozenset())
