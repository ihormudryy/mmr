import datetime as dt
from decimal import Decimal
from dataclasses import asdict

import pytest
from hypothesis import given, strategies as st

from trader.automation.models import (
    ExecutionIntent, EntryPolicy, StopPolicy, TargetPolicy, TimeExitPolicy
)
from trader.automation.intent_ids import derive_intent_id, derive_command_id
from trader.trading.order_correlation import encode_order_ref

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

def make_intent_kwargs(**overrides):
    entry = EntryPolicy(order_type="LIMIT", limit_offset_bps=Decimal("5.0"), tif="DAY")
    stop = StopPolicy(stop_price=Decimal("150.0"), order_type="STP")
    target = TargetPolicy(target_price=Decimal("200.0"), order_type="LMT")
    time_exit = TimeExitPolicy(max_hold_bars=10, close_by=T0 + dt.timedelta(hours=4))

    fields = dict(
        artifact_id="art-1",
        session_id="sess-1",
        bar_id="bar-1",
        signal_id="sig-1",
        account_mode="paper",
        conid=12345,
        side="BUY",
        requested_quantity=Decimal("100"),
        risk_fraction=Decimal("0.02"),
        entry_policy=entry,
        stop_policy=stop,
        target_policy=target,
        time_exit_policy=time_exit,
        artifact_digest="digest-1",
        eligibility_attestation_digest="attest-1",
        signal_timestamp=T0,
        completed_bar_timestamp=T0 - dt.timedelta(minutes=1),
    )
    fields.update(overrides)
    
    dict_fields = {}
    for k, v in fields.items():
        if hasattr(v, "__dataclass_fields__"):
            dict_fields[k] = asdict(v)
        else:
            dict_fields[k] = v
            
    intent_id = derive_intent_id(dict_fields)
    command_id = derive_command_id(intent_id)
    
    fields["intent_id"] = intent_id
    fields["command_id"] = command_id
    return fields


def test_execution_intent_valid():
    kwargs = make_intent_kwargs()
    intent = ExecutionIntent(**kwargs)
    assert intent.intent_id.startswith("intent-")
    assert intent.command_id.startswith("auto-")
    assert ":" not in intent.intent_id
    
    # Must round-trip through encode_order_ref
    encoded = encode_order_ref(intent.intent_id)
    assert encoded == f"mmr:{intent.intent_id}"

def test_execution_intent_rejects_caller_supplied_ids():
    kwargs = make_intent_kwargs()
    kwargs["intent_id"] = "intent-forged"
    with pytest.raises(ValueError, match="does not match derived"):
        ExecutionIntent(**kwargs)

    kwargs = make_intent_kwargs()
    kwargs["command_id"] = "auto-forged"
    with pytest.raises(ValueError, match="does not match derived"):
        ExecutionIntent(**kwargs)

def test_execution_intent_rejects_naive_timestamps():
    kwargs = make_intent_kwargs()
    kwargs["signal_timestamp"] = dt.datetime(2026, 7, 18, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        ExecutionIntent(**kwargs)

    kwargs = make_intent_kwargs()
    kwargs["signal_timestamp"] = dt.datetime(2026, 7, 18, 12, 0)
    # The derive_intent_id will fail
    with pytest.raises(ValueError, match="naive datetime is not canonical"):
        make_intent_kwargs(signal_timestamp=dt.datetime(2026, 7, 18, 12, 0))

def test_execution_intent_rejects_invalid_values():
    with pytest.raises(ValueError, match="conid must be positive"):
        ExecutionIntent(**make_intent_kwargs(conid=0))

    with pytest.raises(ValueError, match="side must be BUY or SELL"):
        ExecutionIntent(**make_intent_kwargs(side="HOLD"))

    with pytest.raises(ValueError, match="risk_fraction must be in"):
        ExecutionIntent(**make_intent_kwargs(risk_fraction=Decimal("0.0")))

    with pytest.raises(ValueError, match="risk_fraction must be in"):
        ExecutionIntent(**make_intent_kwargs(risk_fraction=Decimal("1.5")))

    with pytest.raises(ValueError, match="account_mode must be"):
        ExecutionIntent(**make_intent_kwargs(account_mode="test"))

    with pytest.raises(ValueError, match="completed_bar_timestamp must be <="):
        ExecutionIntent(**make_intent_kwargs(completed_bar_timestamp=T0 + dt.timedelta(minutes=1)))

    with pytest.raises(ValueError, match="stop_price must be strictly positive"):
        stop = StopPolicy(stop_price=Decimal("0.0"), order_type="STP")
        ExecutionIntent(**make_intent_kwargs(stop_policy=stop))

    with pytest.raises(ValueError, match="target_price must be strictly positive"):
        target = TargetPolicy(target_price=Decimal("-1.0"), order_type="LMT")
        ExecutionIntent(**make_intent_kwargs(target_policy=target))

@given(st.integers(min_value=1, max_value=10000))
def test_hypothesis_stable_ids(conid):
    kwargs1 = make_intent_kwargs(conid=conid)
    kwargs2 = make_intent_kwargs(conid=conid)
    intent1 = ExecutionIntent(**kwargs1)
    intent2 = ExecutionIntent(**kwargs2)
    assert intent1.intent_id == intent2.intent_id
    assert intent1.command_id == intent2.command_id

def test_golden_id():
    kwargs = make_intent_kwargs()
    intent = ExecutionIntent(**kwargs)
    # Just to assert deterministic output
    assert intent.intent_id == derive_intent_id(asdict(intent))
