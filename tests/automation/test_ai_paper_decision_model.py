"""Plan 3 Task 7: the strict ai_paper decision model (R16-R18)."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from trader.automation.ai_paper_decision import AiPaperDecision, DecisionInvalid, command_id_for

NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc)
FUTURE = (NOW + dt.timedelta(minutes=5)).isoformat()
ENTER = {"decision_id": "dec-00000001", "deployment_digest": "sha256:" + "a" * 64, "decider": "jev",
         "action": "ENTER", "conid": 265598, "side": "BUY", "stop_price": 98.0, "target_price": None,
         "quantity": None, "policy_revision": 1, "evidence_digest": "sha256:" + "c" * 64, "expires_at": FUTURE}
CLOSE = {**ENTER, "action": "CLOSE", "side": "SELL", "deployment_digest": None, "policy_revision": None,
         "stop_price": None}


def test_a_valid_enter_round_trips():
    decision = AiPaperDecision.from_body(ENTER)
    assert decision.to_body() == ENTER
    assert decision.expires_at == NOW + dt.timedelta(minutes=5)


@pytest.mark.parametrize("key,value", [
    ("conid", True), ("conid", 265598.0), ("conid", "265598"), ("conid", 0), ("quantity", 1.5), ("quantity", True),
    ("quantity", 0), ("stop_price", math.nan), ("stop_price", math.inf), ("stop_price", "98"), ("stop_price", True),
    ("action", "enter"), ("side", "buy"), ("decision_id", "dec:0001"), ("decision_id", "short"),
    ("policy_revision", True), ("policy_revision", 1.0), ("expires_at", "2026-07-17T15:05:00"),
    ("expires_at", 1752764700), ("expires_at", "soon"), ("evidence_digest", "abc"),
    ("decider", "Some Provider"), ("deployment_digest", "sha256:ABC")])
def test_strict_fields(key, value):
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body({**ENTER, key: value})


def test_unknown_or_missing_field_is_invalid():
    for body in ({k: v for k, v in ENTER.items() if k != "evidence_digest"}, {**ENTER, "note": "x"}, [], None):
        with pytest.raises(DecisionInvalid):
            AiPaperDecision.from_body(body)


@pytest.mark.parametrize("extra", [{"order_type": "MARKET"}, {"limit_offset_bps": 50}, {"tif": "GTC"},
                                   {"entry_policy": {"order_type": "MARKET"}}])
def test_a_decision_cannot_choose_the_order_type_or_offset(extra):            # R18, owner answer 5
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body({**ENTER, **extra})


@pytest.mark.parametrize("body", [
    {**ENTER, "action": "CLOSE", "deployment_digest": "sha256:" + "a" * 64, "policy_revision": None, "stop_price": None},
    {**ENTER, "action": "CLOSE", "deployment_digest": None, "policy_revision": 1, "stop_price": None},
    {**ENTER, "action": "CLOSE", "deployment_digest": None, "policy_revision": None, "stop_price": 98.0},
    {**ENTER, "action": "CLOSE", "deployment_digest": None, "policy_revision": None, "stop_price": None, "quantity": 5},
    {**ENTER, "action": "PARTIAL_CLOSE", "deployment_digest": None, "policy_revision": None, "quantity": None},
    {**ENTER, "stop_price": None}, {**ENTER, "deployment_digest": None}, {**ENTER, "policy_revision": None},
    {**ENTER, "target_price": 97.0}])
def test_per_action_shape(body):                                        # R16
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body(body)


def test_reductions_parse():
    assert AiPaperDecision.from_body(CLOSE).action == "CLOSE"
    partial = AiPaperDecision.from_body({**CLOSE, "action": "PARTIAL_CLOSE", "quantity": 100})
    assert (partial.quantity, partial.stop_price, partial.target_price) == (100, None, None)
    with pytest.raises(DecisionInvalid):                       # spec 6.4: a partial never moves protection
        AiPaperDecision.from_body({**CLOSE, "action": "PARTIAL_CLOSE", "quantity": 100, "stop_price": 97.5})


def test_the_constructor_checks_types_too():
    with pytest.raises(DecisionInvalid):
        AiPaperDecision(**{**AiPaperDecision.from_body(ENTER).__dict__, "conid": True})


def test_command_id_is_derived_and_colon_free():
    assert command_id_for("dec-00000001") == "aip-dec-00000001"
