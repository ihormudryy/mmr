from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.trading.approval_context import (
    ApprovalContext,
    ExecutableMarketEvidence,
    WhatIfEvidence,
)
from trader.trading.command_coordinator import CommandRequest, RiskDirection
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.dispatch_guard import DispatchGuard, DispatchGuardError
from trader.trading.proposal_command_service import ExecutableQuote


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU123"
CONID = 265598


def _position(quantity=10.0, value=2100.0):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=CONID, symbol="AAPL", sec_type="STK",
        exchange="NASDAQ", currency="USD", quantity=quantity,
        average_cost=200.0, market_price=210.0, market_value=value,
        unrealized_pnl=100.0, realized_pnl=0.0, daily_pnl=-10.0,
        deleted=False, revision=1, source_timestamp=NOW,
    )


def _snapshot(*, generation=2, cursor=20, quantity=10.0, mode="paper"):
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=cursor,
        promoted_at=NOW - dt.timedelta(seconds=1), account_id=ACCOUNT,
        account_mode=mode, net_liquidation=100_000.0, daily_pnl=-100.0,
        positions=(_position(quantity=quantity, value=abs(quantity) * 210),),
        working_orders=(),
    )


def _quote(*, price=210.0, age=0.0, feed="live", state="continuous",
           bid=209.5, ask=210.0):
    return ExecutableQuote(
        conid=CONID, side="ask", price=price,
        market_timestamp=NOW - dt.timedelta(seconds=age),
        feed_type=feed, session_state=state, bid=bid, ask=ask,
    )


def _approved(*, mode="paper", direction=RiskDirection.INCREASING,
              quantity=5.0, quote=None, snapshot=None, what_if=True):
    quote = quote or _quote()
    return ApprovalContext(
        conid=CONID, side="BUY", quantity=quantity,
        reference_price=210.0, max_drift_bps=50.0,
        risk_direction=direction,
        broker=snapshot or _snapshot(mode=mode),
        market=ExecutableMarketEvidence(quote=quote, received_at=NOW),
        what_if=(WhatIfEvidence(response={"initMarginAfter": 1000.0}, received_at=NOW)
                 if what_if else None),
    )


class _Broker:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def capture(self, account_id):
        return self.snapshot


class _Quotes:
    def __init__(self, quote):
        self.quote = quote

    def executable_quote(self, conid, *, side):
        return self.quote


class _Margin:
    def __init__(self, response=None):
        self.response = response

    def what_if_margin(self, conid, side, quantity):
        return self.response


class _Controls:
    def __init__(self, paused=False):
        self.paused = paused

    def require_unpaused(self, account_id):
        if self.paused:
            raise RuntimeError("paused")


class _Risk:
    def check_leverage(self, margin, net_liq):
        return type("Result", (), {"approved": True, "reason": ""})()


def _request():
    return CommandRequest(
        command_id="cmd-1", action="approve_proposal", account_id=ACCOUNT,
        target_type="proposal", target_id="1", expected_version=1,
        body={"proposal_id": 1},
        source="dashboard",
    )


def _guard(*, snapshot=None, quote=None, margin=None, mode="paper", paused=False,
           max_notional=25_000.0):
    policy = CommandAuthorityPolicy(
        enabled=True, live_enabled=(mode == "live"),
        live_account_id=(ACCOUNT if mode == "live" else None),
        max_order_notional=max_notional, max_drift_bps=50.0,
    )
    return DispatchGuard(
        broker=_Broker(snapshot or _snapshot(mode=mode)),
        quotes=_Quotes(quote or _quote()), margin=_Margin(margin),
        controls=_Controls(paused), risk_gate=_Risk(), policy=policy,
        account_id=ACCOUNT, account_mode=mode,
    )


def test_revalidate_returns_permit_with_refreshed_independent_timestamps():
    permit = _guard(margin={"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0}).revalidate(
        _approved(), _request(), NOW
    )

    assert permit.generation_id == 2
    assert permit.source_cursor == 20
    assert permit.quote_timestamp == NOW
    assert permit.what_if_timestamp == NOW


@pytest.mark.parametrize(
    ("quote", "code"),
    [
        (_quote(price=float("nan")), "EXECUTABLE_QUOTE_INVALID"),
        (_quote(age=6), "QUOTE_STALE"),
        (_quote(feed="delayed"), "FEED_NOT_LIVE"),
        (_quote(state="halted"), "SESSION_INCOMPATIBLE"),
        (_quote(bid=211, ask=210), "CROSSED_MARKET"),
    ],
)
def test_live_market_failures_block_dispatch(quote, code):
    guard = _guard(quote=quote, margin={"initMarginAfter": 1000}, mode="live")

    with pytest.raises(DispatchGuardError) as error:
        guard.revalidate(_approved(mode="live", quote=_quote()), _request(), NOW)

    assert error.value.code == code


def test_live_missing_what_if_and_notional_limit_block_dispatch():
    with pytest.raises(DispatchGuardError) as missing:
        _guard(mode="live", margin=None).revalidate(
            _approved(mode="live"), _request(), NOW
        )
    assert missing.value.code == "WHAT_IF_UNAVAILABLE"

    with pytest.raises(DispatchGuardError) as notional:
        _guard(mode="live", margin={"ok": True}, max_notional=500).revalidate(
            _approved(mode="live", quantity=5), _request(), NOW
        )
    assert notional.value.code == "ORDER_NOTIONAL_LIMIT"


@pytest.mark.parametrize("margin", [{}, {"initMarginAfter": float("nan")},
                                     {"initMarginAfter": 1, "equityWithLoanAfter": float("inf")}])
def test_live_invalid_what_if_blocks_dispatch(margin):
    with pytest.raises(DispatchGuardError) as error:
        _guard(mode="live", margin=margin).revalidate(
            _approved(mode="live"), _request(), NOW
        )

    assert error.value.code == "WHAT_IF_INVALID"


def test_paper_missing_what_if_is_explicitly_recorded_not_zero_filled():
    permit = _guard(mode="paper", margin=None).revalidate(
        _approved(mode="paper"), _request(), NOW
    )

    assert permit.what_if_timestamp is None
    assert permit.warnings == ("WHAT_IF_UNAVAILABLE_PAPER",)


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (_snapshot(generation=1, cursor=20), "GENERATION_REGRESSION"),
        (_snapshot(generation=2, cursor=19), "GENERATION_REGRESSION"),
        (_snapshot(generation=2, cursor=20, quantity=11), "BROKER_STATE_CHANGED"),
        (_snapshot(generation=2, cursor=20, mode="live"), "ACCOUNT_MODE_MISMATCH"),
    ],
)
def test_broker_fence_or_target_state_change_blocks_dispatch(snapshot, code):
    with pytest.raises(DispatchGuardError) as error:
        _guard(snapshot=snapshot).revalidate(_approved(), _request(), NOW)

    assert error.value.code == code


def test_pause_is_rechecked_immediately_before_dispatch():
    with pytest.raises(DispatchGuardError) as error:
        _guard(paused=True).revalidate(_approved(), _request(), NOW)

    assert error.value.code == "TRADING_PAUSED"


@pytest.mark.parametrize(
    ("held", "action", "quantity", "permitted"),
    [
        (10.0, "SELL", 10.0, True),
        (10.0, "SELL", 9.0, True),
        (10.0, "SELL", 11.0, False),
        (-10.0, "BUY", 10.0, True),
        (-10.0, "BUY", 11.0, False),
        (0.0, "SELL", 1.0, False),
    ],
)
def test_reducing_permit_never_flips_or_increases_exposure(held, action, quantity, permitted):
    approved = _approved(
        direction=RiskDirection.REDUCING, quantity=quantity,
        snapshot=_snapshot(quantity=held),
    )
    approved = replace(approved, side=action)
    guard = _guard(snapshot=_snapshot(quantity=held))

    if permitted:
        assert guard.revalidate(approved, _request(), NOW).generation_id == 2
    else:
        with pytest.raises(DispatchGuardError) as error:
            guard.revalidate(approved, _request(), NOW)
        assert error.value.code == "REDUCTION_NOT_MONOTONIC"
