"""Selection-bias-aware statistics for the research evidence chain (P2 Task 5).

This module supplies exactly the quantities the ``PAPER_ELIGIBLE`` gate (design
§8.3) measures: bootstrap intervals on mean P&L and annualized Sharpe, the
deflated / selection-adjusted Sharpe confidence, profit factor, and the
concentration / outlier diagnostics. It is a thin, deterministic layer BUILT ON
``trader.simulation.backtest_stats`` -- it never re-implements PSR or the
bootstrap from scratch.

Design invariants (this is a trading system -- wrong evidence is worse than
none):

* **Deterministic.** Every resample takes a REQUIRED integer ``seed`` and uses
  ``numpy.random.default_rng`` (via ``backtest_stats.bootstrap_ci``). Identical
  inputs + seed -> identical outputs, byte for byte.
* **Fail closed.** Below a documented minimum sample every estimator returns
  ``None`` (matching ``backtest_stats``' convention) -- never a fabricated
  positive.
* **Offline + pure.** No I/O, no wall-clock, no operational imports.

Kurtosis convention: ``excess_kurtosis`` throughout is FISHER excess kurtosis
(normal == 0), matching ``backtest_stats.compute_all``'s ``pnl_excess_kurtosis``
(``scipy.stats.kurtosis(fisher=True)``). The deflated-Sharpe denominator uses the
non-excess form ``(gamma4 - 1)/4 == (excess_kurtosis + 2)/4``, identical to
``backtest_stats.probabilistic_sharpe`` -- see ``deflated_sharpe_ratio``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from trader.simulation.backtest_stats import bootstrap_ci

# Euler-Mascheroni constant -- the deflation benchmark's second-order term.
EULER_MASCHERONI = 0.5772156649015329

# Minimum samples. Mirror backtest_stats: bootstrap needs n >= 10, PSR/DSR
# need n >= 3 (higher moments undefined below that).
_BOOTSTRAP_MIN = 10
_DSR_MIN_OBS = 3


@dataclass(frozen=True)
class BootstrapCI:
    """A seeded percentile-bootstrap interval: ``low <= point <= high``.

    ``point`` is the statistic on the observed sample (not a bootstrap mean),
    so it is exact and deterministic. The eligibility gate reads ``low`` (the
    lower bound) for the "above zero" tests.
    """

    low: float
    point: float
    high: float


@dataclass(frozen=True)
class ConcentrationResult:
    """Largest positive-profit bucket's share of total positive profit."""

    max_share: Optional[float]
    dominant_bucket: Any


@dataclass(frozen=True)
class OutlierDiagnostic:
    """Total P&L with and without the single largest winner (episode dominance)."""

    total: float
    total_without_top: float
    top_share: Optional[float]


def _clean(values: Sequence[float]) -> np.ndarray:
    v = np.asarray(list(values), dtype=float)
    return v[np.isfinite(v)]


def profit_factor(pnls: Sequence[float]) -> float:
    """Gross wins / |gross losses|.

    Returns ``float('inf')`` when there are winners but no losers (all-winning
    series -- rare but real). Callers MUST NOT feed an ``inf`` result into a
    content digest (``canonical_json_bytes`` rejects non-finite floats); treat
    ``inf`` as "no losing trades" before serializing. Returns ``0.0`` when there
    is neither a win nor a loss (empty / all-zero series).
    """
    v = _clean(pnls)
    gross_wins = float(v[v > 0].sum())
    gross_losses = float(v[v < 0].sum())
    if gross_losses < 0:
        return gross_wins / abs(gross_losses)
    if gross_wins > 0:
        return float("inf")
    return 0.0


def bootstrap_mean_ci(pnls: Sequence[float], *, confidence: float = 0.95,
                      n_resamples: int = 2000, seed: int) -> Optional[BootstrapCI]:
    """Seeded percentile bootstrap CI on the MEAN per-trade P&L.

    Returns ``None`` when ``len(pnls) < 10`` (bootstrap on a tiny sample is
    misleading -- the resamples are nearly the original). ``point`` is the
    plain sample mean.
    """
    v = _clean(pnls)
    if len(v) < _BOOTSTRAP_MIN:
        return None
    alpha = 1.0 - confidence
    ci = bootstrap_ci(v, lambda s: np.mean(s, axis=-1),
                      n_boot=n_resamples, alpha=alpha, seed=seed)
    if ci is None:
        return None
    return BootstrapCI(low=ci[0], point=float(v.mean()), high=ci[1])


def annualized_sharpe(returns: Sequence[float], *, periods_per_year: float) -> float:
    """Annualized Sharpe: ``mean / std(ddof=1) * sqrt(periods_per_year)``.

    Returns ``0.0`` when the sample has fewer than two finite points or zero
    variance (no information -- fail to the neutral value, never fabricate).
    """
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    if sd == 0.0:
        return 0.0
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def annualized_sharpe_ci(returns: Sequence[float], *, periods_per_year: float,
                         confidence: float = 0.95, n_resamples: int = 2000,
                         seed: int) -> Optional[BootstrapCI]:
    """Seeded bootstrap CI on the ANNUALIZED Sharpe of a return series.

    The eligibility gate reads ``.low`` ("annualized Sharpe bootstrap lower
    bound above zero"). Returns ``None`` when ``len(returns) < 10``.
    """
    r = _clean(returns)
    if len(r) < _BOOTSTRAP_MIN:
        return None
    ann = math.sqrt(periods_per_year)

    def _sharpe_vec(s: np.ndarray) -> np.ndarray:
        mu = s.mean(axis=-1)
        sd = s.std(axis=-1, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            sr = np.where(sd > 0, mu / sd, 0.0) * ann
        return sr

    alpha = 1.0 - confidence
    ci = bootstrap_ci(r, _sharpe_vec, n_boot=n_resamples, alpha=alpha, seed=seed)
    if ci is None:
        return None
    point = annualized_sharpe(r, periods_per_year=periods_per_year)
    return BootstrapCI(low=ci[0], point=point, high=ci[1])


def expected_max_sharpe_null(n_trials: int, trial_sharpe_std: float) -> float:
    """Bailey & Lopez de Prado (2014) expected-maximum Sharpe under the null.

    ``SR0 = trial_sharpe_std * ((1-gamma)*Phi^-1(1 - 1/N)
                                + gamma*Phi^-1(1 - 1/(N*e)))``

    with ``gamma`` = Euler-Mascheroni and ``Phi^-1`` = ``norm.ppf``. This is the
    deflation benchmark: the Sharpe you would expect to see from the BEST of ``N``
    independent random trials. ``N`` is Task-4's selection denominator
    (``ExperimentRegistry.selection_trial_count``). ``N == 1`` -> ``0.0`` (no
    selection, no inflation to deflate). Monotonically increasing in ``N``.
    """
    n = int(n_trials)
    if n < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials!r}")
    if n == 1:
        return 0.0
    from scipy.stats import norm

    gamma = EULER_MASCHERONI
    term = ((1.0 - gamma) * norm.ppf(1.0 - 1.0 / n)
            + gamma * norm.ppf(1.0 - 1.0 / (n * math.e)))
    return float(trial_sharpe_std * term)


def deflated_sharpe_ratio(observed_sharpe: float, *, sr0: float, n_obs: int,
                          skew: float = 0.0, excess_kurtosis: float = 0.0
                          ) -> Optional[float]:
    """Deflated Sharpe probability (Bailey & Lopez de Prado 2014).

    Implemented as the Probabilistic Sharpe Ratio against the deflated
    benchmark ``sr0`` (rather than 0):

        DSR = Phi[ (SR - SR0) * sqrt(n_obs - 1)
                   / sqrt(1 - skew*SR + ((excess_kurtosis + 2)/4)*SR^2) ]

    The denominator's kurtosis term ``(excess_kurtosis + 2)/4`` equals the
    standard ``(gamma4 - 1)/4`` with non-excess kurtosis ``gamma4 = excess + 3``
    -- IDENTICAL to ``backtest_stats.probabilistic_sharpe`` (which uses
    ``(gamma4 - 1)/4`` with ``gamma4`` non-excess). ``excess_kurtosis`` is Fisher
    excess (normal == 0); a normal series therefore contributes ``2/4 == 0.5``.

    ``observed_sharpe`` and ``sr0`` are PER-OBSERVATION Sharpe ratios (not
    annualized). Returns ``None`` when ``n_obs < 3`` (higher moments undefined).
    """
    if n_obs < _DSR_MIN_OBS:
        return None
    from scipy.stats import norm

    sr = float(observed_sharpe)
    kurt_term = (float(excess_kurtosis) + 2.0) / 4.0  # == (gamma4 - 1)/4
    var_term = max(1e-12, 1.0 - float(skew) * sr + kurt_term * sr * sr)
    z = (sr - float(sr0)) * math.sqrt(n_obs - 1) / math.sqrt(var_term)
    return float(norm.cdf(z))


def selection_adjusted_confidence(observed_sharpe: float, *, n_trials: int,
                                  trial_sharpe_std: float, n_obs: int,
                                  skew: float = 0.0, excess_kurtosis: float = 0.0
                                  ) -> Optional[float]:
    """Deflated Sharpe confidence adjusted for the whole selection family.

    Convenience wrapper: deflates against ``expected_max_sharpe_null(n_trials,
    trial_sharpe_std)``. The gate requires ``>= 0.95``. Because ``SR0`` grows
    with ``n_trials``, MORE trials -> LOWER confidence, all else equal (the core
    anti-selection-bias property). Returns ``None`` below the DSR minimum.
    """
    sr0 = expected_max_sharpe_null(n_trials, trial_sharpe_std)
    return deflated_sharpe_ratio(observed_sharpe, sr0=sr0, n_obs=n_obs,
                                 skew=skew, excess_kurtosis=excess_kurtosis)


def profit_concentration(pnl_by_bucket: Mapping[Any, float]) -> ConcentrationResult:
    """Largest positive bucket's share of total POSITIVE profit.

    Denominator is the sum of ONLY the positive buckets (loss buckets do not
    dilute the share) -- this is what the §8.3 month<=35% / instrument<=40%
    gates measure. ``max_share`` is ``None`` when no bucket is net-positive.
    """
    positives = {k: float(v) for k, v in pnl_by_bucket.items() if float(v) > 0.0}
    total_pos = sum(positives.values())
    if not positives or total_pos <= 0.0:
        return ConcentrationResult(max_share=None, dominant_bucket=None)
    dominant = max(positives, key=lambda k: positives[k])
    return ConcentrationResult(max_share=positives[dominant] / total_pos,
                               dominant_bucket=dominant)


def remove_top_outlier_diagnostic(pnls: Sequence[float]) -> OutlierDiagnostic:
    """Drop the single largest winner; report how much of total P&L it supplied.

    ``top_share = (total - total_without_top) / total`` (== top/total) when
    ``total > 0``; otherwise ``None`` (a non-positive total makes the "share of
    profit" undefined). Detects a book propped up by one implausible episode.
    """
    v = _clean(pnls)
    if len(v) == 0:
        return OutlierDiagnostic(total=0.0, total_without_top=0.0, top_share=None)
    total = float(v.sum())
    top = float(v.max())
    total_without_top = total - top
    top_share = (total - total_without_top) / total if total > 0.0 else None
    return OutlierDiagnostic(total=total, total_without_top=total_without_top,
                             top_share=top_share)
