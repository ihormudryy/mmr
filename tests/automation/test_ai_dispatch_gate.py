"""Plan 3 Task 6 (R25): the AI dispatch gate on the real DispatchGuard."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from tests.test_dispatch_guard import (
    CONID, NOW, _approved, _automated_request, _guard, _quote,
)
from trader.automation.ai_paper_evidence import AI_PAPER_ACTION, ai_entry_gate
from trader.automation.ai_paper_filter import AiEntryFilter
from trader.trading.dispatch_guard import DispatchGuardError
from trader.trading.trading_filter import TradingFilter
from tests.automation.ai_paper_fixtures import FakeUniverse, secdef

GOOD_MARGIN = {"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0}


def ai_request(expires_at=NOW + dt.timedelta(minutes=5), action=AI_PAPER_ACTION):
    return replace(_automated_request(), action=action, body={"expires_at": expires_at.isoformat()})


def ai_guard(fault=None):
    rule = {"denylist": ["AAPL"]} if fault == "denylisted_after_approval" else {}
    entry_filter = AiEntryFilter(universe=FakeUniverse({CONID: secdef(conid=CONID)}),
                                 load_filter=lambda: TradingFilter(**rule))
    margin = {"no_what_if": None, "what_if_nan": {**GOOD_MARGIN, "initMarginAfter": float("nan")}}.get(
        fault, GOOD_MARGIN)
    quote = _quote(price=209.58, ask=209.58, bid=209.5) if fault == "ask_fell_20bps" else None
    guard = _guard(margin=margin, quote=quote)
    if fault == "what_if_timeout":
        def timeout(*args):
            raise TimeoutError("what-if timed out")
        guard._margin.what_if_margin = timeout
    guard._ai_entry_gate = ai_entry_gate(entry_filter=entry_filter)
    guard._strict_margin_actions = frozenset({AI_PAPER_ACTION})
    return guard


@pytest.mark.parametrize("fault,code", [
    ("expired_before_dispatch", "DECISION_EXPIRED"), ("denylisted_after_approval", "TRADING_FILTER_DENIED"),
    ("ask_fell_20bps", "ENTRY_LIMIT_THROUGH_QUOTE"), ("no_what_if", "MARGIN_UNAVAILABLE"),
    ("what_if_timeout", "MARGIN_UNAVAILABLE"), ("what_if_nan", "MARGIN_INVALID")])
def test_ai_entry_is_refused_at_dispatch(fault, code):
    request = ai_request(NOW - dt.timedelta(seconds=1)) if fault == "expired_before_dispatch" else ai_request()
    with pytest.raises(DispatchGuardError) as caught:
        ai_guard(fault).revalidate(_approved(), request, NOW)
    assert caught.value.code == code


def test_a_healthy_ai_entry_passes_the_gate():
    assert ai_guard().revalidate(_approved(), ai_request(), NOW).generation_id == 2


def test_a_naive_or_missing_expiry_is_refused():
    request = replace(ai_request(), body={"expires_at": "2026-07-18T14:35:00"})
    with pytest.raises(DispatchGuardError, match="DECISION_INVALID"):
        ai_guard().revalidate(_approved(), request, NOW)


def test_a_raising_gate_refuses():
    guard = ai_guard()
    guard._ai_entry_gate = lambda *a: 1 / 0
    with pytest.raises(DispatchGuardError, match="AI_ENTRY_GATE_UNAVAILABLE"):
        guard.revalidate(_approved(), ai_request(), NOW)


def test_old_path_still_only_warns_without_a_what_if():                        # old path unchanged
    permit = ai_guard("no_what_if").revalidate(_approved(), _automated_request(), NOW)
    assert "WHAT_IF_UNAVAILABLE_PAPER" in permit.warnings


def test_old_path_ignores_the_ai_gate_even_when_denylisted():
    assert ai_guard("denylisted_after_approval").revalidate(_approved(), _automated_request(), NOW)
