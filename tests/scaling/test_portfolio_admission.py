"""P5 Task 6 — second-strategy portfolio admission."""
from __future__ import annotations

import numpy as np
import pytest

from trader.promotion.allocation_attestation import STAGE_SCALE_1, STAGE_SCALE_2
from trader.promotion.factor_exposure import MIN_FACTOR_SAMPLE, compute_factor_exposures
from trader.promotion.portfolio_admission import (
    BLOCK_COMBINED_DAILY_LOSS,
    BLOCK_FIRST_STRATEGY_STAGE,
    BLOCK_INSUFFICIENT_SAMPLE,
    BLOCK_SECOND_STRATEGY_PATH,
    BLOCK_SHARED_FACTOR_CONCENTRATION,
    BLOCK_SINGULAR_COVARIANCE,
    PortfolioAdmissionGate,
    compute_combined_stressed_daily_loss,
)
from trader.promotion.stage import CANARY_PASSED, PAPER_COLLECTING, PAPER_PASSED

N = MIN_FACTOR_SAMPLE


def _covariance(vol_a: float = 0.01, vol_b: float = 0.01, rho: float = 0.3) -> np.ndarray:
    return np.array([
        [vol_a ** 2, rho * vol_a * vol_b],
        [rho * vol_a * vol_b, vol_b ** 2],
    ])


def _large_covariance(n: int = N) -> np.ndarray:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(n, 2))
    return np.cov(x, rowvar=False)


def test_factor_exposures_ols_recovers_market_beta():
    rng = np.random.default_rng(7)
    market = rng.normal(0, 0.01, size=40)
    noise = rng.normal(0, 0.002, size=40)
    strategy = 1.2 * market + noise
    exposures = compute_factor_exposures(strategy, {"market": market})
    assert exposures["market"] == pytest.approx(1.2, abs=0.15)


def test_factor_exposures_returns_empty_on_short_sample():
    assert compute_factor_exposures([0.01, -0.01], {"market": [0.01, -0.01]}) == {}


def test_combined_stressed_daily_loss_scales_with_allocations():
    cov = _covariance()
    low = compute_combined_stressed_daily_loss([0.05, 0.04], cov)
    high = compute_combined_stressed_daily_loss([0.10, 0.08], cov)
    assert high > low


def test_admission_passes_for_eligible_uncorrelated_portfolio():
    # Low-vol, modestly correlated 2-strategy covariance fixture.
    cov = np.array([[0.0001, 0.00001], [0.00001, 0.0001]])
    decision = PortfolioAdmissionGate().evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=CANARY_PASSED,
        covariance_matrix=cov,
        factor_exposures={
            "strategy_a": {"market": 0.4, "sector_tech": 0.2},
            "strategy_b": {"market": 0.3, "sector_fin": 0.1},
        },
        signed_allocations=[0.05, 0.04],
        evidence_refs=("cov-window-1",),
        covariance_window=N,
    )
    assert decision.passed is True
    assert decision.combined_loss is not None
    assert decision.combined_loss <= decision.daily_loss_limit


def test_admission_rejects_first_strategy_not_at_scale_2():
    decision = PortfolioAdmissionGate().evaluate(
        first_strategy_stage=STAGE_SCALE_1,
        second_strategy_stage=PAPER_PASSED,
        covariance_matrix=_large_covariance(),
        factor_exposures={"a": {"market": 0.2}, "b": {"market": 0.1}},
        signed_allocations=[0.05, 0.04],
        covariance_window=N,
    )
    assert decision.passed is False
    assert BLOCK_FIRST_STRATEGY_STAGE in decision.failures


def test_admission_rejects_second_strategy_without_paper_canary_path():
    decision = PortfolioAdmissionGate().evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=PAPER_COLLECTING,
        covariance_matrix=_large_covariance(),
        factor_exposures={"a": {"market": 0.2}, "b": {"market": 0.1}},
        signed_allocations=[0.05, 0.04],
        covariance_window=N,
    )
    assert decision.passed is False
    assert BLOCK_SECOND_STRATEGY_PATH in decision.failures


def test_admission_rejects_insufficient_covariance_sample():
    decision = PortfolioAdmissionGate().evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=CANARY_PASSED,
        covariance_matrix=_covariance(),
        factor_exposures={"a": {"market": 0.2}, "b": {"market": 0.1}},
        signed_allocations=[0.05, 0.04],
        covariance_window=2,
    )
    assert decision.passed is False
    assert BLOCK_INSUFFICIENT_SAMPLE in decision.failures


def test_admission_rejects_singular_covariance():
    cov = np.array([[0.01, 0.01], [0.01, 0.01]])
    decision = PortfolioAdmissionGate(min_covariance_sample=2).evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=CANARY_PASSED,
        covariance_matrix=cov,
        factor_exposures={"a": {"market": 0.2}, "b": {"market": 0.1}},
        signed_allocations=[0.05, 0.04],
        covariance_window=2,
    )
    assert decision.passed is False
    assert BLOCK_SINGULAR_COVARIANCE in decision.failures


def test_admission_rejects_shared_factor_concentration():
    decision = PortfolioAdmissionGate().evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=CANARY_PASSED,
        covariance_matrix=_large_covariance(),
        factor_exposures={
            "strategy_a": {"market": 1.0, "sector_tech": 0.2},
            "strategy_b": {"market": 0.9, "sector_fin": 0.1},
        },
        signed_allocations=[0.05, 0.04],
        covariance_window=N,
    )
    assert decision.passed is False
    assert BLOCK_SHARED_FACTOR_CONCENTRATION in decision.failures


def test_admission_rejects_combined_stressed_daily_loss_breach():
    cov = _large_covariance()
    decision = PortfolioAdmissionGate(daily_loss_limit=0.001).evaluate(
        first_strategy_stage=STAGE_SCALE_2,
        second_strategy_stage=CANARY_PASSED,
        covariance_matrix=cov,
        factor_exposures={
            "strategy_a": {"market": 0.3},
            "strategy_b": {"market": 0.2},
        },
        signed_allocations=[0.12, 0.10],
        daily_loss_limit=0.001,
        covariance_window=N,
    )
    assert decision.passed is False
    assert BLOCK_COMBINED_DAILY_LOSS in decision.failures
