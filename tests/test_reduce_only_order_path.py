"""SP1 plan 1 Tasks 14 and 10: what the one reduce-only order path adds to master's (PR #42).

Master already pins the entry gates it skips, the account, side, size and
live-position checks, and the IB verdict mapping (``tests/test_trading_runtime.py``)
and the loop rules of ``reduce_position`` (``tests/test_order_dispatch_ports.py``).
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import reactivex as rx
from ib_async import Contract

from trader.trading.command_coordinator import BrokerRejectedError
from trader.trading.liquidation_service import DispatchRefused
from trader.trading.trading_runtime import Trader, TradingRuntimeOrderDispatch

ACCOUNT = "DU12345"
CONID = 265598


class _FakeExecutioner:
    """Each placed order gets an id and emits its local echo."""
    def __init__(self):
        self.placed = []
        self._next_id = 100

    async def subscribe_place_order_direct(self, contract, order):
        self._next_id += 1
        order.orderId = self._next_id
        self.placed.append(order)
        return rx.from_iterable([SimpleNamespace(order=order, orderStatus=SimpleNamespace(
            status="PendingSubmit", filled=0.0), contract=SimpleNamespace(conId=CONID))])


class _Tracker:
    """IB's verdict for an order id (``OrderLifecycleTracker.wait_decisive``)."""
    def __init__(self, verdict="accepted"):
        self.verdict = verdict

    async def wait_decisive(self, order_id, timeout=10.0):
        return self.verdict

    def latest_status(self, order_id):
        return "Inactive" if self.verdict == "rejected" else "Submitted"


class _FakeIB:
    def __init__(self, held):
        self.held = held
        self.connected = True
        self.open_trades = []          # trades working at IB (reducing orders count against the bound)
        self.cancelled = []

    def isConnected(self):
        return self.connected

    def positions(self, account=None):
        return [SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=CONID), position=self.held)]

    def openTrades(self):
        return list(self.open_trades)

    def cancelOrder(self, order):
        self.cancelled.append(order)


def _working(action, quantity, *, filled=0.0, oca_group="", account=ACCOUNT):
    return SimpleNamespace(contract=SimpleNamespace(conId=CONID),
                           order=SimpleNamespace(action=action, totalQuantity=quantity, ocaGroup=oca_group,
                                                 account=account),
                           orderStatus=SimpleNamespace(filled=filled))


def _trader(*, held=10.0, verdict="accepted"):
    trader = object.__new__(Trader)
    trader.ib_account = ACCOUNT
    trader.paper_trading = True
    trader.client = SimpleNamespace(ib=_FakeIB(held))
    trader.executioner = _FakeExecutioner()
    trader.order_tracker = _Tracker(verdict)
    return trader


def _contract():
    return Contract(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")


def _run(coro):
    return asyncio.run(coro)


# -- Task 14: what the Trader adds -------------------------------------------------------

def test_reduce_only_refuses_when_ib_is_not_connected():
    """D13: no connection is a refusal before anything is sent."""
    trader = _trader()
    trader.client.ib.connected = False
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                                 order_ref="mmr:x"))
    assert result.exception is None and "IB is not connected" in result.error
    assert trader.executioner.placed == []


def test_reduce_only_subtracts_reducing_orders_already_working():
    """D14: a stop whose cancel has not landed still sells 6; a second SELL 10 would reverse the position."""
    trader = _trader(held=10.0)
    trader.client.ib.open_trades = [_working("SELL", 10.0, filled=4.0), _working("BUY", 5.0)]
    refused = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                                  order_ref="mmr:x"))
    assert refused.error.startswith("reduce-only refused: quantity 10 is above 4")
    assert _run(trader.place_reduce_only_order(_contract(), "SELL", 4.0, broker_quantity=10.0,
                                               order_ref="mmr:y")).is_success()
    assert [o.totalQuantity for o in trader.executioner.placed] == [4.0]


def test_another_accounts_working_order_does_not_reduce_the_pinned_accounts_capacity():
    """#38: the client cache may hold orders of another account; only the pinned account's count.
    An order with no account is counted: an unknown owner fails closed."""
    trader = _trader(held=10.0)
    trader.client.ib.open_trades = [_working("SELL", 10.0, account="DU99999")]
    assert _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                               order_ref="mmr:x")).is_success()
    trader.client.ib.open_trades = [_working("SELL", 10.0, account="")]
    refused = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                                  order_ref="mmr:y"))
    assert refused.error.startswith("reduce-only refused")


# -- Task 14: the dispatch ---------------------------------------------------------------

class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


def _position(quantity=10.0):
    return SimpleNamespace(conid=CONID, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
                           quantity=quantity, account_id=ACCOUNT)


def _dispatch(trader, loop_thread):
    trader._main_loop = loop_thread.loop
    return TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)


def test_dispatch_reduce_position_and_partial_use_the_reduce_only_path(loop_thread):
    trader = _trader()
    dispatch = _dispatch(trader, loop_thread)
    dispatch.reduce_position(_position(10.0), "SELL", 10.0, "mmr:a-reduce-265598-1")
    dispatch.reduce_partial(_position(10.0), "SELL", 4.0, "mmr:p-reduce-265598-1")
    assert [(o.orderType, o.totalQuantity, o.orderRef) for o in trader.executioner.placed] == [
        ("MKT", 10.0, "mmr:a-reduce-265598-1"), ("MKT", 4.0, "mmr:p-reduce-265598-1")]


@pytest.mark.parametrize("call", [
    lambda d: d.reduce_position(_position(10.0), "SELL", 4.0, "mmr:x"),
    lambda d: d.reduce_partial(_position(10.0), "SELL", 10.0, "mmr:x"),
    lambda d: d.reduce_partial(_position(10.0), "SELL", 4.5, "mmr:x"),
    lambda d: d.reduce_partial(_position(10.0), "BUY", 4.0, "mmr:x"),
    lambda d: d.reduce_partial(_position(0.0), "SELL", 1.0, "mmr:x"),
])
def test_dispatch_refuses_before_the_boundary_with_dispatch_refused(loop_thread, call):
    trader = _trader()
    with pytest.raises(DispatchRefused) as ex:
        call(_dispatch(trader, loop_thread))
    assert ex.value.code == "REDUCE_ONLY_REFUSED"
    assert trader.executioner.placed == []


class _SpyTrader:
    """Records every order the dispatch schedules on the trader loop."""
    def __init__(self, loop):
        self._main_loop = loop
        self.ib_account = ACCOUNT
        self.scheduled = []

    async def place_reduce_only_order(self, *args, **kwargs):
        self.scheduled.append((args, kwargs))
        raise AssertionError("a malformed close input must never be scheduled")


_MISSING = object()


def _malformed(**fields):
    position = dict(conid=CONID, symbol="AAPL", quantity=10.0, account_id=ACCOUNT)
    position.update(fields)
    return SimpleNamespace(**{k: v for k, v in position.items() if v is not _MISSING})


_MALFORMED_POSITIONS = {
    "no symbol": _malformed(symbol=_MISSING),
    "empty symbol": _malformed(symbol=""),
    "no conid": _malformed(conid=_MISSING),
    "fractional conid": _malformed(conid=265598.5),
    "string conid": _malformed(conid=str(CONID)),
    "bool conid": _malformed(conid=True),
    "no quantity": _malformed(quantity=_MISSING),
    "None quantity": _malformed(quantity=None),
    "NaN quantity": _malformed(quantity=float("nan")),
}


@pytest.mark.parametrize("position", _MALFORMED_POSITIONS.values(), ids=_MALFORMED_POSITIONS.keys())
@pytest.mark.parametrize("call", ["reduce_position", "reduce_partial"])
def test_a_malformed_position_is_refused_before_anything_is_scheduled(loop_thread, call, position):
    """#38, ruling 52: built before the boundary, so DispatchRefused (NOT_SENT), never "maybe sent"."""
    trader = _SpyTrader(loop_thread.loop)
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(DispatchRefused) as ex:
        getattr(dispatch, call)(position, "SELL", 10.0 if call == "reduce_position" else 4.0, "mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.scheduled == []


@pytest.mark.parametrize("quantity", [None, "4", True, float("inf")])
@pytest.mark.parametrize("call", ["reduce_position", "reduce_partial"])
def test_a_quantity_that_is_not_a_finite_number_is_refused_before_anything_is_scheduled(loop_thread, call,
                                                                                         quantity):
    """The known minor of round 3: a None quantity raised TypeError, which the close read as maybe sent."""
    trader = _SpyTrader(loop_thread.loop)
    with pytest.raises(DispatchRefused) as ex:
        getattr(TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0), call)(
            _malformed(), "SELL", quantity, "mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.scheduled == []


def test_a_trader_refusal_is_dispatch_refused_and_an_ib_rejection_is_not(loop_thread):
    """R2 / R34: a refusal sent nothing (NOT_SENT); an IB rejection was sent (UNKNOWN until its row)."""
    trader = _trader(held=3.0)                      # IB now holds only 3
    with pytest.raises(DispatchRefused) as ex:
        _dispatch(trader, loop_thread).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.executioner.placed == []
    rejected = _trader(verdict="rejected")
    with pytest.raises(BrokerRejectedError) as ex:
        _dispatch(rejected, loop_thread).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    assert not isinstance(ex.value, DispatchRefused) and len(rejected.executioner.placed) == 1


def test_dispatch_refusals_without_a_running_loop_or_on_the_loop_carry_their_codes(loop_thread):
    """D13: both would block or fail before the order leaves, so both are a proven refusal."""
    trader = _trader()
    trader._main_loop = None
    with pytest.raises(DispatchRefused) as ex:
        TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    assert ex.value.code == "TRADER_LOOP_UNAVAILABLE"
    dispatch = _dispatch(trader, loop_thread)

    async def on_the_loop():
        dispatch.reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    with pytest.raises(DispatchRefused) as ex:
        asyncio.run_coroutine_threadsafe(on_the_loop(), loop_thread.loop).result(timeout=5)
    assert ex.value.code == "ON_TRADER_LOOP"
    assert trader.executioner.placed == []


def test_cancel_on_loop_reads_the_journal_off_the_loop_and_maps_unresolved_to_refused(loop_thread):
    """The perm id read (DuckDB) runs on the caller's thread; the match and cancelOrder run on the IB loop."""
    trader = _trader()
    threads = {}
    live = SimpleNamespace(permId=77)
    trader.client.ib.open_trades = [SimpleNamespace(order=live, orderStatus=SimpleNamespace(status="Submitted"))]
    trader.client.ib.cancelOrder = lambda order: threads.setdefault("cancel", (threading.get_ident(), order))
    dispatch = _dispatch(trader, loop_thread)

    def perm_id(entity):
        threads["perm"] = threading.get_ident()
        return 77 if entity == "og-1:stop" else 99
    dispatch._perm_id_for_order = perm_id
    dispatch.cancel_on_loop("og-1:stop", "mmr:c")
    assert threads["perm"] == threading.get_ident()
    assert threads["cancel"] == (loop_thread.thread.ident, live)
    with pytest.raises(DispatchRefused) as ex:
        dispatch.cancel_on_loop("og-2:stop", "mmr:c")           # perm id 99 has no live order
    assert ex.value.code == "CANCEL_UNRESOLVED"


def test_liquidation_dispatch_encodes_child_ids_and_reads_evidence():
    from trader.trading.command_stack import _LiquidationDispatch

    calls = []
    inner = SimpleNamespace(
        cancel_on_loop=lambda entity, ref: calls.append(("cancel", entity, ref)),
        reduce_position=lambda p, side, q, ref: calls.append(("reduce", side, q, ref)),
        reduce_partial=lambda p, side, q, ref: calls.append(("reduce_partial", side, q, ref)),
        find_by_order_ref=lambda account, ref: calls.append(("find", account, ref)) or ["row"],
    )
    view = SimpleNamespace(get_order=lambda entity: ("order", entity))
    adapter = _LiquidationDispatch(inner, view)
    pos = SimpleNamespace(conid=CONID, quantity=10.0)
    adapter.cancel(SimpleNamespace(order_entity_id="og-1:stop"), "c-1-cancel-265598-1")
    adapter.reduce(pos, "SELL", 10.0, "c-1-reduce-265598-1")
    adapter.reduce_partial(pos, "SELL", 4.0, "p-1-reduce-265598-1")
    assert adapter.find_orders(ACCOUNT, "c-1-reduce-265598-1") == ["row"]
    assert adapter.get_order("og-1:stop") == ("order", "og-1:stop")
    assert calls == [
        ("cancel", "og-1:stop", "mmr:c-1-cancel-265598-1"),
        ("reduce", "SELL", 10.0, "mmr:c-1-reduce-265598-1"),
        ("reduce_partial", "SELL", 4.0, "mmr:p-1-reduce-265598-1"),
        ("find", ACCOUNT, "mmr:c-1-reduce-265598-1"),
    ]


# -- Task 10: exit legs by order identity -----------------------------------------------

def test_exit_legs_carry_price_oca_group_and_reduce_oca_type():
    trader = _trader(held=6.0)
    for order_type, price, ref in (("STP", 95.0, "mmr:p-1-reprotect-stop-265598-1"),
                                   ("LMT", 120.0, "mmr:p-1-reprotect-target-265598-1")):
        result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref=ref,
                                                     order_type=order_type, price=price,
                                                     oca_group="p-1-reprotect-265598-1"))
        assert result.is_success()
    stop, target = trader.executioner.placed
    assert (stop.orderType, stop.auxPrice, target.orderType, target.lmtPrice) == ("STP", 95.0, "LMT", 120.0)
    for order in (stop, target):
        assert (order.ocaGroup, order.ocaType, order.transmit, order.parentId, order.tif) == (
            "p-1-reprotect-265598-1", 2, True, 0, "DAY")


def test_an_exit_leg_rejected_by_ib_is_not_a_refusal():
    """R13: IB's verdict for this order id; a rejected leg was sent, so it is not ``reduce-only refused``."""
    trader = _trader(held=6.0, verdict="rejected")
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:t",
                                                 order_type="LMT", price=120.0, oca_group="g"))
    assert result.exception is None and result.error.startswith("Order rejected by IB")
    assert len(trader.executioner.placed) == 1


@pytest.mark.parametrize("order_type,price", [("STP", None), ("LMT", 0.0), ("TRAIL", 1.0)])
def test_exit_leg_without_a_valid_type_or_price_is_refused(order_type, price):
    trader = _trader(held=6.0)
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:x",
                                                 order_type=order_type, price=price, oca_group="g"))
    assert result.error.startswith("reduce-only refused") and trader.executioner.placed == []


def test_dispatch_place_exit_leg_derives_side_and_type(loop_thread):
    trader = _trader(held=-6.0)
    dispatch = _dispatch(trader, loop_thread)
    dispatch.place_exit_leg(_position(-6.0), leg="stop", quantity=6.0, price=105.0, oca_group="g", order_ref="mmr:s")
    order = trader.executioner.placed[-1]
    assert (order.action, order.orderType, order.auxPrice, order.ocaGroup) == ("BUY", "STP", 105.0, "g")
    with pytest.raises(DispatchRefused):
        dispatch.place_exit_leg(_position(-6.0), leg="trail", quantity=6.0, price=1.0, oca_group="g", order_ref="mmr:x")


@pytest.mark.parametrize("position,quantity,price", [
    (_MALFORMED_POSITIONS["no symbol"], 6.0, 95.0),
    (_MALFORMED_POSITIONS["fractional conid"], 6.0, 95.0),
    (_MALFORMED_POSITIONS["None quantity"], 6.0, 95.0),
    (_malformed(), None, 95.0),
    (_malformed(), 6.0, None),
    (_malformed(), 6.0, float("nan")),
], ids=["no symbol", "fractional conid", "None position quantity", "None quantity", "None price", "NaN price"])
@pytest.mark.parametrize("leg", ["stop", "target"])
def test_a_malformed_exit_leg_is_refused_before_anything_is_scheduled(loop_thread, leg, position, quantity, price):
    """#38, ruling 52: a leg built from bad input is NOT_SENT; the close never waits on it as UNKNOWN."""
    trader = _SpyTrader(loop_thread.loop)
    with pytest.raises(DispatchRefused) as ex:
        TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0).place_exit_leg(
            position, leg=leg, quantity=quantity, price=price, oca_group="g", order_ref="mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.scheduled == []


def test_liquidation_dispatch_sends_exit_legs_with_the_child_id_as_order_ref():
    from trader.trading.command_stack import _LiquidationDispatch

    calls = []
    inner = SimpleNamespace(place_exit_leg=lambda p, **kw: calls.append(kw))
    _LiquidationDispatch(inner, SimpleNamespace()).place_exit_leg(
        SimpleNamespace(conid=CONID, quantity=6.0), leg="stop", quantity=6.0, price=95.0, oca_group="g",
        child_id="p-1-reprotect-stop-265598-1")
    assert calls == [{"leg": "stop", "quantity": 6.0, "price": 95.0, "oca_group": "g",
                      "order_ref": "mmr:p-1-reprotect-stop-265598-1"}]


def test_target_leg_is_allowed_next_to_its_own_oca_stop_but_not_next_to_another_order():
    """D14: the working stop of the same OCA pair does not count against the target's bound."""
    trader = _trader(held=6.0)
    trader.client.ib.open_trades = [_working("SELL", 6.0, oca_group="p-1-reprotect-265598-1")]
    target = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:t",
                                                 order_type="LMT", price=120.0, oca_group="p-1-reprotect-265598-1"))
    assert target.is_success()
    other = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:u",
                                                order_type="LMT", price=120.0, oca_group="another-group"))
    assert other.error.startswith("reduce-only refused")
