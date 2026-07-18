"""Single-generation approval risk context (design Phase 5, sequence step 2a).

Every risk check for an approval must read from ONE immutable snapshot captured
at a single instant, not re-read account/positions/quote/margin at arbitrary
times across the approval->dispatch flow. A failed core read fails the capture
CLOSED (no degrade-to-zero — the current live hazard at trading_runtime.py:1323).
These tests pin the value object + the fail-closed atomic assembly with fake
ports; the live IB-backed adapters land separately.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trader.trading.proposal_command_service import ExecutableQuote
from trader.trading.approval_context import (
    ApprovalContext,
    ApprovalContextError,
    capture_approval_context,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
CONID = 265598
ACCT = "DU123"
MARGIN = {"initMarginAfter": 5000.0, "equityWithLoanAfter": 100000.0}


def _quote(price=210.0, ts=NOW, side="BUY"):
    return ExecutableQuote(
        conid=CONID, side=side, price=price, market_timestamp=ts,
        feed_type="live", session_state="continuous")


class _Quotes:
    def __init__(self, quote):
        self._quote = quote
        self.calls = 0

    def executable_quote(self, conid, *, side):
        self.calls += 1
        return self._quote


class _Positions:
    def __init__(self, reducible=0.0):
        self._reducible = reducible
        self.calls = 0

    def reducible_quantity(self, account_id, conid):
        self.calls += 1
        return self._reducible


class _Broker:
    def __init__(self, *, ready=True, net_liq=100000.0, daily_pnl=-250.0,
                 open_orders=2, position_value=5000.0, margin=MARGIN,
                 raise_on=None):
        self._ready = ready
        self._net_liq = net_liq
        self._daily_pnl = daily_pnl
        self._open_orders = open_orders
        self._position_value = position_value
        self._margin = margin
        self._raise_on = raise_on or set()
        self.calls: list[str] = []

    def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self._raise_on:
            raise RuntimeError(f"broker read {name} failed")

    def is_ready(self):
        self._maybe_raise("is_ready")
        return self._ready

    def net_liquidation(self):
        self._maybe_raise("net_liquidation")
        return self._net_liq

    def daily_pnl(self):
        self._maybe_raise("daily_pnl")
        return self._daily_pnl

    def open_order_count(self):
        self._maybe_raise("open_order_count")
        return self._open_orders

    def position_value(self, conid):
        self._maybe_raise("position_value")
        return self._position_value

    def what_if_margin(self, conid, side, quantity):
        self._maybe_raise("what_if_margin")
        return self._margin


def _capture(**over):
    kw = dict(account_id=ACCT, conid=CONID, side="BUY", quantity=10.0,
              quotes=_Quotes(_quote()), positions=_Positions(),
              broker=_Broker(), now=NOW)
    kw.update(over)
    return capture_approval_context(**kw)


class TestValueObject:
    def test_notional_is_abs_qty_times_price(self):
        ctx = _capture(quotes=_Quotes(_quote(price=210.0)))
        assert ctx.notional(10) == 2100.0
        assert ctx.notional(-4) == 840.0  # magnitude only

    def test_quote_freshness(self):
        ctx = _capture(quotes=_Quotes(_quote(ts=NOW - dt.timedelta(seconds=5))))
        assert ctx.quote_age_seconds(NOW) == pytest.approx(5.0)
        assert ctx.is_quote_fresh(NOW, max_age_seconds=30) is True
        assert ctx.is_quote_fresh(NOW + dt.timedelta(seconds=60), max_age_seconds=30) is False


class TestCapture:
    def test_captures_every_field_once(self):
        quotes, positions, broker = _Quotes(_quote(price=205.0)), _Positions(3.0), _Broker()
        ctx = capture_approval_context(
            account_id=ACCT, conid=CONID, side="SELL", quantity=8.0,
            quotes=quotes, positions=positions, broker=broker, now=NOW)
        assert isinstance(ctx, ApprovalContext)
        assert (ctx.account_id, ctx.conid, ctx.side) == (ACCT, CONID, "SELL")
        assert ctx.quote.price == 205.0
        assert ctx.net_liquidation == 100000.0 and ctx.daily_pnl == -250.0
        assert ctx.open_order_count == 2 and ctx.position_value == 5000.0
        assert ctx.reducible_quantity == 3.0 and ctx.what_if_margin == MARGIN
        assert ctx.captured_at == NOW
        # exactly one read per port (single generation)
        assert quotes.calls == 1 and positions.calls == 1
        assert broker.calls.count("net_liquidation") == 1

    def test_fail_closed_when_broker_not_ready(self):
        with pytest.raises(ApprovalContextError, match="not ready"):
            _capture(broker=_Broker(ready=False))

    def test_fail_closed_when_quote_missing(self):
        with pytest.raises(ApprovalContextError, match="quote"):
            _capture(quotes=_Quotes(None))

    def test_fail_closed_when_core_read_raises(self):
        for read in ("net_liquidation", "daily_pnl", "open_order_count",
                     "position_value"):
            with pytest.raises(ApprovalContextError):
                _capture(broker=_Broker(raise_on={read}))

    def test_fail_closed_when_reducible_read_raises(self):
        class _Boom:
            def reducible_quantity(self, account_id, conid):
                raise RuntimeError("positions unavailable")
        with pytest.raises(ApprovalContextError):
            _capture(positions=_Boom())

    def test_what_if_margin_none_is_tolerated(self):
        # whatIfOrder legitimately fails; leverage is a secondary check the
        # consumer gates by mode. A None margin must NOT fail the capture.
        ctx = _capture(broker=_Broker(margin=None))
        assert ctx.what_if_margin is None
