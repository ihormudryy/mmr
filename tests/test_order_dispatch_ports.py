"""TradingRuntimeOrderDispatch.cancel / find_by_order_ref (sequence step 2c).

The safety-critical correlation is unit-tested in test_command_ports.py
(resolve_cancel_target / orders_matching_group). These tests pin the thin glue:
cancel re-validates against the LIVE session and only ever calls ib.cancelOrder
on a currently-open order (fail safe otherwise); find_by_order_ref reads the
materialized broker_orders store, filtered by the stable order group.
"""
from __future__ import annotations

from types import SimpleNamespace

from trader.trading.command_coordinator import CancelAck
from trader.trading.order_correlation import encode_order_ref
from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

ACCT = "DU123"


class _FakeIB:
    def __init__(self, trades):
        self._trades = list(trades)
        self.cancelled = []

    def openTrades(self):
        return list(self._trades)

    def cancelOrder(self, order):
        self.cancelled.append(order)


class _FakeStore:
    def __init__(self, rows):
        self._rows = list(rows)

    def select_active_orders_in_tx(self, conn):
        return list(self._rows)


def _order_row(entity_id, account=ACCT, group="og-cmd1"):
    return SimpleNamespace(
        order_entity_id=entity_id, account_id=account, order_group_id=group)


def _dispatch(*, trades=(), rows=None):
    trader = SimpleNamespace(
        client=SimpleNamespace(ib=_FakeIB(trades)),
        broker_state_store=(_FakeStore(rows) if rows is not None else None),
        domain_journal=(SimpleNamespace(connect=lambda: object())
                        if rows is not None else None))
    return TradingRuntimeOrderDispatch(trader), trader


def test_cancel_cancels_the_live_order_matched_by_perm_id():
    order = SimpleNamespace(permId=987654321, orderId=3)
    dispatch, trader = _dispatch(trades=[SimpleNamespace(order=order)])
    ack = dispatch.cancel("order:DU123:987654321", encode_order_ref("og-cmd1"))
    assert isinstance(ack, CancelAck)
    assert ack.order_entity_id == "order:DU123:987654321" and ack.cancelled is True
    assert trader.client.ib.cancelled == [order]


def test_cancel_is_fail_safe_when_no_live_perm_id_matches():
    # The persisted order isn't among the current open trades (stale/terminal).
    dispatch, trader = _dispatch(
        trades=[SimpleNamespace(order=SimpleNamespace(permId=111, orderId=1))])
    ack = dispatch.cancel("order:DU123:987654321", encode_order_ref("og-cmd1"))
    assert ack.cancelled is False
    assert trader.client.ib.cancelled == []  # NEVER touched IB


def test_find_by_order_ref_returns_only_the_matching_group_rows():
    rows = [
        _order_row("order:DU123:1", group="og-cmd1"),
        _order_row("order:DU123:2", group="og-other"),
    ]
    dispatch, _ = _dispatch(rows=rows)
    found = dispatch.find_by_order_ref(ACCT, encode_order_ref("og-cmd1"))
    assert [r.order_entity_id for r in found] == ["order:DU123:1"]


def test_find_by_order_ref_is_empty_when_store_dormant():
    dispatch, _ = _dispatch(rows=None)  # no materialized store attached
    assert dispatch.find_by_order_ref(ACCT, encode_order_ref("og-cmd1")) == []
