"""Plan 3 Task 6: the ai_paper evidence factory (sizing, then capture, then re-check)."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import (
    ACCOUNT, CONID, GOOD_MARGIN, SESSION, FakeUniverse, Margin, Quotes, SnapshotSequence, order, pos, prepare,
    quote, secdef, snapshot,
)
from trader.automation.ai_paper_filter import AiEntryFilter
from trader.automation.ai_paper_sizing import max_entry_quantity, sizing_inputs
from trader.automation.risk_limits import PAPER_LIMITS
from trader.promotion.canary_risk import CanaryRiskStore
from trader.trading.approval_context import ApprovalContextError, EntryLimitsEvidence
from trader.trading.trading_filter import TradingFilter, TradingFilterError

def test_factory_captures_snapshot_quote_margin_hwm_and_liquidity(parts):
    prepared = prepare(parts)
    a = prepared.approval
    assert a.broker.generation_id > 0 and a.market.quote.feed_type == "live" and a.what_if is not None
    assert a.risk_direction == "INCREASING" and a.side == "BUY" and a.quantity == prepared.quantity
    assert prepared.session_state.high_water_mark == 1_000_000.0 and prepared.session_state.liquidity is not None
    assert prepared.session_state.limits == PAPER_LIMITS
    assert prepared.session_state.daily_loss_anchor == SESSION.anchor
    assert prepared.allocation.max_gross_fraction == PAPER_LIMITS.gross_fraction
    assert a.entry_limits.stop_price == 98.0 and a.entry_limits.limits == PAPER_LIMITS
    assert CanaryRiskStore(parts["journal"], "ai_paper:exp1", ACCOUNT).get_high_water_mark() == 1_000_000.0


def test_no_quantity_uses_the_maximum_on_the_entry_limit_price(parts):
    # Ask 100.00 + 10 bps = 100.10: position 50,000 / 100.10 = 499.5 -> 499.
    inputs = sizing_inputs(snapshot(), conid=CONID, price=100.10, stop_price=98.0,
                           liquidity_max_shares=2_500.0, notional_cap=1e9 * 1.05)
    assert prepare(parts).quantity == max_entry_quantity(PAPER_LIMITS, inputs) == 499


def test_quantity_at_or_below_the_maximum_is_used_as_is(parts):
    assert prepare(parts, requested_quantity=499).quantity == 499
    assert prepare(parts, requested_quantity=7).quantity == 7


def test_quantity_above_the_maximum_is_refused_not_cut(parts):
    with pytest.raises(ApprovalContextError, match="QUANTITY_ABOVE_MAXIMUM"):
        prepare(parts, requested_quantity=500)


def test_attested_notional_is_checked_before_the_maximum(parts):           # R12
    with pytest.raises(ApprovalContextError, match="ORDER_EXCEEDS_ATTESTED_NOTIONAL"):
        prepare(parts, requested_quantity=30, notional=2_000.0)


def test_attested_notional_caps_the_maximum(parts):
    assert prepare(parts, notional=2_000.0).quantity == 20                   # 2,100 / 100.10


def test_maximum_below_one_share_refuses(parts):
    parts["broker"] = SnapshotSequence(snapshot(positions=[pos(CONID, 499.6, 49_960.0)]))
    with pytest.raises(ApprovalContextError, match="QUANTITY_BELOW_ONE_SHARE"):
        prepare(parts)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_requested_quantity_must_be_a_positive_int(parts, value):
    with pytest.raises(ApprovalContextError, match="QUANTITY_INVALID"):
        prepare(parts, requested_quantity=value)


def test_pending_entries_beyond_the_limit_are_refused_before_any_quote_read(parts):
    calls = []
    parts["quotes"] = SimpleNamespace(executable_quote=lambda *a, **k: calls.append(1))
    parts["broker"] = SnapshotSequence(snapshot(working=[order(conid=c) for c in (1, 2, 3)]))
    with pytest.raises(ApprovalContextError, match="MAX_PENDING_ENTRIES"):
        prepare(parts)
    assert calls == []


def test_size_is_rechecked_on_the_captured_snapshot(parts):
    parts["broker"] = SnapshotSequence(snapshot(), snapshot(positions=[pos(9, 550.0, 55_000.0)]))
    with pytest.raises(ApprovalContextError, match="QUANTITY_ABOVE_MAXIMUM"):
        prepare(parts, requested_quantity=100)


def test_an_unpriced_working_buy_makes_gross_unknown(parts):
    parts["broker"] = SnapshotSequence(snapshot(working=[order(conid=9, limit=None)]))
    with pytest.raises(ApprovalContextError, match="QUANTITY_BELOW_ONE_SHARE"):
        prepare(parts)


def _fault(parts, fault):
    if fault == "no_quote":
        parts["quotes"] = SimpleNamespace(executable_quote=lambda *a, **k: None)
    elif fault == "delayed_feed":
        parts["quotes"] = Quotes(quote(feed="delayed"))
    elif fault == "stale_quote":
        parts["quotes"] = Quotes(quote(age=6.0))
    elif fault == "no_what_if":
        parts["margin"] = Margin(response=None)
    elif fault == "what_if_timeout":
        parts["margin"] = Margin(error=TimeoutError("what-if timed out"))
    elif fault == "what_if_nan":
        parts["margin"] = Margin(response={**GOOD_MARGIN, "initMarginAfter": float("nan")})
    elif fault == "what_if_negative":
        parts["margin"] = Margin(response={**GOOD_MARGIN, "equityWithLoanAfter": -1.0})
    elif fault == "what_if_incomplete":
        parts["margin"] = Margin(response={"initMarginAfter": 1.0})
    elif fault == "no_history":
        parts["history"] = None
    elif fault == "denylisted":
        parts["entry_filter"] = AiEntryFilter(universe=FakeUniverse({CONID: secdef()}),
                                              load_filter=lambda: TradingFilter(denylist=["AAPL"]))
    elif fault == "unresolved":
        parts["entry_filter"] = AiEntryFilter(universe=FakeUniverse({}), load_filter=TradingFilter)
    elif fault == "filter_unparsable":
        def broken():
            raise TradingFilterError("bad yaml")
        parts["entry_filter"] = AiEntryFilter(universe=FakeUniverse({CONID: secdef()}), load_filter=broken)
    elif fault == "live_account":
        parts["account_mode"] = "live"
    elif fault == "wrong_account":
        parts["broker"] = SnapshotSequence(snapshot(account="DU999"))
    elif fault == "fence_zero":
        parts["broker"] = SnapshotSequence(snapshot(generation=0))
    elif fault == "broker_raises":
        parts["broker"] = SimpleNamespace(capture=lambda account: (_ for _ in ()).throw(RuntimeError("secret")))


@pytest.mark.parametrize("fault,code", [
    ("no_quote", "EVIDENCE_UNAVAILABLE"), ("delayed_feed", "FEED_NOT_LIVE"), ("stale_quote", "QUOTE_STALE"),
    ("no_what_if", "MARGIN_UNAVAILABLE"), ("what_if_timeout", "MARGIN_UNAVAILABLE"),
    ("what_if_nan", "MARGIN_INVALID"), ("what_if_negative", "MARGIN_INVALID"),
    ("what_if_incomplete", "MARGIN_INVALID"), ("no_history", "HISTORY_UNAVAILABLE"),
    ("denylisted", "TRADING_FILTER_DENIED"), ("unresolved", "INSTRUMENT_UNRESOLVED"),
    ("filter_unparsable", "TRADING_FILTER_UNAVAILABLE"), ("live_account", "PAPER_ONLY"),
    ("wrong_account", "ACCOUNT_MISMATCH"), ("fence_zero", "BROKER_FENCE_INVALID"),
    ("broker_raises", "EVIDENCE_UNAVAILABLE")])
def test_every_missing_evidence_fails_closed_with_its_code(parts, fault, code):
    _fault(parts, fault)
    with pytest.raises(ApprovalContextError) as exc:
        prepare(parts)
    assert exc.value.code == code
    assert "secret" not in str(exc.value)


def test_margin_is_never_reported_as_a_sizing_bound(parts):                     # R11
    prepared = prepare(parts)
    assert prepared.margin_checked is True
    assert not [f.name for f in dataclasses.fields(EntryLimitsEvidence) if "margin" in f.name]


@pytest.mark.parametrize("stop", [100.10, 101.0])
def test_a_stop_at_or_above_the_entry_price_is_invalid(parts, stop):
    with pytest.raises(ApprovalContextError, match="STOP_INVALID"):
        prepare(parts, stop_price=stop)


# --- Issue #74: paper IEX quotes need the explicit accepted-feed set ------------

def test_an_iex_entry_quote_is_refused_by_default(parts):
    parts["quotes"] = Quotes(quote(feed="iex_realtime"))
    with pytest.raises(ApprovalContextError) as exc:
        prepare(parts)
    assert exc.value.code == "FEED_NOT_LIVE"


def test_an_approved_iex_entry_records_the_iex_feed(parts):
    parts["quotes"] = Quotes(quote(feed="iex_realtime"))
    prepared = prepare(dict(parts, accepted_feeds=frozenset({"live", "iex_realtime"})))
    assert prepared.approval.market.quote.feed_type == "iex_realtime"
    assert prepared.session_state.liquidity.feed_type == "iex_realtime"
