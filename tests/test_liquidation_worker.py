# tests/test_liquidation_worker.py
"""SP1 plan 1 Task 15 (R12): one serialized worker; never block the IB event loop."""
import asyncio
import datetime as dt
import threading
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_stack import _LiquidationDispatch
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import LiquidationRunStore, LiquidationService, apply_liquidation_migration
from trader.trading.liquidation_worker import LiquidationWorker, SerializedLiquidation
from trader.trading.trading_runtime import Trader, TradingRuntimeOrderDispatch

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU12345"
CONID = 265598


def _snapshot(generation, quantity):
    positions = () if not quantity else (BrokerPositionRow(
        account_id=ACCOUNT, conid=CONID, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        quantity=quantity, average_cost=None, market_price=None, market_value=None, unrealized_pnl=None,
        realized_pnl=None, daily_pnl=None, deleted=False, revision=1, source_timestamp=NOW),)
    return BrokerRiskSnapshot(generation_id=generation, source_cursor=generation, promoted_at=NOW,
                              account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
                              daily_pnl=0, positions=positions, working_orders=())


class _Broker:
    def __init__(self, snapshots, lock=None):
        self.snapshots, self.lock = list(snapshots), lock
        self.last = 0

    def capture(self, account_id):
        if self.lock is not None:
            if not self.lock.acquire(timeout=2):
                raise TimeoutError("capture could not get the ingest lock")
            self.lock.release()
        snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        self.last = snapshot.generation_id
        return snapshot


def _evidence_dispatch(broker, **orders):
    """A dispatch fake: order methods from ``orders``, broker evidence from ``broker``."""
    return SimpleNamespace(find_orders=lambda *a: [], get_order=lambda e: None,
                           enumeration_complete=lambda: True, newest_generation=lambda: broker.last, **orders)


class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=5.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


def _real_dispatch_trader(loop, held):
    """A Trader whose reduce-only path runs for real against a fake IB stream."""
    import reactivex as rx
    trader = object.__new__(Trader)
    trader.ib_account = ACCOUNT
    trader.paper_trading = True
    trader._main_loop = loop
    trader.client = SimpleNamespace(ib=SimpleNamespace(
        positions=lambda account=None: [
            SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=CONID), position=held)],
        isConnected=lambda: True, openTrades=lambda: []))
    placed = []

    class _Executioner:
        async def subscribe_place_order_direct(self, contract, order):
            order.orderId = 500 + len(placed)
            placed.append(order)
            echo = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PendingSubmit"))
            ack = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted"))
            return rx.from_iterable([echo, ack])
    trader.executioner = _Executioner()
    return trader, placed


def _service(tmp_path, broker, dispatch):
    db = DuckDBConnection.get_instance(str(tmp_path / "worker.duckdb"))
    migrator = SchemaMigrator(db)
    apply_exit_owner_migration(migrator)
    apply_liquidation_migration(migrator)
    return LiquidationService(broker, dispatch, store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db),
                              now=lambda: NOW)


def test_rescan_from_a_coroutine_on_a_real_loop_does_not_deadlock(tmp_path, loop_thread):
    """R12 / #26: the tick awaits the worker, and the worker's order runs on the same loop."""
    trader, placed = _real_dispatch_trader(loop_thread.loop, held=10.0)
    view = SimpleNamespace(get_order=lambda entity: None)
    broker = _Broker([_snapshot(1, 0.0), _snapshot(2, 10.0)])
    dispatch = _LiquidationDispatch(TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0), view)
    dispatch.find_orders = lambda account, child: []
    dispatch.newest_generation = lambda: broker.last
    service = _service(tmp_path, broker, dispatch)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.start(ACCOUNT, "flat-1", NOW + dt.timedelta(minutes=5))      # flat at generation 1: no order

    async def tick_on_the_loop():
        return await liquidation.tick_async()
    receipt = loop_thread.run(tick_on_the_loop())
    assert [o.orderRef for o in placed] == ["mmr:flat-1-reduce-265598-1"]
    assert receipt.children[0].state == "UNKNOWN"


def test_a_blocking_liquidation_call_on_the_loop_is_refused_not_deadlocked(tmp_path, loop_thread):
    service = SimpleNamespace(rescan=lambda: None, attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)

    async def blocking_on_the_loop():
        liquidation.rescan()
    with pytest.raises(RuntimeError, match="deadlock"):
        loop_thread.run(blocking_on_the_loop())


def test_ingest_thread_holding_the_apply_lock_does_not_deadlock(tmp_path):
    """The protective-failure producer queues the flatten; it never waits on the worker."""
    apply_lock = threading.Lock()
    broker = _Broker([_snapshot(1, 0.0)], lock=apply_lock)
    service = _service(tmp_path, broker, _evidence_dispatch(broker, reduce=lambda *a: None))
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    saga_port = liquidation.nonblocking()

    def ingest_thread():
        with apply_lock:
            saga_port.start(ACCOUNT, "entry-1", NOW + dt.timedelta(minutes=5))
    t = threading.Thread(target=ingest_thread)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    liquidation.worker.submit(lambda: None).result(timeout=5)   # FIFO: the queued start has run
    assert liquidation.root_for("entry-1") == "entry-1"


def test_every_entry_point_runs_on_the_one_worker_thread():
    seen = []

    def record(*_a, **_k):
        seen.append(threading.get_ident())
    service = SimpleNamespace(start=record, rescan=record, upgrade_to_zero=record, liquidate=record,
                              root_for=lambda c: None, attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    threads = [threading.Thread(target=f) for f in (
        lambda: liquidation.start(ACCOUNT, "a", NOW), lambda: liquidation.rescan(),
        lambda: liquidation.upgrade_to_zero("a"), lambda: liquidation.liquidate(object()))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(seen) == 4 and len(set(seen)) == 1 and seen[0] != threading.get_ident()


def test_tick_starts_a_flatten_for_an_unhandled_safety_failed_saga_once(tmp_path):
    broker = _Broker([_snapshot(1, 0.0)])
    service = _service(tmp_path, broker, _evidence_dispatch(broker, reduce=lambda *a: None))
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.attach_protection(SimpleNamespace(
        unhandled_failures=lambda account: ["entry-1"],
        handover_account=lambda **_k: None, close_after_full=lambda **_k: None))
    liquidation.tick()
    liquidation.tick()
    assert liquidation.root_for("entry-1") == "entry-1"
    assert liquidation.receipt_for("entry-1").scope == "account"


def test_trader_service_recovery_loop_ticks_on_the_shared_worker_inline(loop_thread):
    """PR #42's recovery loop runs ``rescan`` on its worker; with the shared worker the facade runs inline."""
    from trader import trader_service

    ticks = []
    worker = LiquidationWorker()
    service = SimpleNamespace(rescan=lambda: ticks.append(threading.get_ident()), root_for=lambda c: None,
                              attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, worker, account_id=ACCOUNT, now=lambda: NOW)

    async def one_tick():
        task = asyncio.ensure_future(trader_service._liquidation_recovery_loop(liquidation, worker, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
    loop_thread.run(one_tick())
    assert ticks and all(t == ticks[0] for t in ticks) and ticks[0] != loop_thread.thread.ident
    assert worker.submit(threading.get_ident).result(timeout=5) == ticks[0]


def test_trader_service_uses_the_worker_the_command_stack_built():
    from trader import trader_service

    built, fallback = LiquidationWorker(), LiquidationWorker()
    assert trader_service._shared_liquidation_worker(SimpleNamespace(liquidation_worker=built), fallback) is built
    assert fallback._shutdown
    other = LiquidationWorker()
    assert trader_service._shared_liquidation_worker(SimpleNamespace(), other) is other


def test_tick_keeps_going_when_one_protective_failure_cannot_start(tmp_path):
    """D8: an error for one saga is logged; the other sagas and the rescan still run."""
    starts, rescans = [], []

    def start(account_id, cause, deadline):
        if cause == "broken":
            raise RuntimeError("cannot claim")
        starts.append(cause)
    service = SimpleNamespace(start=start, rescan=lambda: rescans.append(1), root_for=lambda c: None,
                              attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.attach_protection(SimpleNamespace(unhandled_failures=lambda account: ["broken", "entry-2"]))
    liquidation.tick()
    assert (starts, rescans) == (["entry-2"], [1])
