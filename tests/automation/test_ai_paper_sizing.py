"""Plan 3 Task 6: ai_paper sizing and pending entries."""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from tests.automation.ai_paper_fixtures import order, pos, snapshot
from trader.automation.ai_paper_sizing import (
    SizingInputs, is_pending_entry, max_entry_quantity, pending_entry_refusal,
)
from trader.automation.liquidity_policy import LiquidityEvidence, LiquidityPolicy
from trader.automation.risk_limits import PAPER_LIMITS

BASE = SizingInputs(equity=1_000_000.0, price=100.0, stop_price=98.0, existing_position_value=0.0,
                    current_gross_notional=0.0, liquidity_max_shares=10_000.0, notional_cap=1e9)


@pytest.mark.parametrize("changes,expected", [
    ({}, 500),                                                   # position 500; risk 1,000; gross 600
    ({"stop_price": 90.0}, 200),                                 # risk 2,000 / 10.00 = 200
    ({"current_gross_notional": 55_000.0}, 50),                  # gross 6% left: 5,000 / 100
    ({"existing_position_value": 49_950.0}, 0),                  # 50 dollars of room = 0.5 share
    ({"liquidity_max_shares": 120.4}, 120),
    ({"notional_cap": 2_100.0}, 21),
    ({"price": math.nan}, 0), ({"stop_price": 100.0}, 0), ({"stop_price": 101.0}, 0),
    ({"equity": 0.0}, 0), ({"current_gross_notional": math.inf}, 0)])
def test_max_is_the_minimum_over_every_limit_rounded_down(changes, expected):
    assert max_entry_quantity(PAPER_LIMITS, replace(BASE, **changes)) == expected


def test_floor_is_robust_to_float_noise():
    assert max_entry_quantity(replace(PAPER_LIMITS, position_fraction=0.0003), BASE) == 3   # 300/100, not 2


def test_unknown_and_external_orders_count_as_pending_entries():
    broker = snapshot(working=[order(leg=None, is_external=True), order(leg="child-7"),
                               order(leg="stop"), order(leg="take_profit"), order(leg="exit"),
                               order(leg="entry", filled=10, total=10)])           # filled: not pending
    assert [is_pending_entry(o) for o in broker.working_orders] == [True, True, False, False, False, False]


def test_pending_entries_and_position_slots():
    three = snapshot(working=[order(conid=c, leg="entry") for c in (1, 2, 3)])
    assert pending_entry_refusal(three, 4, PAPER_LIMITS) == "MAX_PENDING_ENTRIES"
    slots = snapshot(positions=[pos(1), pos(2)], working=[order(conid=3, leg="entry")])
    assert pending_entry_refusal(slots, 4, PAPER_LIMITS) == "MAX_PENDING_ENTRIES"   # 2 held + 1 working = 3
    assert pending_entry_refusal(slots, 1, PAPER_LIMITS) is None                    # adding to a held conid


def test_liquidity_max_quantity_is_the_adv_cap_or_the_depth():
    evidence = LiquidityEvidence(price=100.0, median_dollar_volume_20d=1e8, adv_shares_20d=1_000_000.0,
                                 spread_bps=5.0, top_of_book_depth=900.0, feed_type="live",
                                 session_state="continuous")
    assert LiquidityPolicy().max_quantity(evidence) == 900.0
    assert LiquidityPolicy().max_quantity(replace(evidence, top_of_book_depth=5_000.0)) == 2_500.0
    assert LiquidityPolicy().max_quantity(replace(evidence, sliced_execution_approved=True)) == 2_500.0
