"""SP1 plan 1 Task 13 (R21): the safe close through the real ``build_command_stack``.

Real: DuckDB journal, exit owner registry, liquidation run store, worker,
protective saga, session controller, coordinator, reconciler, RiskGate,
``TradingRuntimeOrderDispatch`` and ``Trader.place_reduce_only_order`` on a
real asyncio loop. Fake: only the broker (``_BrokerSim`` plays IB and writes
promoted broker generations into the journal, as broker sync would) and, in
the SELL tests, the research-bundle verifier (signing a bundle needs a
research database; every close-path component stays real).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import threading
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import reactivex as rx

from trader.automation.protective_order_saga import BrokerOrderEvent, SagaState
from trader.automation.session_controller import SessionController
from trader.data.broker_state import BrokerAccountRow, BrokerOrderRow, BrokerPositionRow, BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import CommandRequest
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.liquidation_service import BrokerChangesBusy
from trader.trading.order_correlation import classify_leg, decode_order_ref
from trader.trading.risk_gate import RiskGate, RiskLimits
from trader.trading.trading_runtime import Trader

UTC = dt.timezone.utc
ACCOUNT = "DU111111"
CONID = 265598
OTHER = 4815747
FRIDAY = dt.date(2026, 7, 17)


def _et(hour, minute):
    from zoneinfo import ZoneInfo
    return dt.datetime(FRIDAY.year, FRIDAY.month, FRIDAY.day, hour, minute,
                       tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class _Ingest:
    """Production shape: ``is_ready`` is a property; a sync request promotes a generation."""
    def __init__(self, sim):
        self.sim = sim
        self.syncs = 0
        self.ready = True          # False: a newer generation is staging, the enumeration is not complete
        self.before_hold = None    # an ingest batch applied just before a close holds broker changes
        self.after_hold = None     # an IB update that lands right after a close lets the hold go
        self.holding = 0           # holds taken and not yet let go

    @property
    def is_ready(self):
        return self.ready

    @contextmanager
    def hold_changes(self):
        """Ruling 48: the sim writes only inside a sync, so holding is refusing while one is staging."""
        if self.before_hold is not None:
            self.before_hold()
        if not self.ready:
            raise BrokerChangesBusy("broker generation is staging")
        self.holding += 1
        try:
            yield
        finally:
            self.holding -= 1
            if self.after_hold is not None:
                self.after_hold()

    async def run_broker_sync(self, _client):
        self.syncs += 1
        if self.sim.auto_refresh:
            self.sim.promote()
        return True


_IB_DONE = ("Filled", "Cancelled", "ApiCancelled", "Inactive")   # ib_async OrderStatus.DoneStates


class _BrokerSim:
    """IB and the broker enumeration. Orders become visible only when promote() writes them."""

    def __init__(self, trader):
        self.trader = trader
        self.held: dict[int, float] = {}
        self.orders: dict[str, BrokerOrderRow] = {}
        self.perm: dict[str, int] = {}
        self.ib_trades: dict[str, SimpleNamespace] = {}   # ib_async keeps terminal trades for the session
        self.hidden: set[str] = set()
        self.placed: list[tuple] = []
        self.cancelled: list[str] = []
        self.ack = {"MKT": "Submitted", "STP": "PreSubmitted", "LMT": "Submitted"}
        self.daily_pnl = 0.0
        self.auto_refresh = False
        self.generations = 0
        self._next_perm = 9000

    # -- fake IB client ----------------------------------------------------------------
    def isConnected(self):
        return True

    def accountValues(self, account=None):
        return [SimpleNamespace(tag="NetLiquidation", currency="USD", account=ACCOUNT, value="100000")]

    def managedAccounts(self):
        return [ACCOUNT]

    def trades(self):
        return list(self.ib_trades.values())

    def openTrades(self):
        return [t for t in self.ib_trades.values() if t.orderStatus.status not in _IB_DONE]

    def positions(self, account=None):
        return [SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=c), position=q)
                for c, q in self.held.items() if q]

    def cancelOrder(self, order):
        entity = next(e for e, t in self.ib_trades.items() if t.order is order)
        self.cancelled.append(entity)

    # -- fake executioner ------------------------------------------------------------------
    async def subscribe_place_order_direct(self, contract, order):
        self._next_perm += 1
        order.orderId = order.permId = self._next_perm
        group = decode_order_ref(order.orderRef)
        leg = classify_leg(order.orderType, 0, order.orderId, group)
        entity = f"{group}:{leg}"
        price = {"STP": order.auxPrice, "LMT": order.lmtPrice}.get(order.orderType)
        self.placed.append((group, order.orderType, order.action, order.totalQuantity, price, order.ocaGroup or None))
        self.add_order(entity, group, leg, order.action, order.orderType, order.totalQuantity,
                       conid=int(contract.conId), order=order)
        echo = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PendingSubmit"))
        ack = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=self.ack[order.orderType]))
        return rx.from_iterable([echo, ack])

    # -- broker state ------------------------------------------------------------------------
    def add_order(self, entity, group, leg, action, order_type, quantity, *, conid=CONID, status="Submitted",
                  order=None):
        if order is None:
            self._next_perm += 1
            order = SimpleNamespace(permId=self._next_perm, action=action, totalQuantity=float(quantity), ocaGroup="")
        self.perm[entity] = order.permId
        self.ib_trades[entity] = SimpleNamespace(order=order, contract=SimpleNamespace(conId=conid),
                                                 orderStatus=SimpleNamespace(status=status, filled=0.0))
        self.orders[entity] = BrokerOrderRow(
            order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL", order_group_id=group,
            leg=leg, is_external=False, action=action, order_type=order_type, total_quantity=float(quantity),
            filled_quantity=0.0, avg_fill_price=None, limit_price=None, stop_price=None, tif="DAY",
            status=status, deleted=False, revision=1, source_timestamp=_et(11, 0),
            oca_group=getattr(order, "ocaGroup", "") or None,
            oca_type=getattr(order, "ocaType", 0) or None)

    def set_status(self, entity, status, *, filled=None, total=None):
        row = self.orders[entity]
        self.orders[entity] = replace(row, status=status,
                                      filled_quantity=row.filled_quantity if filled is None else float(filled),
                                      total_quantity=row.total_quantity if total is None else float(total))
        trade = self.ib_trades.get(entity)
        if trade is not None:
            trade.orderStatus.status = status
            if filled is not None:
                trade.orderStatus.filled = float(filled)

    def entity_for(self, group_prefix):
        return next(e for e in self.orders if e.startswith(group_prefix))

    def promote(self, started_at=None):
        store, db = self.trader.broker_state_store, self.trader.journal_db

        def write(conn):
            gid = store.open_generation_in_tx(conn, ("account",), started_at or _et(11, 0))
            store.upsert_account_in_tx(conn, BrokerAccountRow(
                ACCOUNT, "paper", 100_000.0, None, None, None, None,
                {"DailyPnL:USD": str(self.daily_pnl)}, 1, _et(11, 0)))
            for conid, quantity in self.held.items():
                store.upsert_position_in_tx(conn, BrokerPositionRow(
                    ACCOUNT, conid, "AAPL", "STK", "SMART", "USD", quantity, 90.0, 100.0, quantity * 100.0,
                    0.0, 0.0, 0.0, quantity == 0, 1, _et(11, 0)))
            for entity, row in self.orders.items():
                if entity in self.hidden:
                    continue
                store.upsert_order_in_tx(conn, row)
                store.bind_alias_in_tx(conn, "perm_id", str(self.perm[entity]), ACCOUNT, "", entity, _et(11, 0))
            cursor = conn.execute("SELECT COALESCE(MAX(source_cursor), 0) FROM domain_event_journal").fetchone()[0]
            store.mark_generation_promoted_in_tx(conn, gid, int(cursor), _et(11, 0))
            return gid
        self.generations += 1
        return db.transaction(write)


class _Universe:
    def resolve_symbol(self, conid, **_kwargs):
        if conid not in (CONID, OTHER):
            return []
        return [SimpleNamespace(conId=conid, symbol="AAPL", secType="STK", exchange="SMART",
                                primaryExchange="NASDAQ", currency="USD")]


class _Composed:
    def __init__(self, tmp_path, loop_thread, clock, *, automation=False, sim=None):
        from trader.trading.command_stack import build_command_stack

        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        store = BrokerStateStore(db)
        store.migrate(migrator)
        trader = object.__new__(Trader)
        self.trader, self.clock, self.loop_thread = trader, clock, loop_thread
        self.sim = sim or _BrokerSim(trader)
        self.sim.trader = trader
        trader.journal_db, trader.domain_journal, trader.broker_state_store = db, journal, store
        trader.broker_ingest = _Ingest(self.sim)
        trader.risk_gate = RiskGate(RiskLimits(max_daily_loss=1000.0),
                                    event_store=SimpleNamespace(count_since=lambda **_k: 0))
        trader.universe_accessor = _Universe()
        trader.portfolio = SimpleNamespace(get_positions=lambda: [], get_portfolio_items=lambda: [])
        trader.book = SimpleNamespace(get_open_order_count=lambda: 0)
        trader.client = SimpleNamespace(ib=self.sim)
        trader.executioner = self.sim
        trader.ib_account = ACCOUNT
        trader.paper_trading = True
        trader._main_loop = loop_thread.loop
        trader.get_pnl = lambda: [SimpleNamespace(dailyPnL=self.sim.daily_pnl)]
        if automation:
            _enable_automation(trader, tmp_path)

        async def no_margin(*_a):
            raise RuntimeError("what-if is not part of this test")
        trader.check_order_margin = no_margin
        self.stack = build_command_stack(trader, CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0),
                                         now=lambda: clock[0])
        self.liquidation = self.stack.liquidation_service
        self.saga = self.stack.protective_order_saga

    def tick(self):
        """One production recovery tick: a coroutine on the trader loop awaiting the worker."""
        return self.loop_thread.run(self.liquidation.tick_async())

    def run_session(self, at):
        self.clock[0] = at
        return self.loop_thread.run(self.liquidation.run_async(self.stack.session_controller.run_due, at))

    def protected_entry(self, *, quantity=10.0, stop=95.0, target=120.0, command_id="entry-1", conid=CONID):
        """A durable PROTECTED saga and its working stop and target, as after a filled bracket."""
        og = f"og-{command_id}"
        state = SagaState(
            command_id=command_id, order_group_id=og, order_ref=f"mmr:{og}", state="PROTECTED",
            account_id=ACCOUNT, conid=conid, side="BUY", requested_quantity=Decimal(str(quantity)),
            filled_quantity=Decimal(str(quantity)), protection_quantity=Decimal(str(quantity)),
            protection_working=True, stop_working=True, target_working=True, revision=3,
            plan_json={"legs": [{"role": "stop", "stop_price": str(stop)},
                                {"role": "take_profit", "limit_price": str(target)}]})
        self.saga._persist(state, self.clock[0], from_state=None)
        self.sim.held[conid] = quantity
        self.sim.add_order(f"{og}:stop", og, "stop", "SELL", "STP", quantity, conid=conid, status="PreSubmitted")
        self.sim.add_order(f"{og}:take_profit", og, "take_profit", "SELL", "LMT", quantity, conid=conid)
        return og

    def saga_event(self, og, leg, entity, status, *, event_id, filled=0.0):
        return self.saga.on_broker_event(BrokerOrderEvent(og, leg, status, filled, 10.0, 1, event_id,
                                                          self.clock[0], order_entity_id=entity))

    def cancel_landed(self, *entities):
        """The broker applied the cancels: rows Cancelled, open trades gone."""
        for entity in entities:
            self.sim.set_status(entity, "Cancelled")


def _enable_automation(trader, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from trader.research.signing import public_key_pem

    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    (keys / "verify.pem").write_bytes(public_key_pem(ed25519.Ed25519PrivateKey.generate().public_key()))
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    trader.automation_enabled, trader.automation_live_enabled = True, False
    trader.automation_public_key_ring_path = str(keys)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


@pytest.fixture
def composed(tmp_path, loop_thread):
    stack = _Composed(tmp_path, loop_thread, [_et(11, 0)])
    yield stack
    stack.liquidation.worker.shutdown()


@pytest.fixture
def automated(tmp_path, loop_thread):
    """Cold start with paper automation on, so the SELL close path is composed (R32)."""
    stack = _Composed(tmp_path, loop_thread, [_et(11, 0)], automation=True)
    yield stack
    stack.liquidation.worker.shutdown()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_cold_start_wires_registry_worker_saga_time_exit_reconciler_and_the_sell_close_path(automated):
    stack, trader = automated.stack, automated.trader
    assert trader.exit_owner_registry is stack.exit_owner_registry
    assert trader.liquidation_service is stack.liquidation_service
    assert trader.liquidation_worker is stack.liquidation_worker
    assert stack.session_controller._time_exit._liquidation is stack.liquidation_service
    assert automated.saga._liquidation._serialized is stack.liquidation_service
    assert stack.liquidation_service._service._protection is automated.saga
    assert stack.liquidation_service._service._refresh is not None
    assert stack.reconciler._closes is not None
    service = stack.automated_intent_service                # built on cold start (R32)
    assert service._liquidation is stack.liquidation_service and service._broker is not None


def test_hot_arm_builds_the_intent_service_with_the_close_path(tmp_path, composed):
    _enable_automation(composed.trader, tmp_path)
    service = composed.stack.paper_hot_arm._build_intent_service(composed.trader)
    assert service._liquidation is composed.stack.liquidation_service
    assert service._broker is not None


def test_the_stack_applies_the_sp1_migrations_and_keeps_attribution_on_32_to_34(composed):
    rows = dict(composed.trader.journal_db.execute("SELECT version, name FROM schema_migrations", fetch="all"))
    assert {35, 36, 37, 38} <= set(rows)
    assert all(rows[v].startswith("p3_") for v in (32, 33, 34))


# ---------------------------------------------------------------------------
# #23 / R10: two account producers, one root
# ---------------------------------------------------------------------------

def test_two_account_producers_make_one_root_one_reduce_and_release_the_owner(composed):
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    first = composed.liquidation.start(ACCOUNT, "flatten-ui-1", _et(15, 59))
    state = composed.run_session(_et(15, 46))
    assert state.flatten_command_id == "flatten-ui-1" == first.cause_command_id
    assert [p[1] for p in composed.sim.placed] == ["MKT"]
    composed.sim.set_status(composed.sim.entity_for("flatten-ui-1-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.run_session(_et(15, 47))
    composed.sim.promote()
    state = composed.run_session(_et(15, 48))                   # the session polls the root it got back
    assert (state.state, state.flatten_command_id) == ("FLAT", "flatten-ui-1")
    assert composed.stack.exit_owner_registry.get("flatten-ui-1").state == "RELEASED"
    assert [p[1] for p in composed.sim.placed] == ["MKT"]


# ---------------------------------------------------------------------------
# R11 / #26 / #31: exits after a loss breach; the time exit keeps no live stop
# ---------------------------------------------------------------------------

def test_after_a_loss_breach_a_time_exit_hands_over_cancels_the_stop_and_reduces(composed):
    """R11 + spec 5.1: the entry gate refuses; the exit hands protection over, cancels it, then reduces."""
    og = composed.protected_entry()
    composed.sim.daily_pnl = -5000.0
    composed.sim.promote()
    from ib_async import Contract
    entry = composed.loop_thread.run(composed.trader.place_expressive_order(
        Contract(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART", currency="USD"),
        "BUY", 5.0, {"order_type": "MARKET"}, algo_name="mmr:og-new"))
    assert "daily loss" in str(entry.error)
    composed.stack.session_controller._time_exit.request_exit(
        command_id="time-exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    owned = composed.saga.resume("entry-1")
    assert (owned.state, owned.expected_cancel_ids) == ("CLOSE_OWNED", (f"{og}:stop", f"{og}:take_profit"))
    assert sorted(composed.sim.cancelled) == [f"{og}:stop", f"{og}:take_profit"] and composed.sim.placed == []
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()
    assert composed.sim.placed == [("time-exit-1-reduce-265598-1", "MKT", "SELL", 10.0, None, None)]
    composed.sim.set_status(composed.sim.entity_for("time-exit-1-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.tick()
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("time-exit-1").state == "CLOSED"
    assert composed.saga.resume("entry-1").state == "CLOSED"
    assert composed.sim.openTrades() == []                       # no stop is left working
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


# ---------------------------------------------------------------------------
# #21: invisible child and late fill
# ---------------------------------------------------------------------------

def test_invisible_child_and_a_late_fill_never_send_a_second_reduce(composed):
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    entity = composed.sim.entity_for("c-1-reduce")
    composed.sim.hidden.add(entity)
    composed.sim.promote()
    composed.trader.broker_ingest.ready = False  # a newer sync is staging: no complete enumeration yet
    composed.tick()                              # child unseen and not provably absent: UNKNOWN
    assert composed.liquidation.receipt_for("c-1").children[0].state == "UNKNOWN"
    composed.trader.broker_ingest.ready = True
    composed.sim.hidden.clear()
    composed.sim.set_status(entity, "Filled", filled=10.0)
    composed.sim.promote()                       # fill visible, position not yet updated
    composed.tick()
    assert [p[0] for p in composed.sim.placed] == ["c-1-reduce-265598-1"]
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("c-1").state == "CLOSED"
    assert len(composed.sim.placed) == 1


def _real_ingest(composed):
    """The production ``BrokerIngest`` on the same journal: only its execDetails path is used here."""
    from trader.trading.broker_ingest import BrokerIngest
    return BrokerIngest(db=composed.trader.journal_db, journal=composed.trader.domain_journal,
                        store=composed.trader.broker_state_store, account_id=ACCOUNT, account_mode="paper",
                        session_epoch="s1", clock=lambda: composed.clock[0])


def _execution(composed, exec_id, perm_id, shares):
    """IB ``execDetails`` for a SELL of ``shares``; no order-status callback comes with it."""
    ingest = _real_ingest(composed)
    ingest.on_exec_details(None, SimpleNamespace(
        execution=SimpleNamespace(acctNumber=ACCOUNT, execId=exec_id, permId=perm_id, orderId=0, side="SLD",
                                  shares=shares, price=100.0, time=composed.clock[0]),
        contract=SimpleNamespace(conId=CONID)))
    assert ingest.drain_once() == 1


def test_an_execution_without_an_order_status_fences_the_next_reduce(composed):
    """#20 round 5: the order row says ``Cancelled`` with 0 filled; an execution of 4 arrives
    with no order-status change and the position still lags at 10. No reduce is sized on
    that generation; the next one sizes from the live 6."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    entity = composed.sim.entity_for("c-1-reduce")
    composed.sim.set_status(entity, "Cancelled", filled=0.0)
    composed.sim.promote()
    _execution(composed, "exec-1", composed.sim.perm[entity], 4.0)
    composed.tick()
    assert [p[0] for p in composed.sim.placed] == ["c-1-reduce-265598-1"]
    composed.sim.held[CONID] = 6.0
    composed.sim.promote()
    composed.tick()
    assert composed.sim.placed[1][0] == "c-1-reduce-265598-2" and composed.sim.placed[1][3] == 6.0


def test_a_late_execution_of_a_settled_child_fences_a_later_root(composed):
    """#20 round 5, the reviewer's trace: root 1 settles its reduce as ``Cancelled/0`` and ends;
    an execution for that order then arrives alone. A second root must not reduce on the
    same generation."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    entity = composed.sim.entity_for("c-1-reduce")
    composed.sim.set_status(entity, "Cancelled", filled=0.0)
    composed.sim.promote()
    composed.clock[0] = _et(11, 6)                 # past c-1's deadline: it decides on this evidence
    composed.tick()
    assert composed.liquidation.receipt_for("c-1").state == "FAILED_SAFE"
    _execution(composed, "exec-1", composed.sim.perm[entity], 4.0)
    composed.liquidation.start(ACCOUNT, "c-2", _et(11, 15), scope="conid", conid=CONID)
    assert [p[0] for p in composed.sim.placed] == ["c-1-reduce-265598-1"]
    composed.sim.held[CONID] = 6.0
    composed.sim.promote()
    composed.tick()
    assert composed.sim.placed[-1][0] == "c-2-reduce-265598-1" and composed.sim.placed[-1][3] == 6.0


def test_an_execution_no_order_claims_blocks_sizing_until_a_newer_sync(composed):
    """#20 round 5: an execution with no bound order fails closed. Sizing waits for a broker
    generation that started after the execution was recorded."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.clock[0] = _et(11, 1)
    _execution(composed, "exec-orphan", 777777, 4.0)
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 10), scope="conid", conid=CONID)
    assert composed.sim.placed == []
    composed.sim.held[CONID] = 6.0
    composed.sim.promote(started_at=_et(11, 2))
    composed.tick()
    assert composed.sim.placed == [("c-1-reduce-265598-1", "MKT", "SELL", 6.0, None, None)]


# ---------------------------------------------------------------------------
# #22 / #25 / spec 6: partial close of one of two protected positions
# ---------------------------------------------------------------------------

def test_partial_close_of_one_of_two_positions_reprotects_the_actual_remainder(composed):
    """Terminal partial fill (2 of 4), stop then target for the live 8, release, a late retired-leg event.
    The other position and its protection stay untouched; the breaker stays clear."""
    og = composed.protected_entry()
    other = composed.protected_entry(command_id="entry-2", conid=OTHER)
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    assert sorted(composed.sim.cancelled) == [f"{og}:stop", f"{og}:take_profit"]
    for leg in ("stop", "take_profit"):
        composed.cancel_landed(f"{og}:{leg}")
        composed.saga_event(og, leg, f"{og}:{leg}", "Cancelled", event_id=f"cancel-{leg}")
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    composed.sim.promote()
    composed.tick()                                                   # partial reduce of 4
    assert composed.sim.placed[-1][:4] == ("p-1-reduce-265598-1", "MKT", "SELL", 4.0)
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Cancelled", filled=2.0)
    composed.sim.held[CONID] = 8.0
    composed.sim.promote()
    composed.tick()                                                   # the partial fill is seen
    composed.sim.promote()
    composed.tick()                                                   # stop leg for the actual 8
    assert composed.sim.placed[-1] == ("p-1-reprotect-stop-265598-1", "STP", "SELL", 8.0, 95.0, "p-1-reprotect-265598-1")
    composed.sim.promote()
    composed.tick()                                                   # target after the stop is accepted
    assert composed.sim.placed[-1] == ("p-1-reprotect-target-265598-1", "LMT", "SELL", 8.0, 120.0, "p-1-reprotect-265598-1")
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("p-1").state == "DONE"
    released = composed.saga.resume("entry-1")
    assert (released.state, released.protection_quantity) == ("PROTECTED", Decimal("8"))
    late = composed.saga_event(og, "stop", f"{og}:stop", "Cancelled", event_id="late-original-stop")
    assert late.state == "PROTECTED"                                 # a retired leg, a new event id
    assert composed.saga.resume("entry-2").state == "PROTECTED"
    assert {f"{other}:stop", f"{other}:take_profit"} <= {e for e, t in composed.sim.ib_trades.items()
                                                          if t.orderStatus.status not in _IB_DONE}
    assert all(entity.startswith(og) for entity in composed.sim.cancelled)
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


def _partial_close_up_to_done(composed, after_done):
    """Partial close of 4 of 10: reduce filled, stop and target for the live 6 working.
    ``after_done`` runs as an ingest batch right after DONE commits, before the release hold."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()                                                   # reduce 4
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Filled", filled=4.0)
    composed.sim.held[CONID] = 6.0
    for _ in range(3):                                                # fill seen, stop, target
        composed.sim.promote()
        composed.tick()
    assert composed.sim.placed[-1][:4] == ("p-1-reprotect-target-265598-1", "LMT", "SELL", 6.0)
    fired = []

    def ingest_after_done():
        if not fired and composed.liquidation.receipt_for("p-1").state == "DONE":
            fired.append(1)
            after_done()
            composed.sim.promote()
    composed.trader.broker_ingest.before_hold = ingest_after_done
    composed.sim.promote()
    composed.tick()
    composed.trader.broker_ingest.before_hold = None
    assert fired and composed.liquidation.receipt_for("p-1").state == "DONE"


def test_a_stop_filled_at_release_with_the_position_still_open_is_a_safety_failure(composed):
    """#22/#25 round 8 (openai): after DONE and before the release hold the stop row turns Filled for 6,
    but the broker still holds +6. A filled stop protects nothing: the saga is SAFETY_FAILED and the
    breaker trips, never PROTECTED."""
    def stop_filled_position_open():
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-stop"), "Filled", filled=6.0)
    _partial_close_up_to_done(composed, stop_filled_position_open)
    released = composed.saga.resume("entry-1")
    assert released.state == "SAFETY_FAILED" and released.flatten_requested
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"


def test_a_safety_failure_at_release_queues_the_flatten_without_stalling_the_hold(composed):
    """#22/#25 round 8 follow-up: the saga trips inside the close's cleanup, which holds the
    liquidation lock and broker changes. Its start goes through ``nonblocking()``: it is queued on the
    one liquidation worker and runs after the tick, so nothing waits on the lock while ingest is held.
    The flatten root exists right after, and SAFETY_FAILED + flatten_requested are durable first,
    so a crash before the queued start is picked up by the next tick (``unhandled_failures``)."""
    import time

    def stop_filled_position_open():
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-stop"), "Filled", filled=6.0)
    started = time.monotonic()
    _partial_close_up_to_done(composed, stop_filled_position_open)
    composed.liquidation.worker.submit(lambda: None).result(timeout=2.0)   # the queued start has run
    assert time.monotonic() - started < 2.0
    assert composed.trader.broker_ingest.holding == 0
    assert composed.saga.resume("entry-1").state == "SAFETY_FAILED"
    assert composed.liquidation.root_for("entry-1") is not None


def test_a_stop_filled_at_release_that_closed_the_remainder_is_closed_without_an_alarm(composed):
    """#22/#25 round 8: the stop really sold the released 6, the broker is flat and OCA cancelled the
    target. Spec 5.1: a zero position closed by an exit is a close. The saga ends CLOSED and the
    breaker stays clear."""
    def stop_closed_the_remainder():
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-stop"), "Filled", filled=6.0)
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-target"), "Cancelled")
        composed.sim.held[CONID] = 0.0
    _partial_close_up_to_done(composed, stop_closed_the_remainder)
    assert composed.saga.resume("entry-1").state == "CLOSED"
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


@pytest.mark.parametrize("target_status", ["PendingCancel", "ApiPending"])
def test_a_stop_filled_at_release_next_to_a_live_target_is_a_safety_failure(composed, target_status):
    """#22/#25 round 9 (openai): the stop sold the released 6 and the broker is flat, but the target is
    not broker-proven terminal. PendingCancel (or any other non-terminal status) can still fill and
    open a short. The release takes the safety path: SAFETY_FAILED, breaker tripped, flatten queued.
    A later target fill (-6) never turns the saga CLOSED."""
    def stop_closed_the_remainder_target_live():
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-stop"), "Filled", filled=6.0)
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-target"), target_status)
        composed.sim.held[CONID] = 0.0
    _partial_close_up_to_done(composed, stop_closed_the_remainder_target_live)
    composed.liquidation.worker.submit(lambda: None).result(timeout=2.0)   # the queued start has run
    released = composed.saga.resume("entry-1")
    assert released.state == "SAFETY_FAILED" and released.flatten_requested
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"
    assert composed.liquidation.root_for("entry-1") is not None
    composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-target"), "Filled", filled=6.0)
    composed.sim.held[CONID] = -6.0
    composed.sim.promote()
    composed.tick()
    assert composed.saga.resume("entry-1").state == "SAFETY_FAILED"
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"


def _release_safety_flatten_to_flat(composed):
    """Round 10 (openai): stop Filled/6, target PendingCancel, broker flat -> SAFETY_FAILED and the
    flatten; then the target reports Cancelled/0 and the flatten ends FLAT. Returns the target entity."""
    def stop_closed_the_remainder_target_pending_cancel():
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-stop"), "Filled", filled=6.0)
        composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-target"), "PendingCancel")
        composed.sim.held[CONID] = 0.0
    _partial_close_up_to_done(composed, stop_closed_the_remainder_target_pending_cancel)
    composed.liquidation.worker.submit(lambda: None).result(timeout=2.0)   # the queued start has run
    target = composed.sim.entity_for("p-1-reprotect-target")
    composed.sim.set_status(target, "Cancelled", filled=0.0)
    for _ in range(6):
        composed.sim.promote()
        composed.tick()
    flatten = composed.liquidation.root_for("entry-1")
    assert composed.liquidation.receipt_for(flatten).state == "FLAT"
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"
    return target


def _late_roots(composed):
    return [r[0] for r in composed.trader.journal_db.execute(
        "SELECT cause_command_id FROM liquidation_runs WHERE cause_command_id LIKE '%-late-%' "
        "ORDER BY cause_command_id", fetch="all")]


def _late_target_fill(composed, target):
    """The target that reported Cancelled/0 fills 6 after all: the account is short 6."""
    composed.sim.set_status(target, "Filled", filled=6.0)
    composed.sim.held[CONID] = -6.0


def test_a_late_fill_after_a_flat_flatten_starts_a_new_safety_close(composed):
    """Round 10 (openai), the exact order: after FLAT a bound fill of the cancelled target makes
    the account short 6. A new root linked to the flatten (``entry-1-late-1``) buys 6 back through
    the reduce-only boundary; the old root stays FLAT; the breaker stays tripped."""
    target = _release_safety_flatten_to_flat(composed)
    placed = len(composed.sim.placed)
    _late_target_fill(composed, target)
    for _ in range(3):
        composed.sim.promote()
        composed.tick()
    assert _late_roots(composed) == ["entry-1-late-1"]
    assert composed.sim.placed[placed:] == [("entry-1-late-1-reduce-265598-1", "MKT", "BUY", 6.0, None, None)]
    assert composed.liquidation.receipt_for("entry-1").state == "FLAT"
    composed.sim.set_status(composed.sim.entity_for("entry-1-late-1-reduce"), "Filled", filled=6.0)
    composed.sim.held[CONID] = 0.0
    for _ in range(2):
        composed.sim.promote()
        composed.tick()
    assert composed.liquidation.receipt_for("entry-1-late-1").state == "FLAT"
    assert len(composed.sim.placed) == placed + 1
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"


def test_no_new_close_while_the_account_stays_flat_or_a_new_entry_has_no_late_fill(composed):
    """No bound late fill, no new root: a flat account stays alone, and a later position that no
    settled child explains (a new entry) is never closed by this path."""
    _release_safety_flatten_to_flat(composed)
    placed = len(composed.sim.placed)
    for _ in range(3):
        composed.sim.promote()
        composed.tick()
    composed.sim.held[CONID] = 10.0
    for _ in range(3):
        composed.sim.promote()
        composed.tick()
    assert _late_roots(composed) == []
    assert len(composed.sim.placed) == placed


def test_a_late_fill_seen_on_many_ticks_starts_exactly_one_new_close(composed):
    target = _release_safety_flatten_to_flat(composed)
    placed = len(composed.sim.placed)
    _late_target_fill(composed, target)
    composed.tick()                                    # seen before the position moved
    for _ in range(6):
        composed.sim.promote()
        composed.tick()
    assert _late_roots(composed) == ["entry-1-late-1"]
    assert [p[2:4] for p in composed.sim.placed[placed:]] == [("BUY", 6.0)]


def test_a_restart_between_flat_and_the_late_fill_still_starts_the_new_close(tmp_path, composed):
    target = _release_safety_flatten_to_flat(composed)
    placed = len(composed.sim.placed)
    restarted = _restart(composed, tmp_path)
    _late_target_fill(composed, target)
    for _ in range(3):
        composed.sim.promote()
        restarted.tick()
    assert _late_roots(restarted) == ["entry-1-late-1"]
    assert composed.sim.placed[placed:] == [("entry-1-late-1-reduce-265598-1", "MKT", "BUY", 6.0, None, None)]
    restarted.liquidation.worker.shutdown()


def test_a_stop_fill_after_the_final_admission_hold_is_refused_at_the_order_boundary(composed):
    """#22 round 8 (openai): the target's last admission hold read +6 and a stop with 6 outstanding.
    Right after that hold the stop fills 2: IB's order status has it, the position cache and the
    journal do not yet. The reduce-only boundary counts the OCA pair once (stop outstanding 4 != 6),
    so the target of 6 is refused in the loop step that would place it: nothing sent, the target is
    PLANNED again, no escalation. Once the fill is promoted the next tick sends a target of 4."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()                                                   # reduce 4
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Filled", filled=4.0)
    composed.sim.held[CONID] = 6.0
    for _ in range(2):                                                # fill seen, stop for 6
        composed.sim.promote()
        composed.tick()
    stop = composed.sim.entity_for("p-1-reprotect-stop")
    holds = []

    def stop_fills_2_after_the_final_hold():
        holds.append(1)
        if len(holds) == 2:                                           # _target_still_admitted
            composed.sim.ib_trades[stop].orderStatus.filled = 2.0
    composed.trader.broker_ingest.after_hold = stop_fills_2_after_the_final_hold
    composed.sim.promote()
    composed.tick()
    composed.trader.broker_ingest.after_hold = None
    assert len(holds) == 2
    assert not any(p[0] == "p-1-reprotect-target-265598-1" for p in composed.sim.placed)
    receipt = composed.liquidation.receipt_for("p-1")
    assert next(c for c in receipt.children if c.kind == "reprotect-target").state == "PLANNED"
    assert not receipt.escalated and composed.stack.circuit_breaker.store.get().state == "CLEAR"
    composed.sim.set_status(stop, "Submitted", filled=2.0)
    composed.sim.held[CONID] = 4.0
    for _ in range(3):
        composed.sim.promote()
        composed.tick()
    targets = [p for p in composed.sim.placed if p[0] == "p-1-reprotect-target-265598-1"]
    assert [p[3] for p in targets] == [4.0]


def test_a_stop_filled_after_the_final_admission_hold_blocks_the_target_at_the_order_boundary(composed):
    """#22 round 9 (openai): right after the target's last admission hold the stop fills all 6.
    ib_async drops it from openTrades(), while the position cache still shows +6. A target sent now
    joins a group whose stop already filled, so OCA cannot shrink it: it could sell 6 more. The
    boundary finds the stop through ib.trades() (terminal trades included), sees Filled and refuses:
    nothing sent, the target is PLANNED again. Once the flat position is seen the close ends CLOSED
    (spec 5.1: the stop closed the remainder) and no target is ever placed."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()                                                   # reduce 4
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Filled", filled=4.0)
    composed.sim.held[CONID] = 6.0
    for _ in range(2):                                                # fill seen, stop for 6
        composed.sim.promote()
        composed.tick()
    stop = composed.sim.entity_for("p-1-reprotect-stop")
    holds = []

    def stop_fills_after_the_final_hold():
        holds.append(1)
        if len(holds) == 2:                                           # _target_still_admitted
            composed.sim.set_status(stop, "Filled", filled=6.0)
    composed.trader.broker_ingest.after_hold = stop_fills_after_the_final_hold
    composed.sim.promote()
    composed.tick()
    composed.trader.broker_ingest.after_hold = None
    assert len(holds) == 2
    assert not any(p[0] == "p-1-reprotect-target-265598-1" for p in composed.sim.placed)
    receipt = composed.liquidation.receipt_for("p-1")
    assert next(c for c in receipt.children if c.kind == "reprotect-target").state == "PLANNED"
    composed.sim.held[CONID] = 0.0
    for _ in range(3):
        composed.sim.promote()
        composed.tick()
    assert not any(p[0] == "p-1-reprotect-target-265598-1" for p in composed.sim.placed)
    assert composed.liquidation.receipt_for("p-1").state == "CLOSED"           # the stop closed the 6
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


def test_target_leg_rejected_by_the_broker_escalates_to_a_full_close(composed):
    """#26 through the stack: a target that goes Inactive after its echo is a failed re-protect."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Filled", filled=4.0)
    composed.sim.held[CONID] = 6.0
    composed.sim.promote()
    composed.tick()
    composed.sim.promote()
    composed.tick()                                        # stop leg
    composed.sim.ack["LMT"] = "Inactive"
    composed.sim.promote()
    composed.tick()                                        # target sent; the ack says Inactive
    composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-target"), "Inactive")
    composed.sim.promote()
    composed.tick()
    receipt = composed.liquidation.receipt_for("p-1")
    assert receipt.escalated is True and receipt.goal == "zero"
    assert composed.sim.cancelled[-1] == composed.sim.entity_for("p-1-reprotect-stop")
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"


# ---------------------------------------------------------------------------
# R20 / #19 / #20 / #24: restarts through the stack
# ---------------------------------------------------------------------------

def _restart(composed, tmp_path):
    """Stop the old worker, build a new stack over the same journal and the same broker."""
    composed.liquidation.worker.shutdown()
    return _Composed(tmp_path, composed.loop_thread, composed.clock, sim=composed.sim)


def test_crash_between_journal_and_broker_call_restarts_without_a_duplicate(tmp_path, composed):
    """The reduce child was journaled; the process died before the broker call. The restart
    proves it absent on a complete newer generation and sends one reduce, attempt 2."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()

    class _Crash(BaseException):
        pass

    def crash(*_args, **_kwargs):
        raise _Crash()
    composed.stack.liquidation_service._service._still_dispatchable = crash
    with pytest.raises(_Crash):
        composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    restarted = _restart(composed, tmp_path)
    composed.sim.promote()
    restarted.tick()                                       # fenced on the restart's newest generation
    composed.sim.promote()
    restarted.tick()                                       # complete and newer: proven absent
    composed.sim.promote()
    restarted.tick()                                       # newer than that observation: reduce
    assert composed.sim.placed == [("c-1-reduce-265598-2", "MKT", "SELL", 10.0, None, None)]
    restarted.liquidation.worker.shutdown()


def test_restart_after_the_order_left_continues_without_a_second_order(tmp_path, composed):
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    restarted = _restart(composed, tmp_path)
    composed.sim.set_status(composed.sim.entity_for("c-1-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    restarted.tick()
    composed.sim.promote()
    restarted.tick()
    assert restarted.liquidation.receipt_for("c-1").state == "CLOSED"
    assert len(composed.sim.placed) == 1
    assert restarted.stack.exit_owner_registry.get("c-1").state == "RELEASED"
    restarted.liquidation.worker.shutdown()


# ---------------------------------------------------------------------------
# #27: the session cancel phase, the flatten and its exact root
# ---------------------------------------------------------------------------

def test_session_cancel_keeps_protection_and_the_flatten_owns_it_until_flat(composed):
    og = composed.protected_entry()
    composed.sim.add_order("og-entry-2:entry", "og-entry-2", "entry", "BUY", "LMT", 5.0)
    composed.sim.promote()
    composed.run_session(_et(15, 35))
    assert composed.sim.cancelled == ["og-entry-2:entry"]
    assert composed.saga.resume("entry-1").state == "PROTECTED"
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"
    composed.cancel_landed("og-entry-2:entry")
    composed.sim.promote()
    state = composed.run_session(_et(15, 46))
    root = SessionController.flatten_command_id(ACCOUNT, FRIDAY)
    assert state.flatten_command_id == root
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.run_session(_et(15, 47))                                 # the flatten reduces
    composed.sim.set_status(composed.sim.entity_for(f"{root}-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.run_session(_et(15, 48))
    composed.sim.promote()
    state = composed.run_session(_et(15, 49))
    assert (state.state, state.flatten_command_id) == ("FLAT", root)
    assert composed.saga.resume("entry-1").state == "CLOSED"


# ---------------------------------------------------------------------------
# #28 / R17 / R33: commands resolve from their root, through the reconciler only
# ---------------------------------------------------------------------------

def test_joined_flatten_command_resolves_only_through_the_reconciler(composed):
    """#28: the /flatten command that joined the session flatten resolves when FLAT is proven."""
    _register_production_actions(composed)
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    root = composed.run_session(_et(15, 46)).flatten_command_id
    receipt = composed.stack.coordinator.execute(CommandRequest(
        command_id="flatten-ui-1", action="liquidate_account", account_id=ACCOUNT, target_type="account",
        target_id=ACCOUNT, expected_version=None, body={"reason": "test"}, source="dashboard"))
    assert receipt.outcome["close_root_id"] == root
    composed.sim.set_status(composed.sim.entity_for(f"{root}-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.tick()
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for(root).state == "FLAT"
    assert composed.stack.ledger.get("flatten-ui-1").state == "OUTCOME_UNKNOWN"   # the close never resolves it
    composed.stack.reconciler.run_due(composed.clock[0])
    assert composed.stack.ledger.get("flatten-ui-1").state == "RESOLVED"
    assert composed.stack.ledger.unresolved_for_account(ACCOUNT) == []
    assert composed.stack.exit_owner_registry.account_owner(ACCOUNT) is None


def _register_production_actions(composed):
    """The production RPC registry registers the coordinator actions (liquidate_account, intents)."""
    from trader.messaging.production_api import build_production_registry
    from trader.domain.feed_service import DomainFeedService
    from trader.domain.snapshot_service import DomainSnapshotService
    from trader.messaging.typed_rpc import HmacServiceAuthenticator

    build_production_registry(
        composed.trader, HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        snapshot_service=DomainSnapshotService(composed.trader.domain_journal),
        feed_service=DomainFeedService(composed.trader.domain_journal), command_stack=composed.stack)


def _sell(automated, *, requested=None):
    """A SELL intent through the real coordinator and intent service; only the bundle check is faked."""
    from tests.automation.test_automated_command_boundary import (
        FakeArtifactVerifier, intent_to_request_body, make_intent,
    )
    _register_production_actions(automated)
    service = automated.stack.automated_intent_service
    service._verifier = FakeArtifactVerifier()
    service._bundle_evidence_validator = None
    intent = make_intent(side="SELL", requested_quantity=None if requested is None else Decimal(str(requested)))
    receipt = automated.stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent, bundle_digest="sha256:manifest-ok"), source="strategy_service"))
    return receipt, intent


def test_a_sell_intent_closes_end_to_end_and_resolves_from_its_root(automated):
    """R21 / #28: SELL -> scoped close -> reduce-only order -> CLOSED -> reconciler RESOLVED."""
    automated.sim.held[CONID] = 10.0
    automated.sim.promote()
    receipt, intent = _sell(automated)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert automated.sim.placed == [(f"{intent.command_id}-reduce-265598-1", "MKT", "SELL", 10.0, None, None)]
    automated.sim.set_status(automated.sim.entity_for(f"{intent.command_id}-reduce"), "Filled", filled=10.0)
    automated.sim.held[CONID] = 0.0
    automated.sim.promote()
    automated.tick()
    automated.sim.promote()
    automated.tick()
    assert automated.liquidation.receipt_for(intent.command_id).state == "CLOSED"
    automated.stack.reconciler.run_due(automated.clock[0])
    row = automated.stack.ledger.get(intent.command_id)
    assert (row.state, row.outcome["liquidation_state"]) == ("RESOLVED", "CLOSED")


def test_a_partial_sell_that_sells_nothing_is_protected_again_and_rejected(automated):
    """R25 / R2-2: the reduce is rejected; protection comes back; the command fails with an alert."""
    og = automated.protected_entry()
    automated.sim.promote()
    receipt, intent = _sell(automated, requested=4)
    root = intent.command_id
    automated.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    automated.sim.promote()
    automated.tick()                                                   # partial reduce of 4
    automated.sim.set_status(automated.sim.entity_for(f"{root}-reduce"), "Inactive")
    automated.sim.promote()
    automated.tick()                                                   # rejected: re-protect the untouched 10
    assert automated.sim.placed[-1][1:4] == ("STP", "SELL", 10.0)
    for _ in range(3):
        automated.sim.promote()
        automated.tick()                                               # target, then both legs working
    assert automated.liquidation.receipt_for(root).state == "REDUCE_FAILED"
    assert automated.saga.resume("entry-1").state == "PROTECTED"
    automated.stack.reconciler.run_due(automated.clock[0])
    row = automated.stack.ledger.get(root)
    assert (row.state, row.error_code) == ("REJECTED", "REDUCE_FAILED")


def test_a_waiting_close_asks_for_a_broker_sync_through_the_trader_loop(composed):
    """Ruling 1 in composition: the wait triggers run_broker_sync, which promotes a newer generation."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.sim.auto_refresh = True
    before = composed.sim.generations
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    composed.loop_thread.run(asyncio.sleep(0.05))
    assert composed.trader.broker_ingest.syncs == 1
    assert composed.sim.generations == before + 1


def test_generation_refresh_is_rate_limited_and_never_blocks():
    """Ruling 1: the close asks for a newer broker generation while it waits."""
    from trader.trading.command_stack import _BrokerGenerationRefresh

    loop_thread = _LoopThread()
    try:
        syncs, tick = [], [100.0]

        async def sync(client):
            syncs.append(client)
            return True
        trader = SimpleNamespace(_main_loop=loop_thread.loop, client="ib",
                                 broker_ingest=SimpleNamespace(run_broker_sync=sync))
        refresh = _BrokerGenerationRefresh(trader, min_interval_seconds=5.0, clock=lambda: tick[0])
        refresh.request_refresh(ACCOUNT)
        refresh.request_refresh(ACCOUNT)          # inside 5 s: skipped
        tick[0] = 106.0
        refresh.request_refresh(ACCOUNT)
        loop_thread.run(asyncio.sleep(0.05))
        assert syncs == ["ib", "ib"]
        trader._main_loop = None
        refresh.request_refresh(ACCOUNT)          # no loop: nothing, no error
    finally:
        loop_thread.stop()
