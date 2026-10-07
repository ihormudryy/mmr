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

import asyncio
import datetime as dt
import threading
import time
from types import SimpleNamespace

import pytest

from trader.common.reactivex import SuccessFail
from trader.data.broker_state import BrokerPositionRow

from trader.trading.command_coordinator import BrokerRejectedError, CancelAck
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.command_ports import CancelUnresolved
from trader.trading.liquidation_service import DispatchRefused
from trader.trading.order_correlation import encode_order_ref
from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

ACCT = "DU123"
ENTITY = "og-cmd1:entry"  # real order key: order_group_id:leg


class _FakeIB:
    def __init__(self, trades):
        self._trades = list(trades)
        self.cancelled = []
        self.cancel_threads = []

    def openTrades(self):
        return list(self._trades)

    def cancelOrder(self, order):
        self.cancel_threads.append(threading.get_ident())
        self.cancelled.append(order)


@pytest.fixture
def running_loop():
    """An event loop running in its own thread, like the trader's main loop."""
    loop = asyncio.new_event_loop()
    started = threading.Event()
    loop_thread = {}

    def run():
        loop_thread["ident"] = threading.get_ident()
        loop.call_soon(started.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(2.0)
    yield loop, loop_thread["ident"]
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2.0)
    loop.close()


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


def test_cancel_cancels_the_live_order_resolved_via_perm_id_alias(running_loop):
    loop, loop_ident = running_loop
    order = SimpleNamespace(permId=987654321, orderId=3)
    dispatch, trader = _dispatch(
        trades=[SimpleNamespace(order=order)], perm_ids={ENTITY: 987654321})
    trader._main_loop = loop
    ack = dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))
    assert isinstance(ack, CancelAck) and ack.cancelled is True
    assert ack.order_entity_id == ENTITY
    assert trader.client.ib.cancelled == [order]
    # ib_async is not thread-safe: cancelOrder must run on the trader loop.
    assert trader.client.ib.cancel_threads == [loop_ident]


def test_cancel_refuses_when_trader_loop_is_not_running():
    order = SimpleNamespace(permId=987654321, orderId=3)
    for loop in (None, asyncio.new_event_loop()):
        dispatch, trader = _dispatch(
            trades=[SimpleNamespace(order=order)], perm_ids={ENTITY: 987654321})
        trader._main_loop = loop
        with pytest.raises(RuntimeError, match="not running"):
            dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))
        assert trader.client.ib.cancelled == []
        if loop is not None:
            loop.close()


def test_cancel_refuses_on_the_trader_loop_thread(running_loop):
    loop, _ = running_loop
    order = SimpleNamespace(permId=987654321, orderId=3)
    dispatch, trader = _dispatch(
        trades=[SimpleNamespace(order=order)], perm_ids={ENTITY: 987654321})
    trader._main_loop = loop

    async def cancel_on_loop():
        dispatch.cancel(ENTITY, encode_order_ref("og-cmd1"))

    with pytest.raises(RuntimeError, match="loop thread"):
        asyncio.run_coroutine_threadsafe(cancel_on_loop(), loop).result(timeout=2.0)
    assert trader.client.ib.cancelled == []


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


# ---------------------------------------------------------------------------
# reduce_position: the liquidation exit
# ---------------------------------------------------------------------------

def _position(quantity=10.0, account=ACCT):
    return BrokerPositionRow(
        account_id=account, conid=1, symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", quantity=quantity, average_cost=None, market_price=None,
        market_value=None, unrealized_pnl=None, realized_pnl=None, daily_pnl=None,
        deleted=False, revision=1, source_timestamp=dt.datetime(2026, 7, 18, tzinfo=dt.timezone.utc),
    )


class _ReduceTrader:
    def __init__(self, loop, *, result=None, delay=0.0):
        self._main_loop = loop
        self.ib_account = ACCT
        self.calls = []
        self.cancelled = []
        self._result = result or SuccessFail.success(obj=["trade"])
        self._delay = delay

    async def place_expressive_order(self, *args, **kwargs):
        raise AssertionError("liquidation must not use the entry order path")

    async def place_reduce_only_order(self, contract, side, quantity, *, broker_quantity, order_ref):
        self.calls.append((contract.conId, contract.symbol, side, quantity, broker_quantity, order_ref))
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled.append(order_ref)
            raise
        return self._result


def test_reduce_position_uses_reduce_only_path_and_keeps_exact_size(running_loop):
    loop, _ = running_loop
    trader = _ReduceTrader(loop)
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)

    assert dispatch.reduce_position(_position(10.0), "SELL", 10.0, "mmr:og-x") == ["trade"]
    assert trader.calls == [(1, "AAPL", "SELL", 10.0, 10.0, "mmr:og-x")]

    with pytest.raises(DispatchRefused, match="exactly reduce"):
        dispatch.reduce_position(_position(10.0), "SELL", 5.0, "mmr:og-x")
    with pytest.raises(DispatchRefused, match="exactly reduce"):
        dispatch.reduce_position(_position(10.0), "BUY", 10.0, "mmr:og-x")
    assert dispatch.reduce_position(_position(-4.0), "BUY", 4.0, "mmr:og-y") == ["trade"]
    assert trader.calls[-1] == (1, "AAPL", "BUY", 4.0, -4.0, "mmr:og-y")
    assert len(trader.calls) == 2


def test_reduce_position_refuses_a_position_from_another_account(running_loop):
    loop, _ = running_loop
    trader = _ReduceTrader(loop)
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(DispatchRefused, match="account"):
        dispatch.reduce_position(_position(10.0, account="DU999"), "SELL", 10.0, "mmr:og-x")
    assert trader.calls == []


def test_reduce_position_refuses_when_loop_missing_or_stopped():
    for loop in (None, asyncio.new_event_loop()):
        trader = _ReduceTrader(loop)
        dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=0.5)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="not running"):
            dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
        assert time.monotonic() - started < 0.2
        if loop is not None:
            # No late order once the loop does run.
            loop.run_until_complete(asyncio.sleep(0.05))
            loop.close()
        assert trader.calls == []


def test_reduce_position_refuses_on_the_trader_loop_thread(running_loop):
    loop, _ = running_loop
    trader = _ReduceTrader(loop)
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=0.5)

    async def reduce_on_loop():
        started = time.monotonic()
        try:
            dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
        finally:
            elapsed = time.monotonic() - started
        return elapsed

    with pytest.raises(RuntimeError, match="loop thread"):
        asyncio.run_coroutine_threadsafe(reduce_on_loop(), loop).result(timeout=2.0)
    # Let the loop yield: a queued order would run now.
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(timeout=2.0)
    assert trader.calls == []


def test_reduce_position_refusal_is_dispatch_refused(running_loop):
    loop, _ = running_loop
    trader = _ReduceTrader(loop, result=SuccessFail.fail(error="reduce-only refused: x"))
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(DispatchRefused, match="reduce-only refused"):
        dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")


def test_a_live_size_refusal_keeps_its_own_dispatch_code(running_loop):
    """#22 round 8: the liquidation re-plans a target refused for a stale size, so the code must survive."""
    loop, _ = running_loop
    trader = _ReduceTrader(loop, result=SuccessFail.fail(error="reduce-only refused: live size mismatch: x"))
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(DispatchRefused) as refused:
        dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
    assert refused.value.code == "LIVE_SIZE_MISMATCH"


def test_reduce_position_ib_rejection_is_broker_rejected_not_refused(running_loop):
    from trader.trading.command_coordinator import BrokerRejectedError

    loop, _ = running_loop
    trader = _ReduceTrader(loop, result=SuccessFail.fail(error="Order rejected by IB (entry status=Inactive)"))
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(BrokerRejectedError, match="rejected by IB") as raised:
        dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
    assert not isinstance(raised.value, DispatchRefused)


def test_reduce_position_ambiguous_send_is_not_broker_rejected(running_loop):
    from trader.trading.command_coordinator import BrokerRejectedError

    loop, _ = running_loop
    lost = RuntimeError("lost after placeOrder; may have been sent")
    trader = _ReduceTrader(loop, result=SuccessFail.fail(exception=lost))
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(Exception) as raised:
        dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
    assert not isinstance(raised.value, BrokerRejectedError)
    assert raised.value is lost


def test_reduce_position_timeout_says_may_have_been_sent_and_cancels(running_loop):
    from trader.trading.command_coordinator import BrokerRejectedError

    loop, _ = running_loop
    trader = _ReduceTrader(loop, delay=1.0)
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=0.2)
    with pytest.raises(TimeoutError, match="may have been sent") as raised:
        dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
    assert not isinstance(raised.value, BrokerRejectedError)
    deadline = time.monotonic() + 1.0
    while not trader.cancelled and time.monotonic() < deadline:
        time.sleep(0.01)
    assert trader.cancelled == ["mmr:og-x"]
