"""Plan 3 Task 1: risk limits as data."""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from trader.automation.risk_limits import PAPER_LIMITS, STEADY_LIMITS, RiskLimits, RiskLimitsError
from trader.promotion.allocation_policy import STEADY_MAX_GROSS_FRACTION


def test_paper_limits_are_what_runs_today():
    assert PAPER_LIMITS.to_json() == {
        "max_positions": 3, "position_fraction": 0.05, "gross_fraction": 0.06,
        "trade_risk_fraction": 0.002, "daily_loss_fraction": 0.005, "drawdown_fraction": 0.03,
        "max_pending_entry_orders": 3,
    }


def test_steady_limits_raise_only_gross():
    assert STEADY_LIMITS.fields_above(PAPER_LIMITS) == ("gross_fraction",)
    assert STEADY_LIMITS.gross_fraction == STEADY_MAX_GROSS_FRACTION == 0.15


@pytest.mark.parametrize("field,value", [
    ("max_positions", True), ("max_positions", 3.0), ("max_positions", "3"), ("max_positions", 0),
    ("gross_fraction", True), ("gross_fraction", "0.06"), ("gross_fraction", math.nan),
    ("gross_fraction", math.inf), ("gross_fraction", 0.0), ("gross_fraction", -0.01),
    ("max_pending_entry_orders", -1)])
def test_constructor_refuses_bad_types_and_values(field, value):
    with pytest.raises(RiskLimitsError) as exc:
        replace(PAPER_LIMITS, **{field: value})
    assert exc.value.fields == (field,)


def test_int_is_accepted_for_a_fraction_and_stored_as_float():
    limits = replace(PAPER_LIMITS, gross_fraction=1)
    assert limits.gross_fraction == 1.0 and type(limits.gross_fraction) is float


def test_tighter_is_field_wise_min():
    a = replace(PAPER_LIMITS, gross_fraction=0.10, max_positions=2)
    b = replace(PAPER_LIMITS, gross_fraction=0.04)
    assert a.tighter(b) == replace(PAPER_LIMITS, gross_fraction=0.04, max_positions=2)


@pytest.mark.parametrize("changes,problem", [
    ({"gross_fraction": 1.5}, "GROSS_ABOVE_ONE"),
    ({"position_fraction": 0.07}, "POSITION_ABOVE_GROSS"),
    ({"daily_loss_fraction": 1.0}, "DAILY_LOSS_NOT_BELOW_ONE"),
    ({"drawdown_fraction": 1.0}, "DRAWDOWN_NOT_BELOW_ONE")])
def test_structural_problems(changes, problem):
    assert problem in replace(PAPER_LIMITS, **changes).structural_problems()


def test_paper_limits_have_no_structural_problem():
    assert PAPER_LIMITS.structural_problems() == ()


@pytest.mark.parametrize("value", [None, [], {"max_positions": 3}, {**PAPER_LIMITS.to_json(), "extra": 1}])
def test_from_json_requires_exact_keys(value):
    with pytest.raises(RiskLimitsError):
        RiskLimits.from_json(value)


def test_from_json_round_trips():
    assert RiskLimits.from_json(PAPER_LIMITS.to_json()) == PAPER_LIMITS
