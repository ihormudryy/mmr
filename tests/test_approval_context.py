from __future__ import annotations

import datetime as dt

import pytest

from trader.data.broker_state import (
    BrokerPositionRow,
    BrokerRiskSnapshot,
)
from trader.trading.approval_context import (
    ApprovalContext,
    ApprovalContextError,
    capture_approval_context,
)
from trader.trading.proposal_command_service import ExecutableQuote


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
PROMOTED = NOW - dt.timedelta(seconds=2)
CONID = 265598
ACCT = "DU123"
MARGIN = {"initMarginAfter": 5000.0, "equityWithLoanAfter": 100000.0}


def _quote(price=210.0, ts=NOW, side="BUY"):
    return ExecutableQuote(
        conid=CONID,
        side=side,
        price=price,
        market_timestamp=ts,
        feed_type="live",
        session_state="continuous",
    )


def _position(quantity=3.0, value=5000.0):
    return BrokerPositionRow(
        account_id=ACCT,
        conid=CONID,
        symbol="AAPL",
        sec_type="STK",
        exchange="NASDAQ",
        currency="USD",
        quantity=quantity,
        average_cost=200.0,
        market_price=210.0,
        market_value=value,
        unrealized_pnl=50.0,
        realized_pnl=0.0,
        daily_pnl=-25.0,
        deleted=False,
        revision=2,
        source_timestamp=PROMOTED,
    )


def _snapshot(account=ACCT):
    return BrokerRiskSnapshot(
        generation_id=7,
        source_cursor=42,
        promoted_at=PROMOTED,
        account_id=account,
        account_mode="paper",
        net_liquidation=100000.0,
        daily_pnl=-250.0,
        positions=(_position(),),
        working_orders=(),
    )


class _Quotes:
    def __init__(self, quote):
        self.quote = quote
        self.calls = 0

    def executable_quote(self, conid, *, side):
        self.calls += 1
        return self.quote


class _Broker:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot or _snapshot()
        self.error = error
        self.calls = 0

    def capture(self, account_id):
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot


class _Margin:
    def __init__(self, response=MARGIN, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def what_if_margin(self, conid, side, quantity):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def _capture(**overrides):
    values = dict(
        account_id=ACCT,
        conid=CONID,
        side="BUY",
        quantity=10.0,
        quotes=_Quotes(_quote()),
        broker=_Broker(),
        margin=_Margin(),
        now=NOW,
    )
    values.update(overrides)
    return capture_approval_context(**values)


def test_context_keeps_broker_generation_separate_from_market_and_what_if_clocks():
    broker = _Broker()
    quotes = _Quotes(_quote(price=205.0, ts=NOW - dt.timedelta(seconds=5)))
    margin = _Margin()

    context = _capture(broker=broker, quotes=quotes, margin=margin, side="SELL")

    assert isinstance(context, ApprovalContext)
    assert context.broker.generation_id == 7
    assert context.broker.source_cursor == 42
    assert context.broker.promoted_at == PROMOTED
    assert context.broker.account_mode == "paper"
    assert context.market.quote.price == 205.0
    assert context.market.received_at == NOW
    assert context.what_if.response == MARGIN
    assert context.what_if.received_at == NOW
    assert broker.calls == quotes.calls == margin.calls == 1


def test_compatibility_properties_read_from_fenced_subobjects():
    context = _capture()

    assert context.account_id == ACCT
    assert context.net_liquidation == 100000.0
    assert context.daily_pnl == -250.0
    assert context.open_order_count == 0
    assert context.position_value == 5000.0
    assert context.reducible_quantity == 3.0
    assert context.what_if_margin == MARGIN
    assert context.notional(10) == 2100.0
    assert context.captured_at == NOW


def test_quote_freshness_uses_market_timestamp_not_generation_timestamp():
    context = _capture(
        quotes=_Quotes(_quote(ts=NOW - dt.timedelta(seconds=5)))
    )

    assert context.quote_age_seconds(NOW) == pytest.approx(5.0)
    assert context.is_quote_fresh(NOW, max_age_seconds=30) is True
    assert context.is_quote_fresh(
        NOW + dt.timedelta(seconds=60), max_age_seconds=30
    ) is False


def test_failed_snapshot_and_missing_quote_fail_closed():
    with pytest.raises(ApprovalContextError, match="broker_risk_snapshot"):
        _capture(broker=_Broker(error=RuntimeError("database unavailable")))
    with pytest.raises(ApprovalContextError, match="quote"):
        _capture(quotes=_Quotes(None))


def test_snapshot_for_wrong_account_fails_closed():
    with pytest.raises(ApprovalContextError) as error:
        _capture(broker=_Broker(snapshot=_snapshot(account="DU999")))

    assert error.value.code == "ACCOUNT_MISMATCH"


def test_what_if_failure_is_timestamped_none_for_mode_aware_consumer():
    context = _capture(margin=_Margin(error=RuntimeError("IB timeout")))

    assert context.what_if is None
    assert context.what_if_margin is None
