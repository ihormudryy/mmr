"""IB-backed read adapters for the command authority (sequence step 2b).

Thin, read-only wrappers over live trader state (portfolio / book / account
values / PnL / whatIfOrder). No mutation, no order placement. Loop-safety for the
one async read (whatIfOrder) is via an injected ``run_coro``; here it's the
identity so the adapters are testable against a fake trader without a live loop
or IB. The concrete production wiring supplies a real
``run_coroutine_threadsafe`` runner and contract resolver.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trader.trading.command_ports import (
    TraderBrokerAuthority,
    TraderPositionAuthority,
    scoped_net_liquidation,
)

ACCT = "DU123"
OTHER = "DUother"
CONID = 265598
MARGIN = {"initMarginAfter": 5000.0, "equityWithLoanAfter": 100000.0}


def _av(tag, value, currency="USD", account=ACCT):
    return SimpleNamespace(tag=tag, value=value, currency=currency, account=account)


def _pos(conid, qty, account=ACCT):
    return SimpleNamespace(
        account=account, contract=SimpleNamespace(conId=conid), position=qty)


def _item(conid, market_value, account=ACCT):
    return SimpleNamespace(
        account=account, contract=SimpleNamespace(conId=conid), marketValue=market_value)


def _fake_trader(*, positions=(), items=(), account_values=(), pnl=(),
                 open_orders=2, ready=True, ib_account=ACCT,
                 managed=(ACCT,), margin=MARGIN):
    return SimpleNamespace(
        ib_account=ib_account,
        portfolio=SimpleNamespace(
            get_positions=lambda: list(positions),
            get_portfolio_items=lambda: list(items)),
        book=SimpleNamespace(get_open_order_count=lambda: open_orders),
        client=SimpleNamespace(ib=SimpleNamespace(
            accountValues=lambda: list(account_values),
            managedAccounts=lambda: list(managed))),
        get_pnl=lambda: list(pnl),
        broker_ingest=SimpleNamespace(is_ready=lambda: ready),
        check_order_margin=lambda contract, order: margin,
    )


class TestScopedNetLiquidation:
    def test_picks_the_row_for_the_active_account(self):
        vals = [
            _av("NetLiquidation", "999", account=OTHER),
            _av("NetLiquidation", "40002.78", account=ACCT),
        ]
        assert scoped_net_liquidation(vals, ACCT) == pytest.approx(40002.78)

    def test_skips_base_currency_summary_row(self):
        vals = [
            _av("NetLiquidation", "1", currency="BASE", account=ACCT),
            _av("NetLiquidation", "40002.78", currency="USD", account=ACCT),
        ]
        assert scoped_net_liquidation(vals, ACCT) == pytest.approx(40002.78)

    def test_no_matching_row_is_zero(self):
        assert scoped_net_liquidation([_av("NetLiquidation", "5", account=OTHER)], ACCT) == 0.0
        assert scoped_net_liquidation([], ACCT) == 0.0


class TestPositionAuthority:
    def test_reducible_quantity_matches_account_and_conid(self):
        trader = _fake_trader(positions=[
            _pos(CONID, 100.0), _pos(999, 5.0), _pos(CONID, 7.0, account=OTHER)])
        auth = TraderPositionAuthority(trader)
        assert auth.reducible_quantity(ACCT, CONID) == 100.0

    def test_reducible_quantity_flat_is_zero(self):
        auth = TraderPositionAuthority(_fake_trader(positions=[]))
        assert auth.reducible_quantity(ACCT, CONID) == 0.0

    def test_reducible_quantity_preserves_short_sign(self):
        auth = TraderPositionAuthority(_fake_trader(positions=[_pos(CONID, -40.0)]))
        assert auth.reducible_quantity(ACCT, CONID) == -40.0


class TestBrokerAuthority:
    def _auth(self, trader, *, resolve=None):
        return TraderBrokerAuthority(
            trader, run_coro=lambda x: x,
            resolve_contract=resolve or (lambda conid: SimpleNamespace(conId=conid)))

    def test_sync_reads(self):
        trader = _fake_trader(
            account_values=[_av("NetLiquidation", "40002.78")],
            pnl=[SimpleNamespace(dailyPnL=-100.0), SimpleNamespace(dailyPnL=-50.0)],
            open_orders=3, items=[_item(CONID, 5000.0)])
        auth = self._auth(trader)
        assert auth.is_ready() is True
        assert auth.net_liquidation() == pytest.approx(40002.78)
        assert auth.daily_pnl() == pytest.approx(-150.0)
        assert auth.open_order_count() == 3
        assert auth.position_value(CONID) == 5000.0
        assert auth.position_value(999) == 0.0  # not held

    def test_not_ready_when_broker_ingest_not_ready(self):
        assert self._auth(_fake_trader(ready=False)).is_ready() is False

    def test_what_if_margin_routes_and_returns_dict(self):
        trader = _fake_trader(margin=MARGIN)
        assert self._auth(trader).what_if_margin(CONID, "BUY", 10.0) == MARGIN

    def test_what_if_margin_none_when_contract_unresolved(self):
        auth = self._auth(_fake_trader(), resolve=lambda conid: None)
        assert auth.what_if_margin(CONID, "BUY", 10.0) is None

    def test_what_if_margin_none_on_broker_failure(self):
        def _boom(contract, order):
            raise RuntimeError("whatIfOrder failed")
        trader = _fake_trader()
        trader.check_order_margin = _boom
        assert self._auth(trader).what_if_margin(CONID, "BUY", 10.0) is None
