"""P5 Task 6 — factor exposure from return series.

Pure functions for estimating market and sector factor betas via OLS on
aligned daily return series. Used by portfolio admission to detect shared
factor concentration across strategies.
"""
from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

__all__ = [
    "MIN_FACTOR_SAMPLE",
    "compute_factor_exposures",
    "align_return_series",
]


MIN_FACTOR_SAMPLE = 20


def align_return_series(
    strategy_returns: Sequence[float],
    factor_returns: Mapping[str, Sequence[float]],
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Trim all series to the shortest common length (most recent overlap)."""
    n = len(strategy_returns)
    aligned_factors: dict[str, np.ndarray] = {}
    for name, series in factor_returns.items():
        n = min(n, len(series))
    if n == 0:
        return np.array([]), {name: np.array([]) for name in factor_returns}
    y = np.asarray(strategy_returns[-n:], dtype=float)
    for name, series in factor_returns.items():
        aligned_factors[name] = np.asarray(series[-n:], dtype=float)
    return y, aligned_factors


def compute_factor_exposures(
    strategy_returns: Sequence[float],
    factor_returns: Mapping[str, Sequence[float]],
    *,
    min_sample: int = MIN_FACTOR_SAMPLE,
) -> dict[str, float]:
    """OLS betas of strategy returns on factor proxies (market, sector, …).

    Returns an empty dict when overlap is below ``min_sample`` or inputs are
    non-finite / degenerate."""
    y, factors = align_return_series(strategy_returns, factor_returns)
    if len(y) < min_sample:
        return {}

    if not np.all(np.isfinite(y)):
        return {}

    names = sorted(factors.keys())
    if not names:
        return {}

    x_cols = [np.ones(len(y))]
    for name in names:
        col = factors[name]
        if len(col) != len(y) or not np.all(np.isfinite(col)):
            return {}
        x_cols.append(col)

    x = np.column_stack(x_cols)
    if np.linalg.matrix_rank(x) < x.shape[1]:
        return {}

    beta, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
    return {name: float(beta[i + 1]) for i, name in enumerate(names)}
