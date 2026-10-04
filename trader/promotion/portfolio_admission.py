"""P5 Task 6 — second-strategy portfolio admission gate.

Deterministic analysis of combined portfolio risk: requires the first strategy
at Scale 2, the second on its own paper/canary path, sufficient covariance
history, and combined stressed daily loss within the unchanged 0.50% account
daily-loss ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from trader.promotion.allocation_attestation import STAGE_SCALE_2, STAGE_STEADY
from trader.promotion.factor_exposure import MIN_FACTOR_SAMPLE, compute_factor_exposures
from trader.promotion.stage import CANARY_PASSED, CANARY_ACTIVE, PAPER_PASSED

MIN_COVARIANCE_SAMPLE = MIN_FACTOR_SAMPLE
STRESS_MULTIPLIER = 2.33  # ~99th percentile daily move
MAX_SHARED_FACTOR_BETA = 1.5
METHODOLOGY_VERSION = "p5_portfolio_admission_v1"

BLOCK_FIRST_STRATEGY_STAGE = "first_strategy_not_at_scale_2"
BLOCK_SECOND_STRATEGY_PATH = "second_strategy_missing_paper_canary_path"
BLOCK_INSUFFICIENT_SAMPLE = "insufficient_covariance_sample"
BLOCK_NON_FINITE_INPUT = "non_finite_covariance_input"
BLOCK_SINGULAR_COVARIANCE = "singular_covariance_matrix"
BLOCK_SHARED_FACTOR_CONCENTRATION = "shared_factor_concentration"
BLOCK_COMBINED_DAILY_LOSS = "combined_stressed_daily_loss_breach"

_ELIGIBLE_SECOND_PROMOTION_STAGES = frozenset({
    PAPER_PASSED, CANARY_ACTIVE, CANARY_PASSED,
})
_ELIGIBLE_FIRST_ALLOCATION_STAGES = frozenset({STAGE_SCALE_2, STAGE_STEADY})

__all__ = [
    "MIN_COVARIANCE_SAMPLE",
    "STRESS_MULTIPLIER",
    "METHODOLOGY_VERSION",
    "BLOCK_FIRST_STRATEGY_STAGE",
    "BLOCK_SECOND_STRATEGY_PATH",
    "BLOCK_INSUFFICIENT_SAMPLE",
    "BLOCK_NON_FINITE_INPUT",
    "BLOCK_SINGULAR_COVARIANCE",
    "BLOCK_SHARED_FACTOR_CONCENTRATION",
    "BLOCK_COMBINED_DAILY_LOSS",
    "PortfolioAdmissionDecision",
    "PortfolioAdmissionGate",
    "compute_combined_stressed_daily_loss",
]


def compute_combined_stressed_daily_loss(
    signed_allocations: Sequence[float],
    covariance_matrix: np.ndarray,
    *,
    stress_multiplier: float = STRESS_MULTIPLIER,
) -> float:
    """Portfolio stressed daily loss as stress_multiplier × σ_p."""
    w = np.asarray(signed_allocations, dtype=float)
    cov = np.asarray(covariance_matrix, dtype=float)
    if w.ndim != 1 or cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
        raise ValueError("signed_allocations must be 1-D and covariance square")
    if len(w) != cov.shape[0]:
        raise ValueError("allocation length must match covariance dimension")
    variance = float(w @ cov @ w)
    if variance < 0:
        variance = 0.0
    return stress_multiplier * float(np.sqrt(variance))


@dataclass(frozen=True)
class PortfolioAdmissionDecision:
    passed: bool
    failures: tuple[str, ...]
    covariance_window: int
    stress_windows: tuple[str, ...]
    factor_exposures: Mapping[str, Mapping[str, float]]
    combined_loss: Optional[float]
    evidence_refs: tuple[str, ...]
    daily_loss_limit: float
    methodology_version: str = METHODOLOGY_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "covariance_window": self.covariance_window,
            "stress_windows": list(self.stress_windows),
            "factor_exposures": {
                k: dict(v) for k, v in self.factor_exposures.items()
            },
            "combined_loss": self.combined_loss,
            "evidence_refs": list(self.evidence_refs),
            "daily_loss_limit": self.daily_loss_limit,
            "methodology_version": self.methodology_version,
        }


class PortfolioAdmissionGate:
    """Evaluates whether a second strategy may join the live portfolio."""

    def __init__(
        self,
        *,
        min_covariance_sample: int = MIN_COVARIANCE_SAMPLE,
        stress_multiplier: float = STRESS_MULTIPLIER,
        max_shared_factor_beta: float = MAX_SHARED_FACTOR_BETA,
        daily_loss_limit: float = 0.005,
    ):
        self._min_covariance_sample = min_covariance_sample
        self._stress_multiplier = stress_multiplier
        self._max_shared_factor_beta = max_shared_factor_beta
        self._daily_loss_limit = daily_loss_limit

    def evaluate(
        self,
        first_strategy_stage: str,
        second_strategy_stage: str,
        covariance_matrix: np.ndarray,
        factor_exposures: Mapping[str, Mapping[str, float]],
        signed_allocations: Sequence[float],
        *,
        strategy_returns: Optional[Mapping[str, Sequence[float]]] = None,
        factor_returns: Optional[Mapping[str, Sequence[float]]] = None,
        daily_loss_limit: Optional[float] = None,
        evidence_refs: Optional[Sequence[str]] = None,
        covariance_window: Optional[int] = None,
    ) -> PortfolioAdmissionDecision:
        limit = self._daily_loss_limit if daily_loss_limit is None else daily_loss_limit
        failures: list[str] = []
        exposures = {k: dict(v) for k, v in factor_exposures.items()}

        if first_strategy_stage not in _ELIGIBLE_FIRST_ALLOCATION_STAGES:
            failures.append(BLOCK_FIRST_STRATEGY_STAGE)
        if second_strategy_stage not in _ELIGIBLE_SECOND_PROMOTION_STAGES:
            failures.append(BLOCK_SECOND_STRATEGY_PATH)

        cov = np.asarray(covariance_matrix, dtype=float)
        n = cov.shape[0] if cov.ndim == 2 else 0

        window_size = covariance_window
        if window_size is None and strategy_returns:
            window_size = min(len(v) for v in strategy_returns.values())
        if window_size is None:
            window_size = n

        if window_size < self._min_covariance_sample:
            failures.append(BLOCK_INSUFFICIENT_SAMPLE)
        if not np.all(np.isfinite(cov)):
            failures.append(BLOCK_NON_FINITE_INPUT)
        elif n >= 2 and np.linalg.matrix_rank(cov) < n:
            failures.append(BLOCK_SINGULAR_COVARIANCE)

        if strategy_returns and factor_returns:
            for strategy_id, returns in strategy_returns.items():
                computed = compute_factor_exposures(returns, factor_returns)
                if computed:
                    exposures[strategy_id] = computed

        self._check_shared_factor_concentration(exposures, failures)

        combined_loss: Optional[float] = None
        w = np.asarray(signed_allocations, dtype=float)
        if (
            not failures
            and len(w) == n
            and np.all(np.isfinite(w))
            and n >= 2
        ):
            combined_loss = compute_combined_stressed_daily_loss(
                w, cov, stress_multiplier=self._stress_multiplier,
            )
            if combined_loss > limit:
                failures.append(BLOCK_COMBINED_DAILY_LOSS)

        failures = list(dict.fromkeys(failures))
        return PortfolioAdmissionDecision(
            passed=not failures,
            failures=tuple(failures),
            covariance_window=window_size,
            stress_windows=("daily_covariance_stress",),
            factor_exposures=exposures,
            combined_loss=combined_loss,
            evidence_refs=tuple(evidence_refs or ()),
            daily_loss_limit=limit,
        )

    def _check_shared_factor_concentration(
        self,
        exposures: Mapping[str, Mapping[str, float]],
        failures: list[str],
    ) -> None:
        if len(exposures) < 2:
            return
        strategies = sorted(exposures.keys())
        shared_factors = set(exposures[strategies[0]].keys())
        for sid in strategies[1:]:
            shared_factors &= set(exposures[sid].keys())
        for factor in shared_factors:
            total_beta = sum(abs(exposures[s][factor]) for s in strategies)
            if total_beta > self._max_shared_factor_beta:
                failures.append(BLOCK_SHARED_FACTOR_CONCENTRATION)
                return
