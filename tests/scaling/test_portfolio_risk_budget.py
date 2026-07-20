"""P5 Task 7 — portfolio risk budget evaluation."""
from __future__ import annotations

import pytest

from trader.promotion.portfolio_risk_budget import (
    ACCOUNT_DAILY_LOSS_LIMIT,
    BLOCK_COMBINED_GROSS,
    BLOCK_DAILY_LOSS,
    BLOCK_MISSING_PORTFOLIO_AUTHORITY,
    BLOCK_POSITION_COUNT,
    PortfolioRiskBudget,
)


def test_single_strategy_within_ceiling():
    decision = PortfolioRiskBudget().evaluate(
        intents=[{"proposed_gross": 0.02, "projected_daily_loss": 0.001}],
        broker_snapshot={"positions": [{"symbol": "AAPL"}], "gross_exposure": 0.02, "daily_loss_pct": 0.001},
        authorities=[{"max_gross_allocation": 0.06}],
    )
    assert decision.passed is True
    assert decision.remaining_capacity is not None
    assert decision.remaining_capacity == pytest.approx(0.02)


def test_combined_gross_breach():
    decision = PortfolioRiskBudget().evaluate(
        intents=[{"proposed_gross": 0.05}, {"proposed_gross": 0.04}],
        broker_snapshot={"positions": [], "gross_exposure": 0.0, "daily_loss_pct": 0.0},
        authorities=[{"max_gross_allocation": 0.06}, {"max_gross_allocation": 0.06}],
    )
    assert decision.passed is False
    assert BLOCK_COMBINED_GROSS in decision.blockers


def test_daily_loss_ceiling():
    decision = PortfolioRiskBudget().evaluate(
        intents=[{"proposed_gross": 0.02, "projected_daily_loss": 0.003}],
        broker_snapshot={"positions": [], "gross_exposure": 0.02, "daily_loss_pct": 0.003},
        authorities=[{"max_gross_allocation": 0.15}],
    )
    assert decision.passed is False
    assert BLOCK_DAILY_LOSS in decision.blockers
    assert decision.combined_daily_loss > ACCOUNT_DAILY_LOSS_LIMIT


def test_second_strategy_requires_portfolio_authority():
    decision = PortfolioRiskBudget().evaluate(
        intents=[{"proposed_gross": 0.02}],
        broker_snapshot={"positions": [], "gross_exposure": 0.0, "daily_loss_pct": 0.0},
        authorities=[{"max_gross_allocation": 0.06}],
        strategy_count=2,
        portfolio_authority_present=False,
    )
    assert decision.passed is False
    assert BLOCK_MISSING_PORTFOLIO_AUTHORITY in decision.blockers


def test_position_count_breach():
    decision = PortfolioRiskBudget().evaluate(
        intents=[],
        broker_snapshot={
            "positions": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}, {"symbol": "D"}],
            "gross_exposure": 0.0,
            "daily_loss_pct": 0.0,
        },
        authorities=[{"max_gross_allocation": 0.15}],
    )
    assert decision.passed is False
    assert BLOCK_POSITION_COUNT in decision.blockers
