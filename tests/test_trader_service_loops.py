"""trader_service liquidation/session ticks on a real event loop.

Before the fix the 5s ticks called ``rescan()`` / ``run_due()`` on the trader
loop, and ``reduce_position`` then waited on that same loop: a timeout, then a
late order. The ticks now run on the single liquidation worker thread while
the loop stays free to place the order.

Wiring: a real ``LiquidationService`` on a DuckDB journal ->
``command_stack._LiquidationDispatch`` -> ``TradingRuntimeOrderDispatch`` ->
a fake trader whose ``place_reduce_only_order`` records which thread ran it
and when. Broker evidence (order rows, generations) is answered by the test.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader import trader_service
from trader.common.reactivex import SuccessFail
from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_stack import _LiquidationDispatch
from trader.trading.exit_owner import ExitOwnerRegistry
from trader.trading.liquidation_service import (
    LiquidationRunStore, LiquidationService, apply_liquidation_migration,
)
from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

UTC = dt.timezone.utc
ET = ZoneInfo("America/New_York")
ACCOUNT = "DU111111"
SESSION_DATE = dt.date(2026, 7, 17)
FLATTEN_TIME = dt.datetime(2026, 7, 17, 15, 45, tzinfo=ET).astimezone(UTC)


def _position(quantity=10.0):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=265598, symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", quantity=quantity, average_cost=None, market_price=None,
        market_value=None, unrealized_pnl=None, realized_pnl=None, daily_pnl=None,
        deleted=False, revision=1, source_timestamp=FLATTEN_TIME,
    )


class _Broker:
    def capture(self, account_id):
        assert account_id == ACCOUNT
        return BrokerRiskSnapshot(
            generation_id=1, source_cursor=1, promoted_at=FLATTEN_TIME,
            account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000.0,
            daily_pnl=0.0, positions=(_position(),), working_orders=(),
        )


class _FakeTrader:
    def __init__(self, loop, *, send_seconds=0.01):
        self._main_loop = loop
        self.ib_account = ACCOUNT
        self.orders = []
        self._send_seconds = send_seconds

    async def place_reduce_only_order(self, contract, side, quantity, *, broker_quantity, order_ref):
        await asyncio.sleep(self._send_seconds)
        self.orders.append((threading.get_ident(), time.monotonic(), side, quantity))
        return SuccessFail.success(obj=[])


class _EvidenceDispatch(TradingRuntimeOrderDispatch):
    """The real order dispatch; the fake trader has no broker store, so the test answers the evidence."""
    def find_by_order_ref(self, account_id, order_ref):
        return []

    def enumeration_complete(self):
        return True

    def newest_generation(self):
        return 1

    def executed_quantities(self, account_id, order_entity_ids):
        return {}

    def unbound_execution_since(self, account_id, conid, generation_id):
        return False


def _liquidation(trader, tmp_path, *, resume=True, clock=None):
    """``resume``: a run a previous process claimed and left REQUESTED."""
    clock = clock or [FLATTEN_TIME]
    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
    apply_liquidation_migration(SchemaMigrator(db))
    dispatch = _LiquidationDispatch(_EvidenceDispatch(trader, dispatch_timeout=0.5),
                                    SimpleNamespace(get_order=lambda entity: None))
    service = LiquidationService(_Broker(), dispatch, store=LiquidationRunStore(db),
                                 registry=ExitOwnerRegistry(db), now=lambda: clock[0])
    if resume:
        deadline = clock[0] + dt.timedelta(minutes=5)
        LiquidationRunStore(db).transaction(
            lambda conn: service._claim_account_in_tx(conn, ACCOUNT, "root-1", deadline))
    return service


@pytest.fixture
def worker():
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="liquidation-worker")
    yield executor
    executor.shutdown(wait=True)


def _close_loop(loop):
    tasks = asyncio.all_tasks(loop)
    for task in tasks:
        task.cancel()
    if tasks:
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    loop.close()


# ---------------------------------------------------------------------------
# Periodic ticks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_liquidation_recovery_tick_on_real_loop_places_exactly_one_order(tmp_path, worker):
    trader = _FakeTrader(asyncio.get_running_loop())
    service = _liquidation(trader, tmp_path)

    started = time.monotonic()
    receipt = await trader_service._liquidation_recovery_tick(service, worker)
    returned = time.monotonic()

    assert receipt.state == "VERIFYING", receipt
    assert len(trader.orders) == 1
    thread_id, sent_at, side, quantity = trader.orders[0]
    assert (side, quantity) == ("SELL", 10.0)
    assert thread_id == threading.get_ident()  # placed on the trader loop
    assert sent_at < returned  # no late order
    assert returned - started < 0.5


def _session_controller(tmp_path: Path, liquidation, clock):
    from trader.automation.calendar_policy import XNYSCalendarPolicy
    from trader.automation.session_controller import (
        SessionController,
        apply_session_controller_migration,
    )
    from trader.data.domain_journal import DomainJournal
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator

    db = DuckDBConnection.get_instance(str(tmp_path / "session.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_session_controller_migration(migrator)

    class _NoCancel:
        def cancel_working_entries(self, *, root_command_id, orders):
            return []

    class _Breaker:
        def record(self, signal):
            return SimpleNamespace(state="TRIPPED", reason_code=signal.kind)

    return SessionController(
        journal=journal, db=db, calendar=XNYSCalendarPolicy(), broker=_Broker(),
        cancel=_NoCancel(), liquidation=liquidation, breaker=_Breaker(),
        time_exit=SimpleNamespace(request_exit=lambda **kw: None),
        account_id=ACCOUNT, now=lambda: clock[0],
    )


@pytest.mark.asyncio
async def test_session_controller_tick_flatten_on_real_loop_does_not_block_loop(tmp_path, worker):
    clock = [FLATTEN_TIME]
    trader = _FakeTrader(asyncio.get_running_loop(), send_seconds=0.1)
    liquidation = _liquidation(trader, tmp_path, resume=False, clock=clock)
    controller = _session_controller(tmp_path, liquidation, clock)
    controller.recover(dt.datetime(2026, 7, 17, 11, 0, tzinfo=ET))

    beats = []

    async def heartbeat():
        while True:
            beats.append(time.monotonic())
            await asyncio.sleep(0.01)

    beating = asyncio.create_task(heartbeat())
    try:
        started = time.monotonic()
        state = await trader_service._session_controller_tick(controller, worker, clock[0])
        returned = time.monotonic()
    finally:
        beating.cancel()

    assert state.flatten_issued and state.state == "VERIFYING_FLAT", state
    assert len(trader.orders) == 1
    assert trader.orders[0][1] < returned
    receipt = liquidation.rescan()
    assert receipt.state == "VERIFYING"
    assert len(trader.orders) == 1
    # The loop kept running while the worker waited on the order.
    assert len([b for b in beats if started <= b <= returned]) >= 5


@pytest.mark.asyncio
async def test_watched_loop_logs_critical_and_never_stacks_worker_calls(monkeypatch):
    critical = []
    monkeypatch.setattr(trader_service.logging, "critical",
                        lambda msg, *args: critical.append(msg % args))
    release = asyncio.Event()
    calls = []

    async def stuck_tick():
        calls.append(time.monotonic())
        if len(calls) == 1:
            await release.wait()

    loop_task = asyncio.create_task(trader_service._watched_ticks(
        "test", stuck_tick, interval=0.01, stuck_after=0.05))
    try:
        await asyncio.sleep(0.25)
        assert len(calls) == 1
        assert len(critical) == 1
        assert "still running" in critical[0]
        release.set()
        await asyncio.sleep(0.1)
        assert len(calls) >= 2
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Startup recovery (before trader.run())
# ---------------------------------------------------------------------------

def test_startup_liquidation_recovery_dispatches_while_loop_runs(tmp_path, worker):
    loop = asyncio.new_event_loop()
    try:
        trader = _FakeTrader(loop)
        service = _liquidation(trader, tmp_path)
        holder = SimpleNamespace(liquidation_service=service)

        started = time.monotonic()
        trader_service._maybe_start_liquidation_recovery(holder, loop, worker)
        returned = time.monotonic()

        assert len(trader.orders) == 1
        assert trader.orders[0][1] < returned
        assert returned - started < 0.5
        assert service.receipt_for("root-1").state == "VERIFYING"
    finally:
        _close_loop(loop)


def test_startup_liquidation_recovery_without_trader_loop_sends_nothing_late(tmp_path, worker):
    loop = asyncio.new_event_loop()
    try:
        trader = _FakeTrader(None)  # e.g. the fake-broker path: _main_loop not set yet
        service = _liquidation(trader, tmp_path)
        holder = SimpleNamespace(liquidation_service=service)

        started = time.monotonic()
        trader_service._maybe_start_liquidation_recovery(holder, loop, worker)
        assert time.monotonic() - started < 0.5

        [child] = service.receipt_for("root-1").children
        assert (child.kind, child.state) == ("reduce", "NOT_SENT")    # a proven refusal (R34)
        trader._main_loop = loop
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.run_until_complete(loop.run_in_executor(worker, lambda: None))   # a tick in flight has ended
        # The refused attempt never leaves late; the recovery loop may send a new attempt (R3).
        sent = [c for c in service.receipt_for("root-1").children if c.state != "NOT_SENT"]
        assert len(trader.orders) == len(sent) and all(c.attempt > 1 for c in sent)
    finally:
        _close_loop(loop)


def test_startup_session_recovery_flattens_while_loop_runs(tmp_path, worker, monkeypatch):
    clock = [FLATTEN_TIME]
    loop = asyncio.new_event_loop()
    try:
        trader = _FakeTrader(loop)
        liquidation = _liquidation(trader, tmp_path, resume=False, clock=clock)
        controller = _session_controller(tmp_path, liquidation, clock)
        monkeypatch.setattr(trader_service, "dt", SimpleNamespace(
            datetime=_FrozenDatetime, timezone=dt.timezone, timedelta=dt.timedelta))
        holder = SimpleNamespace(session_controller=controller)

        trader_service._maybe_start_session_recovery(holder, loop, worker)

        assert len(trader.orders) == 1
        assert liquidation.receipt_for(controller.flatten_command_id(ACCOUNT, SESSION_DATE)).state == "VERIFYING"
    finally:
        _close_loop(loop)


class _FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FLATTEN_TIME.astimezone(tz) if tz else FLATTEN_TIME


# ---------------------------------------------------------------------------
# A failed tick or startup call never stops the periodic loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watched_loop_logs_failed_tick_and_runs_the_next_one(monkeypatch):
    from trader.trading.liquidation_service import LiquidationBusy

    errors = []
    monkeypatch.setattr(trader_service.logging, "error", lambda msg, *args: errors.append(msg))
    calls = []

    async def busy_once():
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise LiquidationBusy("busy")

    loop_task = asyncio.create_task(trader_service._watched_ticks(
        "test", busy_once, interval=0.01, stuck_after=1.0))
    try:
        await asyncio.sleep(0.1)
        assert len(errors) == 1
        assert "tick failed" in errors[0] and "busy" in errors[0]
        assert len(calls) >= 2
        assert not loop_task.done()
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_watched_loop_logs_critical_once_when_busy_lasts_past_stuck_after(monkeypatch):
    """A lock holder wedged outside the worker (e.g. the ingest thread) makes
    every tick fail fast with LiquidationBusy; that must still reach CRITICAL."""
    from trader.trading.liquidation_service import LiquidationBusy

    critical = []
    monkeypatch.setattr(trader_service.logging, "critical",
                        lambda msg, *args: critical.append(msg % args))
    monkeypatch.setattr(trader_service.logging, "error", lambda *args: None)
    busy = {"on": True}

    async def tick():
        if busy["on"]:
            raise LiquidationBusy("busy")

    loop_task = asyncio.create_task(trader_service._watched_ticks(
        "test", tick, interval=0.01, stuck_after=0.3))
    try:
        await asyncio.sleep(0.05)
        assert critical == []
        await asyncio.sleep(0.6)
        assert len(critical) == 1
        assert "busy" in critical[0]
        busy["on"] = False
        await asyncio.sleep(0.1)
        busy["on"] = True
        await asyncio.sleep(0.6)
        assert len(critical) == 2
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


class _BusyOnce:
    """Raises LiquidationBusy on the first call, then returns ``result``."""
    def __init__(self, result):
        self.calls = 0
        self._result = result

    def __call__(self, *args):
        from trader.trading.liquidation_service import LiquidationBusy
        self.calls += 1
        if self.calls == 1:
            raise LiquidationBusy("busy")
        return self._result


def test_busy_startup_rescan_still_starts_the_recovery_loop(worker):
    loop = asyncio.new_event_loop()
    try:
        rescan = _BusyOnce(None)
        holder = SimpleNamespace(liquidation_service=SimpleNamespace(rescan=rescan))

        trader_service._maybe_start_liquidation_recovery(holder, loop, worker)
        assert rescan.calls == 1
        loop.run_until_complete(asyncio.sleep(0.1))

        assert rescan.calls >= 2
    finally:
        _close_loop(loop)


def test_busy_startup_recover_still_starts_the_session_loop(worker):
    loop = asyncio.new_event_loop()
    try:
        flat = SimpleNamespace(state="FLAT", session_date=SESSION_DATE, incident=None,
                               entry_cutoff_reached=True)
        recover, run_due = _BusyOnce(flat), _BusyOnce(flat)
        run_due.calls = 1  # run_due never raises
        holder = SimpleNamespace(session_controller=SimpleNamespace(recover=recover, run_due=run_due))

        trader_service._maybe_start_session_recovery(holder, loop, worker)
        assert recover.calls == 1
        loop.run_until_complete(asyncio.sleep(0.1))

        assert run_due.calls >= 2
    finally:
        _close_loop(loop)


# ---------------------------------------------------------------------------
# A shutdown signal during startup recovery stops startup cleanly
# ---------------------------------------------------------------------------

def _periodic_loop_tasks(loop):
    names = ("_liquidation_recovery_loop", "_session_controller_loop")
    return [t for t in asyncio.all_tasks(loop) if t.get_coro().__name__ in names]


def _stop_loop_during_call(loop, stopping, result=None):
    """A worker call during which shutdown sets the flag and stops the loop."""
    def call(*_args):
        stopping[0] = True
        loop.call_soon_threadsafe(loop.stop)
        time.sleep(0.1)
        return result
    return call


def _cancel_caller_during_call(loop, stopping, result=None):
    """A worker call whose awaiting task shutdown cancels."""
    def cancel_all():
        for task in asyncio.all_tasks(loop):
            task.cancel()

    def call(*_args):
        stopping[0] = True
        loop.call_soon_threadsafe(cancel_all)
        time.sleep(0.1)
        return result
    return call


@pytest.mark.parametrize("make_call", [_stop_loop_during_call, _cancel_caller_during_call])
def test_shutdown_during_startup_rescan_starts_no_recovery_loop(worker, make_call):
    loop = asyncio.new_event_loop()
    stopping = [False]
    try:
        holder = SimpleNamespace(liquidation_service=SimpleNamespace(
            rescan=make_call(loop, stopping)))

        trader_service._maybe_start_liquidation_recovery(
            holder, loop, worker, stopping=lambda: stopping[0])

        assert _periodic_loop_tasks(loop) == []
    finally:
        _close_loop(loop)


@pytest.mark.parametrize("make_call", [_stop_loop_during_call, _cancel_caller_during_call])
def test_shutdown_during_startup_recover_starts_no_session_loop(worker, make_call):
    loop = asyncio.new_event_loop()
    stopping = [False]
    try:
        holder = SimpleNamespace(session_controller=SimpleNamespace(
            recover=make_call(loop, stopping), run_due=lambda now: None))

        trader_service._maybe_start_session_recovery(
            holder, loop, worker, stopping=lambda: stopping[0])

        assert _periodic_loop_tasks(loop) == []
    finally:
        _close_loop(loop)


def test_main_returns_without_running_trader_when_signalled_during_startup_recovery(monkeypatch):
    import os
    import signal

    class _StartupTrader:
        def __init__(self):
            self.run_calls = 0
            self.shutdowns = 0
            self.recover_calls = 0
            self.liquidation_service = SimpleNamespace(rescan=self._rescan)
            self.session_controller = SimpleNamespace(recover=self._recover, run_due=self._recover)

        def _rescan(self):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
            return None

        def _recover(self, now):
            self.recover_calls += 1

        def connect(self):
            pass

        def run(self):
            self.run_calls += 1

        async def shutdown(self):
            self.shutdowns += 1

    fake = _StartupTrader()
    monkeypatch.setattr(trader_service, "Container", SimpleNamespace(
        create=lambda _config: SimpleNamespace(resolve=lambda *_a, **_k: fake)))
    monkeypatch.setattr(trader_service, "_seed_trading_control", lambda *_a: None)
    monkeypatch.setattr(trader_service, "_maybe_start_command_reconciliation", lambda *_a: None)

    try:
        trader_service.main.callback(simulation=False, debug=False, config="unused.yaml")
    finally:
        loop = asyncio.get_event_loop()
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        _close_loop(loop)
        asyncio.set_event_loop(None)

    assert fake.shutdowns == 1
    assert fake.run_calls == 0
    assert fake.recover_calls == 0


def test_orphan_reservation_sweep_starts_with_an_immediate_tick(worker):
    loop = asyncio.new_event_loop()
    try:
        calls = []
        saga = SimpleNamespace(retire_orphan_reservations=lambda: calls.append(1) or ())
        holder = SimpleNamespace(protective_order_saga=saga)

        trader_service._maybe_start_orphan_reservation_sweep(holder, loop, worker)
        (sweep,) = asyncio.all_tasks(loop)
        loop.run_until_complete(asyncio.sleep(0.15))
        sweep.cancel()

        assert calls == [1]
    finally:
        _close_loop(loop)


def test_no_orphan_reservation_sweep_without_a_saga(worker):
    loop = asyncio.new_event_loop()
    try:
        trader_service._maybe_start_orphan_reservation_sweep(SimpleNamespace(), loop, worker)

        assert asyncio.all_tasks(loop) == set()
    finally:
        _close_loop(loop)
