"""Plan 3 Task 1: the old path decides the same with PAPER_LIMITS passed as data (spec 6, parity)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tests.automation.test_session_risk import (
    _position, make_approval, make_artifact, make_attested_strategy,
    make_broker, make_controller, make_intent, make_session,
)
from trader.automation.models import StopPolicy
from trader.automation.risk_limits import PAPER_LIMITS
from trader.automation.session_risk import AllocationCeiling
from trader.promotion.portfolio_risk_budget import BLOCK_POSITION_COUNT, PortfolioRiskBudget


def three_other_positions():
    return tuple(_position(conid, 10.0, 1_000.0) for conid in (1, 2, 3))


def gross_positions(value: float):
    return (_position(999, value / 100.0, value),)


def artifact(**overrides):
    return make_artifact(attested_strategy=make_attested_strategy(30_000.0), **overrides)


CASES = [  # (intent overrides, broker overrides, expected reason or None): one row per old-path rule
    ({}, {}, None),
    ({"requested_quantity": Decimal("600")}, {}, "POSITION_PCT"),                       # 6% > 5%
    ({"stop_policy": StopPolicy(Decimal("90"), "STP"), "requested_quantity": Decimal("300")}, {},
     "TRADE_RISK"),                                                                     # 3,000 = 0.30%
    ({}, {"daily_pnl": -5_000.0}, "DAILY_LOSS"),                                        # 0.50% of 1M
    ({}, {"positions": three_other_positions()}, "MAX_POSITIONS"),
    ({}, {"net_liquidation": 969_000.0}, "DRAWDOWN"),                                   # 3.1% below HWM
    ({"requested_quantity": Decimal("400")}, {}, "ORDER_EXCEEDS_ATTESTED_NOTIONAL"),    # 40,000 > 31,500
]


@pytest.mark.parametrize("intent_kw,broker_kw,expected", CASES)
def test_old_path_decisions_are_unchanged_with_paper_limits(intent_kw, broker_kw, expected):
    decision = make_controller().evaluate(
        make_intent(**intent_kw), artifact(), make_approval(broker=make_broker(**broker_kw)),
        make_session(limits=PAPER_LIMITS),
        AllocationCeiling(max_gross_fraction=min(PAPER_LIMITS.gross_fraction, 0.15)))
    if expected is None:
        assert decision.approved, decision.reason_codes
    else:
        assert expected in decision.reason_codes


def test_paper_gross_still_stops_at_six_percent():
    # 5.5% held + 1% new = 6.5% > 6%; the artifact allows 15%.
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("100")), make_artifact(max_gross_allocation=0.15),
        make_approval(broker=make_broker(positions=gross_positions(55_000.0))),
        make_session(limits=PAPER_LIMITS), AllocationCeiling(max_gross_fraction=0.06))
    assert "GROSS_EXPOSURE" in decision.reason_codes
    assert decision.effective_gross_ceiling == pytest.approx(0.06)


def test_risk_limits_gross_caps_even_a_looser_allocation_ceiling():
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("100")), make_artifact(max_gross_allocation=0.15),
        make_approval(broker=make_broker(positions=gross_positions(55_000.0))),
        make_session(limits=PAPER_LIMITS), AllocationCeiling(max_gross_fraction=0.15))
    assert "GROSS_EXPOSURE" in decision.reason_codes          # R3: the risk_limits candidate binds
    assert ("risk_limits", 0.06) in decision.allocation_limit_candidates


def test_tighter_limits_change_the_decision():
    tight = replace(PAPER_LIMITS, position_fraction=0.0005)  # 500 < the 1,000 order
    decision = make_controller().evaluate(
        make_intent(), make_artifact(), make_approval(), make_session(limits=tight),
        AllocationCeiling(max_gross_fraction=0.06))
    assert "POSITION_PCT" in decision.reason_codes


def test_daily_loss_uses_the_anchor_only_when_given():
    broker = make_broker(net_liquidation=990_000.0, daily_pnl=-4_960.0)
    old = make_controller().evaluate(
        make_intent(), make_artifact(), make_approval(broker=broker),
        make_session(limits=PAPER_LIMITS), AllocationCeiling(0.06))
    assert "DAILY_LOSS" in old.reason_codes                   # 4960 / 990000 = 0.501% (today's formula)
    anchored = make_controller().evaluate(
        make_intent(), make_artifact(), make_approval(broker=broker),
        make_session(limits=PAPER_LIMITS, daily_loss_anchor=1_000_000.0), AllocationCeiling(0.06))
    assert "DAILY_LOSS" not in anchored.reason_codes          # budget 5,000 on the frozen anchor


@pytest.mark.parametrize("anchor", [0.0, -1.0, float("nan"), float("inf")])
def test_an_invalid_anchor_fails_closed(anchor):
    decision = make_controller().evaluate(
        make_intent(), make_artifact(), make_approval(),
        make_session(limits=PAPER_LIMITS, daily_loss_anchor=anchor), AllocationCeiling(0.06))
    assert "DAILY_LOSS_ANCHOR_INVALID" in decision.reason_codes


def test_portfolio_budget_reads_limits():
    two = [object(), object()]
    snapshot = {"positions": two, "gross_exposure": 0.0, "daily_loss_pct": 0.0}
    blocked = PortfolioRiskBudget().evaluate(
        [], snapshot, [], limits=replace(PAPER_LIMITS, max_positions=1))
    assert BLOCK_POSITION_COUNT in blocked.blockers
    assert PortfolioRiskBudget().evaluate([], snapshot, [], limits=PAPER_LIMITS).passed

