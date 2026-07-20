"""P3 Task 4 — trader-owned session / portfolio risk controller."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.liquidity_policy import LiquidityEvidence
from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.automation.session_risk import (
    AllocationCeiling,
    AutomationSessionState,
    SessionRiskController,
)
from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.trading.approval_context import ApprovalContext, ExecutableMarketEvidence
from trader.trading.circuit_breaker import BreakerSignal
from trader.trading.proposal_command_service import ExecutableQuote

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)  # 11:00 ET mid-session
ACCOUNT = "DU111111"
CONID = 265598


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def _intent_fields(**overrides):
    entry = EntryPolicy(order_type="LIMIT", limit_offset_bps=Decimal("5"), tif="DAY")
    stop = StopPolicy(stop_price=Decimal("99"), order_type="STP")
    target = TargetPolicy(target_price=Decimal("110"), order_type="LMT")
    time_exit = TimeExitPolicy(max_hold_bars=10, close_by=NOW + dt.timedelta(hours=2))
    fields = dict(
        artifact_id="artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        session_id="session-1",
        bar_id="bar-1",
        signal_id="signal-1",
        account_mode="paper",
        conid=CONID,
        side="BUY",
        requested_quantity=Decimal("10"),
        risk_fraction=Decimal("0.001"),
        entry_policy=entry,
        stop_policy=stop,
        target_policy=target,
        time_exit_policy=time_exit,
        artifact_digest="digest-artifact-1",
        eligibility_attestation_digest="digest-attest-1",
        signal_timestamp=NOW,
        completed_bar_timestamp=NOW - dt.timedelta(minutes=1),
    )
    fields.update(overrides)
    dict_fields = {
        k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
        for k, v in fields.items()
    }
    fields["intent_id"] = derive_intent_id(dict_fields)
    fields["command_id"] = derive_command_id(fields["intent_id"])
    return fields


def make_intent(**overrides) -> ExecutionIntent:
    return ExecutionIntent(**_intent_fields(**overrides))


def make_artifact(**overrides) -> VerifiedArtifact:
    base = dict(
        artifact_id="artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        manifest_digest="sha256:manifest",
        dataset_manifest_digest="sha256:dataset",
        parameters={"opening_stabilization_minutes": 5},
        allowlist=(str(CONID),),
        max_gross_allocation=0.06,
        expires_at=NOW + dt.timedelta(days=30),
        public_key_id="key-1",
        verification_reason_codes=("OK",),
    )
    base.update(overrides)
    return VerifiedArtifact(**base)


def _position(conid: int, qty: float, market_value: float) -> BrokerPositionRow:
    return BrokerPositionRow(
        account_id=ACCOUNT,
        conid=conid,
        symbol=str(conid),
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        quantity=qty,
        average_cost=100.0,
        market_price=100.0,
        market_value=market_value,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        daily_pnl=0.0,
        deleted=False,
        revision=1,
        source_timestamp=NOW,
    )


def make_broker(
    *,
    net_liquidation: float = 1_000_000.0,
    daily_pnl: float = 0.0,
    positions: tuple[BrokerPositionRow, ...] = (),
    account_id: str = ACCOUNT,
) -> BrokerRiskSnapshot:
    return BrokerRiskSnapshot(
        generation_id=1,
        source_cursor=1,
        promoted_at=NOW,
        account_id=account_id,
        account_mode="paper",
        net_liquidation=net_liquidation,
        daily_pnl=daily_pnl,
        positions=positions,
        working_orders=(),
    )


def make_quote(*, price: float = 100.0, bid: float = 99.95, ask: float = 100.05,
               feed_type: str = "live", session_state: str = "continuous") -> ExecutableQuote:
    return ExecutableQuote(
        conid=CONID,
        side="BUY",
        price=price,
        market_timestamp=NOW,
        feed_type=feed_type,
        session_state=session_state,
        bid=bid,
        ask=ask,
    )


def make_approval(
    *,
    quantity: float = 10.0,
    broker: Optional[BrokerRiskSnapshot] = None,
    quote: Optional[ExecutableQuote] = None,
) -> ApprovalContext:
    q = quote or make_quote()
    return ApprovalContext(
        conid=CONID,
        side="BUY",
        quantity=quantity,
        reference_price=q.price,
        max_drift_bps=50.0,
        risk_direction="INCREASING",
        broker=broker or make_broker(),
        market=ExecutableMarketEvidence(quote=q, received_at=NOW),
        what_if=None,
    )


def make_liquidity(**overrides) -> LiquidityEvidence:
    base = dict(
        price=100.0,
        median_dollar_volume_20d=80_000_000.0,
        adv_shares_20d=800_000.0,
        spread_bps=5.0,
        top_of_book_depth=50_000.0,
        feed_type="live",
        session_state="continuous",
        halt_requalifying=False,
        sliced_execution_approved=False,
    )
    base.update(overrides)
    return LiquidityEvidence(**base)


def make_session(**overrides) -> AutomationSessionState:
    base = dict(
        high_water_mark=1_000_000.0,
        expected_account_id=ACCOUNT,
        liquidity=make_liquidity(),
        opening_stabilization=dt.timedelta(minutes=5),
    )
    base.update(overrides)
    return AutomationSessionState(**base)


class _RecordingBreaker:
    def __init__(self):
        self.signals: list[BreakerSignal] = []

    def record(self, signal: BreakerSignal):
        self.signals.append(signal)
        return SimpleNamespace(state="TRIPPED" if signal.kind != "QUOTE_FAILURE" else "CLEAR")


def make_controller(*, breaker=None, now=NOW) -> SessionRiskController:
    return SessionRiskController(
        calendar=XNYSCalendarPolicy(opening_stabilization=dt.timedelta(minutes=5)),
        breaker=breaker,
        now=lambda: now,
    )


# ---------------------------------------------------------------------------
# Hard rule tests
# ---------------------------------------------------------------------------

def test_approves_compliant_long_entry():
    decision = make_controller().evaluate(
        make_intent(), make_artifact(), make_approval(), make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is True
    assert decision.reason_codes == ()
    assert decision.approved_quantity == Decimal("10")
    assert decision.calendar_version is not None


def test_rejects_conid_not_on_allowlist():
    decision = make_controller().evaluate(
        make_intent(conid=999),
        make_artifact(allowlist=(str(CONID),)),
        make_approval(),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "CONID_NOT_PERMITTED" in decision.reason_codes


def test_rejects_artifact_id_mismatch():
    decision = make_controller().evaluate(
        make_intent(artifact_id="artifact-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        make_artifact(),
        make_approval(),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "ARTIFACT_MISMATCH" in decision.reason_codes


def test_rejects_short_opening_sell_when_flat():
    decision = make_controller().evaluate(
        make_intent(side="SELL", stop_policy=StopPolicy(stop_price=Decimal("101"), order_type="STP")),
        make_artifact(),
        make_approval(quote=make_quote(price=100.0, bid=99.95, ask=100.05)),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "LONG_ONLY" in decision.reason_codes


def test_rejects_fourth_concurrent_position():
    positions = tuple(
        _position(1000 + i, 10.0, 10_000.0) for i in range(3)
    )
    decision = make_controller().evaluate(
        make_intent(),
        make_artifact(),
        make_approval(broker=make_broker(positions=positions)),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "MAX_POSITIONS" in decision.reason_codes


def test_allows_adding_to_existing_position_without_counting_new_slot():
    positions = (
        _position(CONID, 10.0, 10_000.0),
        _position(1001, 10.0, 10_000.0),
        _position(1002, 10.0, 10_000.0),
    )
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("5")),
        make_artifact(),
        make_approval(broker=make_broker(positions=positions), quantity=5.0),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is True


def test_rejects_position_above_five_percent():
    # 10 shares * $100 = $1_000; equity $10_000 → 10% > 5%
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("10")),
        make_artifact(),
        make_approval(
            quantity=10.0,
            broker=make_broker(net_liquidation=10_000.0),
            quote=make_quote(price=100.0),
        ),
        make_session(high_water_mark=10_000.0, liquidity=make_liquidity(price=100.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "POSITION_PCT" in decision.reason_codes


def test_rejects_gross_above_six_percent_initial():
    # Existing 5% + new 2% = 7% > 6%
    positions = (_position(1001, 50.0, 50_000.0),)
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("200")),
        make_artifact(max_gross_allocation=0.06),
        make_approval(
            quantity=200.0,
            broker=make_broker(net_liquidation=1_000_000.0, positions=positions),
            quote=make_quote(price=100.0),
        ),
        make_session(liquidity=make_liquidity(adv_shares_20d=1_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "GROSS_EXPOSURE" in decision.reason_codes


def test_most_restrictive_wins_on_allocation_ceiling():
    """Signed allocation 3% beats trader/artifact 6%."""
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("400")),  # $40k = 4% of $1M
        make_artifact(max_gross_allocation=0.06),
        make_approval(quantity=400.0, quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=1_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.03),
    )
    assert decision.approved is False
    assert "GROSS_EXPOSURE" in decision.reason_codes


def test_ignores_request_fields_that_would_weaken_hard_limits():
    """Intent risk_fraction above 0.20% cannot loosen the hard trade-risk cap."""
    # stop $1 away on $100 → per-share risk $1; 3000 shares = $3000 = 0.30% of $1M
    decision = make_controller().evaluate(
        make_intent(
            requested_quantity=Decimal("3000"),
            risk_fraction=Decimal("0.01"),  # would-be weaker than 0.20%
            stop_policy=StopPolicy(stop_price=Decimal("99"), order_type="STP"),
        ),
        make_artifact(),
        make_approval(quantity=3000.0, quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=5_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "TRADE_RISK" in decision.reason_codes


def test_rejects_trade_risk_above_020_percent():
    # 2500 shares * $1 stop distance = $2500 = 0.25% of $1M
    decision = make_controller().evaluate(
        make_intent(
            requested_quantity=Decimal("2500"),
            risk_fraction=Decimal("0.002"),
            stop_policy=StopPolicy(stop_price=Decimal("99"), order_type="STP"),
        ),
        make_artifact(),
        make_approval(quantity=2500.0, quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=5_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "TRADE_RISK" in decision.reason_codes


def test_rejects_daily_loss_and_feeds_breaker():
    breaker = _RecordingBreaker()
    decision = make_controller(breaker=breaker).evaluate(
        make_intent(),
        make_artifact(),
        make_approval(broker=make_broker(daily_pnl=-6_000.0)),  # 0.60% of $1M
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "DAILY_LOSS" in decision.reason_codes
    assert any(s.kind == "DAILY_LOSS_BREACH" for s in decision.breaker_signals)
    assert any(s.kind == "DAILY_LOSS_BREACH" for s in breaker.signals)


def test_rejects_drawdown_and_feeds_breaker():
    breaker = _RecordingBreaker()
    # HWM 1_000_000, equity 960_000 → 4% drawdown > 3%
    decision = make_controller(breaker=breaker).evaluate(
        make_intent(),
        make_artifact(),
        make_approval(broker=make_broker(net_liquidation=960_000.0)),
        make_session(high_water_mark=1_000_000.0),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "DRAWDOWN" in decision.reason_codes
    assert any(s.kind == "DRAWDOWN_BREACH" for s in decision.breaker_signals)
    assert any(s.kind == "DRAWDOWN_BREACH" for s in breaker.signals)


def test_rejects_invalid_stop_for_long():
    decision = make_controller().evaluate(
        make_intent(stop_policy=StopPolicy(stop_price=Decimal("105"), order_type="STP")),
        make_artifact(),
        make_approval(quote=make_quote(price=100.0)),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "STOP_INVALID" in decision.reason_codes


def test_rejects_after_entry_cutoff():
    late = dt.datetime(2026, 7, 17, 19, 35, tzinfo=UTC)  # 15:35 ET
    decision = make_controller(now=late).evaluate(
        make_intent(
            signal_timestamp=late,
            completed_bar_timestamp=late - dt.timedelta(minutes=1),
            time_exit_policy=TimeExitPolicy(
                max_hold_bars=5, close_by=late + dt.timedelta(minutes=10),
            ),
        ),
        make_artifact(),
        make_approval(),
        make_session(),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "ENTRY_CUTOFF" in decision.reason_codes


def test_account_mismatch_rejects_and_feeds_breaker():
    breaker = _RecordingBreaker()
    decision = make_controller(breaker=breaker).evaluate(
        make_intent(),
        make_artifact(),
        make_approval(broker=make_broker(account_id="DU999999")),
        make_session(expected_account_id=ACCOUNT),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "ACCOUNT_MISMATCH" in decision.reason_codes
    assert any(s.kind == "ACCOUNT_MISMATCH" for s in decision.breaker_signals)
    assert any(s.kind == "ACCOUNT_MISMATCH" for s in breaker.signals)


def test_quote_failure_feeds_breaker_signal():
    breaker = _RecordingBreaker()
    decision = make_controller(breaker=breaker).evaluate(
        make_intent(),
        make_artifact(),
        make_approval(quote=make_quote(feed_type="delayed")),
        make_session(liquidity=make_liquidity(feed_type="delayed")),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert any(s.kind == "QUOTE_FAILURE" for s in decision.breaker_signals)
    assert any(s.kind == "QUOTE_FAILURE" for s in breaker.signals)


def test_halt_feeds_breaker_signal():
    breaker = _RecordingBreaker()
    decision = make_controller(breaker=breaker).evaluate(
        make_intent(),
        make_artifact(),
        make_approval(quote=make_quote(session_state="halted")),
        make_session(liquidity=make_liquidity(session_state="halted")),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert any(s.kind == "INSTRUMENT_HALT" for s in decision.breaker_signals)
    assert any(s.kind == "INSTRUMENT_HALT" for s in breaker.signals)


def test_artifact_gross_ceiling_more_restrictive_than_trader_default():
    decision = make_controller().evaluate(
        make_intent(requested_quantity=Decimal("500")),  # $50k = 5% of $1M
        make_artifact(max_gross_allocation=0.04),
        make_approval(quantity=500.0, quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=1_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    assert decision.approved is False
    assert "GROSS_EXPOSURE" in decision.reason_codes


# ---------------------------------------------------------------------------
# Monotonic property tests
# ---------------------------------------------------------------------------

@given(
    equity=st.floats(min_value=50_000.0, max_value=2_000_000.0, allow_nan=False, allow_infinity=False),
    shrink=st.floats(min_value=0.5, max_value=0.99, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=30, deadline=None)
def test_monotonic_lowering_equity_cannot_approve_after_reject(equity, shrink):
    qty = Decimal("200")
    controller = make_controller()
    hi = controller.evaluate(
        make_intent(requested_quantity=qty),
        make_artifact(),
        make_approval(
            quantity=float(qty),
            broker=make_broker(net_liquidation=equity),
            quote=make_quote(price=100.0),
        ),
        make_session(
            high_water_mark=equity,
            liquidity=make_liquidity(adv_shares_20d=1_000_000.0),
        ),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    lo_equity = equity * shrink
    lo = controller.evaluate(
        make_intent(requested_quantity=qty),
        make_artifact(),
        make_approval(
            quantity=float(qty),
            broker=make_broker(net_liquidation=lo_equity),
            quote=make_quote(price=100.0),
        ),
        make_session(
            high_water_mark=lo_equity,
            liquidity=make_liquidity(adv_shares_20d=1_000_000.0),
        ),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    if not hi.approved:
        assert not lo.approved


@given(
    alloc=st.floats(min_value=0.01, max_value=0.06, allow_nan=False, allow_infinity=False),
    shrink=st.floats(min_value=0.3, max_value=0.95, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=30, deadline=None)
def test_monotonic_lowering_allocation_cannot_approve_after_reject(alloc, shrink):
    qty = Decimal("400")  # $40k
    controller = make_controller()
    hi = controller.evaluate(
        make_intent(requested_quantity=qty),
        make_artifact(),
        make_approval(quantity=float(qty), quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=1_000_000.0)),
        AllocationCeiling(max_gross_fraction=alloc),
    )
    lo = controller.evaluate(
        make_intent(requested_quantity=qty),
        make_artifact(),
        make_approval(quantity=float(qty), quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=1_000_000.0)),
        AllocationCeiling(max_gross_fraction=alloc * shrink),
    )
    if not hi.approved:
        assert not lo.approved


@given(
    qty=st.floats(min_value=10.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
    bump=st.floats(min_value=1.01, max_value=3.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=30, deadline=None)
def test_monotonic_raising_order_size_cannot_approve_after_reject(qty, bump):
    controller = make_controller()
    q = Decimal(str(round(qty, 4)))
    bigger = Decimal(str(round(qty * bump, 4)))
    assume(bigger > q)
    base = controller.evaluate(
        make_intent(requested_quantity=q),
        make_artifact(),
        make_approval(quantity=float(q), quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=5_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    raised = controller.evaluate(
        make_intent(requested_quantity=bigger),
        make_artifact(),
        make_approval(quantity=float(bigger), quote=make_quote(price=100.0)),
        make_session(liquidity=make_liquidity(adv_shares_20d=5_000_000.0)),
        AllocationCeiling(max_gross_fraction=0.06),
    )
    if not base.approved:
        assert not raised.approved
