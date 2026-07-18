"""TradingRuntimeOrderDispatch.cancel / find_by_order_ref (step 2c, P1 fix).

The safety-critical correlation is unit-tested in test_command_ports.py
(resolve_cancel_target / orders_matching_group). These tests pin the thin glue:
cancel resolves the entity's STABLE perm_id from the alias table, matches the
LIVE open trades by it, and RAISES when it can't resolve a live order (so the
coordinator records OUTCOME_UNKNOWN instead of a false SUBMITTED);
find_by_order_ref reads the materialized broker_orders store, filtered by the
stable order group.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trader.trading.command_coordinator import BrokerRejectedError, CancelAck
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.command_ports import CancelUnresolved
from trader.trading.order_correlation import encode_order_ref
from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

ACCT = "DU123"
ENTITY = "og-cmd1:entry"  # real order key: order_group_id:leg


class _FakeIB:
    def __init__(self, trades):
        self._trades = list(trades)
        self.cancelled = []

    def openTrades(self):
        return list(self._trades)

    def cancelOrder(self, order):
        self.cancelled.append(order)


class _FakeStore:
    def __init__(self, *, rows=(), perm_ids=None):
        self._rows = list(rows)
        self._perm_ids = dict(perm_ids or {})

    def select_active_orders_in_tx(self, conn):
        return list(self._rows)

    def find_perm_id_for_order_in_tx(self, conn, order_entity_id):
        return self._perm_ids.get(order_entity_id)


def _order_row(entity_id, account=ACCT, group="og-cmd1"):
    return SimpleNamespace(
        order_entity_id=entity_id, account_id=account, order_group_id=group)


def _dispatch(*, trades=(), rows=(), perm_ids=None, store=True):
    trader = SimpleNamespace(
        client=SimpleNamespace(ib=_FakeIB(trades)),
        broker_state_store=(_FakeStore(rows=rows, perm_ids=perm_ids) if store else None),
        domain_journal=(SimpleNamespace(connect=lambda: object()) if store else None))
    return TradingRuntimeOrderDispatch(trader), trader


def test_cancel_cancels_the_live_order_resolved_via_perm_id_alias():
    order = SimpleNamespace(permId=987654321, orderId=3)
    dispatch, trader = _dispatch(
        trades=[SimpleNamespace(order=order)], perm_ids={ENTITY: 987654321})
    ack = dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))
    assert isinstance(ack, CancelAck) and ack.cancelled is True
    assert ack.order_entity_id == ENTITY
    assert trader.client.ib.cancelled == [order]


def test_cancel_raises_when_no_live_order_matches_the_perm_id():
    # perm_id resolves from the alias table, but that order isn't among the
    # current open trades (stale/terminal). Must RAISE -> OUTCOME_UNKNOWN ->
    # reconciler, NEVER report SUBMITTED for an order it didn't cancel.
    dispatch, trader = _dispatch(
        trades=[SimpleNamespace(order=SimpleNamespace(permId=111, orderId=1))],
        perm_ids={ENTITY: 987654321})
    with pytest.raises(CancelUnresolved):
        dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))
    assert trader.client.ib.cancelled == []  # NEVER touched IB


def test_cancel_raises_when_perm_id_alias_missing():
    # No perm_id alias for this entity yet (order not observed) -> unresolved.
    dispatch, trader = _dispatch(
        trades=[SimpleNamespace(order=SimpleNamespace(permId=987654321))],
        perm_ids={})
    with pytest.raises(CancelUnresolved):
        dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))
    assert trader.client.ib.cancelled == []


def test_cancel_raises_when_store_dormant():
    # No materialized store -> perm_id unresolvable -> raise (never a wrong/false cancel).
    dispatch, trader = _dispatch(trades=[SimpleNamespace(
        order=SimpleNamespace(permId=987654321))], store=False)
    with pytest.raises(CancelUnresolved):
        dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))
    assert trader.client.ib.cancelled == []


def test_find_by_order_ref_returns_only_the_matching_group_rows():
    rows = [
        _order_row("og-cmd1:entry", group="og-cmd1"),
        _order_row("og-other:entry", group="og-other"),
    ]
    dispatch, _ = _dispatch(rows=rows)
    found = dispatch.find_by_order_ref(ACCT, encode_order_ref("og-cmd1"))
    assert [r.order_entity_id for r in found] == ["og-cmd1:entry"]


def test_find_by_order_ref_is_empty_when_store_dormant():
    dispatch, _ = _dispatch(store=False)
    assert dispatch.find_by_order_ref(ACCT, encode_order_ref("og-cmd1")) == []


@pytest.mark.parametrize(
    ("account", "mode", "quantity", "reference", "message"),
    [
        ("DU999", "live", 1.0, 100.0, "account"),
        (ACCT, "paper", 1.0, 100.0, "mode"),
        (ACCT, "live", 300.0, 100.0, "notional"),
        (ACCT, "live", 1.0, float("nan"), "notional"),
    ],
)
def test_submit_rechecks_account_mode_and_notional_at_final_adapter(
    account, mode, quantity, reference, message,
):
    dispatch, trader = _dispatch()
    trader.ib_account = ACCT
    trader.paper_trading = False
    dispatch._policy = CommandAuthorityPolicy(
        enabled=True, live_enabled=True, live_account_id=ACCT,
        max_order_notional=25_000.0,
    )
    proposal = SimpleNamespace(
        account_id=account, account_mode=mode, quantity=quantity,
        reference_price=reference,
    )

    with pytest.raises(BrokerRejectedError, match=message):
        dispatch.submit(proposal, "mmr:og-cmd", "og-cmd")
