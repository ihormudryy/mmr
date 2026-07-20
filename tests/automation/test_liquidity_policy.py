"""P3 Task 4 — instrument liquidity policy."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from trader.automation.liquidity_policy import (
    LiquidityEvidence,
    LiquidityPolicy,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _evidence(**overrides) -> LiquidityEvidence:
    base = dict(
        price=100.0,
        median_dollar_volume_20d=80_000_000.0,
        adv_shares_20d=800_000.0,
        spread_bps=8.0,
        top_of_book_depth=50_000.0,
        feed_type="live",
        session_state="continuous",
        halt_requalifying=False,
        sliced_execution_approved=False,
    )
    base.update(overrides)
    return LiquidityEvidence(**base)


def test_approves_liquid_instrument():
    decision = LiquidityPolicy().evaluate(Decimal("100"), _evidence(), now=NOW)
    assert decision.approved is True
    assert decision.reason_codes == ()


def test_rejects_price_below_five():
    decision = LiquidityPolicy().evaluate(Decimal("10"), _evidence(price=4.99), now=NOW)
    assert decision.approved is False
    assert "PRICE_FLOOR" in decision.reason_codes


def test_rejects_low_median_dollar_volume():
    decision = LiquidityPolicy().evaluate(
        Decimal("10"), _evidence(median_dollar_volume_20d=49_999_999.0), now=NOW,
    )
    assert decision.approved is False
    assert "MEDIAN_DOLLAR_VOLUME" in decision.reason_codes


def test_rejects_spread_above_15bps():
    decision = LiquidityPolicy().evaluate(Decimal("10"), _evidence(spread_bps=15.01), now=NOW)
    assert decision.approved is False
    assert "SPREAD_BPS" in decision.reason_codes


def test_rejects_quantity_above_adv_cap():
    # 0.25% of 800_000 ADV = 2_000 shares
    decision = LiquidityPolicy().evaluate(Decimal("2001"), _evidence(), now=NOW)
    assert decision.approved is False
    assert "ADV_CAP" in decision.reason_codes


def test_rejects_non_live_feed():
    for feed in ("delayed", "frozen", "delayed-frozen", "unknown"):
        decision = LiquidityPolicy().evaluate(Decimal("10"), _evidence(feed_type=feed), now=NOW)
        assert decision.approved is False
        assert "FEED_NOT_LIVE" in decision.reason_codes


def test_rejects_halted_and_feeds_breaker_signal():
    decision = LiquidityPolicy().evaluate(
        Decimal("10"), _evidence(session_state="halted"), now=NOW,
    )
    assert decision.approved is False
    assert "INSTRUMENT_HALT" in decision.reason_codes
    assert any(s.kind == "INSTRUMENT_HALT" for s in decision.breaker_signals)


def test_rejects_halt_requalification():
    decision = LiquidityPolicy().evaluate(
        Decimal("10"), _evidence(halt_requalifying=True), now=NOW,
    )
    assert decision.approved is False
    assert "HALT_REQUALIFICATION" in decision.reason_codes


def test_rejects_over_depth_without_sliced_policy():
    decision = LiquidityPolicy().evaluate(
        Decimal("50001"),
        _evidence(adv_shares_20d=50_000_000.0, top_of_book_depth=50_000.0),
        now=NOW,
    )
    assert decision.approved is False
    assert "DEPTH_EXCEEDED" in decision.reason_codes


def test_allows_over_depth_with_explicit_sliced_policy():
    decision = LiquidityPolicy().evaluate(
        Decimal("1000"),
        _evidence(
            adv_shares_20d=50_000_000.0,
            top_of_book_depth=100.0,
            sliced_execution_approved=True,
        ),
        now=NOW,
    )
    assert decision.approved is True


def test_rejects_missing_or_non_finite_inputs():
    decision = LiquidityPolicy().evaluate(
        Decimal("10"), _evidence(median_dollar_volume_20d=float("nan")), now=NOW,
    )
    assert decision.approved is False
    assert "LIQUIDITY_EVIDENCE_INVALID" in decision.reason_codes


@given(
    adv=st.floats(min_value=1e5, max_value=1e8, allow_nan=False, allow_infinity=False),
    qty_frac=st.floats(min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=40, deadline=None)
def test_monotonic_lowering_adv_cannot_approve_after_reject(adv, qty_frac):
    policy = LiquidityPolicy()
    qty = Decimal(str(round(adv * qty_frac, 4)))
    assume(qty > 0)
    hi = policy.evaluate(qty, _evidence(adv_shares_20d=adv), now=NOW)
    lo = policy.evaluate(qty, _evidence(adv_shares_20d=adv * 0.5), now=NOW)
    if not hi.approved:
        assert not lo.approved


@given(
    spread=st.floats(min_value=1.0, max_value=40.0, allow_nan=False, allow_infinity=False),
    worse=st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=40, deadline=None)
def test_monotonic_raising_spread_cannot_approve_after_reject(spread, worse):
    policy = LiquidityPolicy()
    base = policy.evaluate(Decimal("10"), _evidence(spread_bps=spread), now=NOW)
    raised = policy.evaluate(Decimal("10"), _evidence(spread_bps=spread + worse), now=NOW)
    if not base.approved:
        assert not raised.approved


@given(
    depth=st.floats(min_value=10.0, max_value=1e5, allow_nan=False, allow_infinity=False),
    qty=st.floats(min_value=1.0, max_value=2e5, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=40, deadline=None)
def test_monotonic_lowering_depth_cannot_approve_after_reject(depth, qty):
    policy = LiquidityPolicy()
    quantity = Decimal(str(round(qty, 4)))
    # Keep ADV high so ADV_CAP does not dominate
    evidence_hi = _evidence(
        adv_shares_20d=1e9, top_of_book_depth=depth, sliced_execution_approved=False,
    )
    evidence_lo = _evidence(
        adv_shares_20d=1e9, top_of_book_depth=depth * 0.5, sliced_execution_approved=False,
    )
    hi = policy.evaluate(quantity, evidence_hi, now=NOW)
    lo = policy.evaluate(quantity, evidence_lo, now=NOW)
    if not hi.approved:
        assert not lo.approved
