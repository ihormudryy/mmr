"""SP1 composed-system fixtures (Plan 6 Task 1).

Moved from ``tests/test_safe_close_integration.py`` (Plan 1 Task 13) so the
safe-close tests, Plan 3's cutoff tests and the Plan 6 acceptance tests share one
composed stack. Real: DuckDB journal, ``build_command_stack``, the liquidation
worker, the protective saga, the session controller, the coordinator, the
reconciler and ``TradingRuntimeOrderDispatch`` on a real asyncio loop. Fake:
only the broker (``BrokerSim`` plays IB and writes promoted broker generations,
as broker sync would) and, with ``ai_paper=True``, the quote and what-if
authorities, which read the simulator's quote table.

``ServedStack`` serves the production registry over typed RPC on loopback
sockets, signed with in-memory identities, so every acceptance call is a real
signed round trip.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import threading
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional
from unittest import mock

import reactivex as rx

from trader.automation.protective_order_saga import BrokerOrderEvent, SagaState
from trader.data.broker_state import BrokerAccountRow, BrokerOrderRow, BrokerPositionRow, BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.liquidation_service import BrokerChangesBusy
from trader.trading.order_correlation import classify_leg, decode_order_ref
from trader.trading.risk_gate import RiskGate, RiskLimits
from trader.trading.trading_runtime import Trader

UTC = dt.timezone.utc
ACCOUNT = "DU111111"
CONID = 265598           # AAPL
OTHER = 4815747          # NVDA in Plan 1's tests (resolved as "AAPL" there, unchanged)
MSFT = 272093
FRIDAY = dt.date(2026, 7, 17)
SYMBOLS = {CONID: "AAPL", OTHER: "AAPL", MSFT: "MSFT"}


def et(hour, minute, second=0):
    from zoneinfo import ZoneInfo
    return dt.datetime(FRIDAY.year, FRIDAY.month, FRIDAY.day, hour, minute, second,
                       tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class Ingest:
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


IB_DONE = ("Filled", "Cancelled", "ApiCancelled", "Inactive")   # ib_async OrderStatus.DoneStates
_WORKING = ("Submitted", "PreSubmitted", "PendingSubmit", "ApiPending", "PendingCancel")


class BrokerSim:
    """IB and the broker enumeration. Orders become visible only when promote() writes them.

    Race hooks (Plan 6 Task 1) only change what ``promote()`` writes; they never call the trader.
    With ``auto_fill`` the sim also plays the market: marketable orders fill at the quote, cancels
    land, bracket children follow their parent and an OCA type 2 sibling shrinks on a partial fill.
    """

    def __init__(self, trader):
        self.trader = trader
        self.held: dict[int, float] = {}
        self.orders: dict[str, BrokerOrderRow] = {}
        self.perm: dict[str, int] = {}
        self.ib_trades: dict[str, SimpleNamespace] = {}   # ib_async keeps terminal trades for the session
        self.hidden: set[str] = set()
        self.placed: list[tuple] = []
        self.cancelled: list[str] = []
        self.modified: list = []                         # Orders sent with an existing orderId (a modify)
        self.ack = {"MKT": "Submitted", "STP": "PreSubmitted", "LMT": "Submitted"}
        self.daily_pnl = 0.0
        self.net_liquidation = 100_000.0
        self.auto_refresh = False
        self.generations = 0
        self.now = lambda: et(11, 0)
        self.saga = None                                 # set: the sim notifies the saga like broker ingest
        self.quotes: dict[int, tuple[float, float]] = {}
        self._next_perm = 9000
        self._auto_fill: Optional[tuple[str, ...]] = None
        self._pending_cancel: dict[str, bool] = {}
        self._fill_after_cancel: dict[str, float] = {}
        self._landed: set[str] = set()
        self._target_script: Optional[list[float]] = None
        self._staged_generation: Optional[int] = None
        self._modify_clears_oca = False
        self._never_close: set[int] = set()
        self._changes: list[tuple] = []                  # (entity, row) per change, in arrival order
        from trader.data.broker_order_events import record_order_event_in_tx
        self.record_order_event_in_tx = record_order_event_in_tx   # what broker ingest appends (Plan 6 Task 2)
        self._notified: dict[str, tuple] = {}
        self._fills: list = []                           # executions not yet written (broker_fills)
        self._exec_seq = 0

    # -- fake IB client ----------------------------------------------------------------
    def isConnected(self):
        return True

    def accountValues(self, account=None):
        return [SimpleNamespace(tag="NetLiquidation", currency="USD", account=ACCOUNT,
                                value=str(self.net_liquidation))]

    def managedAccounts(self):
        return [ACCOUNT]

    def trades(self):
        return list(self.ib_trades.values())

    def openTrades(self):
        return [t for t in self.ib_trades.values() if t.orderStatus.status not in IB_DONE]

    def positions(self, account=None):
        return [SimpleNamespace(account=ACCOUNT, avgCost=100.0, position=q,
                                contract=SimpleNamespace(conId=c, symbol=SYMBOLS.get(c, "AAPL"), secType="STK",
                                                         currency="USD", exchange="SMART"))
                for c, q in self.held.items() if q]

    def cancelOrder(self, order):
        entity = next(e for e, t in self.ib_trades.items() if t.order is order)
        self.cancelled.append(entity)

    def placeOrder(self, contract, order):
        """ib_async: an existing orderId is a whole-order replace (a modify)."""
        entity = next(e for e, t in self.ib_trades.items() if t.order.orderId == order.orderId)
        self.modified.append(order)
        trade = self.ib_trades[entity]
        trade.order = order
        row = self.orders[entity]
        oca_group, oca_type = (None, None) if self._modify_clears_oca else (
            order.ocaGroup or None, order.ocaType or None)
        if self._modify_clears_oca:
            order.ocaGroup, order.ocaType = "", 0
        self.orders[entity] = replace(row, oca_group=oca_group, oca_type=oca_type)
        self._apply_live(entity)
        return trade

    def _apply_live(self, entity):
        """IB echoes a modify at once (openOrder/orderStatus); ingest applies it without a new generation."""
        store, db = self.trader.broker_state_store, self.trader.journal_db
        row, now = self.orders[entity], self.now()

        def write(conn):
            store.upsert_order_in_tx(conn, row)
            self.record_order_event_in_tx(conn, store.latest_promoted_generation_in_tx(conn), entity,
                                          self.perm.get(entity), self.ib_trades[entity].order.orderId, row, now)
        db.transaction(write)

    # -- fake executioner ------------------------------------------------------------------
    async def subscribe_place_order_direct(self, contract, order):
        self._next_perm += 1
        order.orderId = order.permId = self._next_perm
        group = decode_order_ref(order.orderRef)
        leg = classify_leg(order.orderType, int(getattr(order, "parentId", 0) or 0), order.orderId, group)
        entity = f"{group}:{leg}"
        price = {"STP": order.auxPrice, "LMT": order.lmtPrice}.get(order.orderType)
        self.placed.append((group, order.orderType, order.action, order.totalQuantity, price, order.ocaGroup or None))
        self.add_order(entity, group, leg, order.action, order.orderType, order.totalQuantity,
                       conid=int(contract.conId), order=order)
        if getattr(self, "_hide_next", False):
            self._hide_next = False
            self.hidden.add(entity)
        echo = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PendingSubmit"))
        ack = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=self.ack[order.orderType]))
        return rx.from_iterable([echo, ack])

    # -- broker state ------------------------------------------------------------------------
    def add_order(self, entity, group, leg, action, order_type, quantity, *, conid=CONID, status="Submitted",
                  order=None):
        if order is None:
            self._next_perm += 1
            order = SimpleNamespace(permId=self._next_perm, orderId=self._next_perm, action=action,
                                    totalQuantity=float(quantity), ocaGroup="", ocaType=0, parentId=0,
                                    orderType=order_type, lmtPrice=None, auxPrice=None, displaySize=0)
        self.perm[entity] = order.permId
        self.ib_trades[entity] = SimpleNamespace(order=order, contract=SimpleNamespace(conId=conid),
                                                 orderStatus=SimpleNamespace(status=status, filled=0.0))
        self.orders[entity] = BrokerOrderRow(
            order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol=SYMBOLS.get(conid, "AAPL"),
            order_group_id=group, leg=leg, is_external=False, action=action, order_type=order_type,
            total_quantity=float(quantity), filled_quantity=0.0, avg_fill_price=None,
            limit_price=None, stop_price=None, tif="DAY",
            status=status, deleted=False, revision=1, source_timestamp=et(11, 0),
            oca_group=getattr(order, "ocaGroup", "") or None,
            oca_type=getattr(order, "ocaType", 0) or None)
        self._changed(entity)

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
            if total is not None:
                trade.order.totalQuantity = float(total)
        self._changed(entity)

    def _changed(self, entity):
        """Each change is one broker callback: the row as it was at that moment, in order."""
        self._changes.append((entity, self.orders[entity]))

    def entity_for(self, group_prefix):
        return next(e for e in self.orders if e.startswith(group_prefix))

    # -- race hooks (Plan 6 Task 1) ------------------------------------------------------------
    def fill_after_cancel(self, entity, quantity):
        """The next promote() after the cancel shows the order Filled for ``quantity`` instead."""
        self._fill_after_cancel[entity] = float(quantity)

    def pending_cancel(self, entity, *, lands=True):
        """Once a cancel is sent: PendingCancel at the next promote(); Cancelled one promote later, or never."""
        self._pending_cancel[entity] = lands

    def hide(self, entity):
        self.hidden.add(entity)

    def reveal(self, entity):
        self.hidden.discard(entity)

    def stage_generation(self):
        """A newer generation opens and stays staging until the next promote() (capture: GENERATION_STAGING)."""
        store, db = self.trader.broker_state_store, self.trader.journal_db
        if self._staged_generation is None:
            self._staged_generation = db.transaction(
                lambda conn: store.open_generation_in_tx(conn, ("account",), self.now()))
        return self._staged_generation

    def reconnect(self):
        """The IB reconnect shape: not ready, a staging generation, then a full re-enumeration."""
        self.trader.broker_ingest.ready = False
        self.stage_generation()

    def auto_fill(self, order_types=("LMT", "MKT")):
        self._auto_fill = tuple(order_types)

    def quote(self, conid, bid, ask):
        self.quotes[int(conid)] = (float(bid), float(ask))

    def bid(self, conid):
        return self.quotes[int(conid)][0]

    def script_target_fills(self, slices):
        """A target with a display size fills in these slices, one slice per promote(), then stops."""
        self._target_script = [float(s) for s in slices]

    def modify_clears_oca(self):
        self._modify_clears_oca = True

    def hide_next_child(self):
        """The next order placed stays invisible to every generation (a submitted child IB has not shown)."""
        self._hide_next = True

    def never_close(self, conid):
        """Market orders on ``conid`` are acknowledged and never fill."""
        self._never_close.add(int(conid))

    def working_order(self, leg, conid=CONID):
        for entity, row in self.orders.items():
            if (row.conid == conid and row.leg == leg and row.status in _WORKING
                    and entity in self.ib_trades):
                return self.ib_trades[entity].order
        return None

    # -- market play ----------------------------------------------------------------------------
    def _play(self):
        for entity, lands in list(self._pending_cancel.items()):
            row = self.orders.get(entity)
            if row is None or row.status in IB_DONE:
                self._pending_cancel.pop(entity)
            elif entity not in self.cancelled:
                continue                                 # nothing asked IB to cancel it yet
            elif row.status != "PendingCancel":
                self.set_status(entity, "PendingCancel")
            elif lands:
                self.set_status(entity, "Cancelled")
                self._pending_cancel.pop(entity)
        for entity, quantity in list(self._fill_after_cancel.items()):
            if entity in self.cancelled:
                self._fill(entity, quantity)
                self._fill_after_cancel.pop(entity)
        if self._auto_fill is None:
            return
        for entity in list(self.cancelled):
            row = self.orders[entity]
            if (entity not in self._landed and entity not in self._pending_cancel
                    and entity not in self._fill_after_cancel and row.status not in IB_DONE):
                self.set_status(entity, "Cancelled")
                self._cancel_children(entity)
            self._landed.add(entity)
        for entity, row in list(self.orders.items()):
            if row.status in IB_DONE or row.status == "PendingCancel" or entity in self.hidden:
                continue
            if row.order_type not in self._auto_fill or not self._active(entity):
                continue
            if row.order_type == "MKT" and row.conid in self._never_close:
                continue
            remaining = row.total_quantity - row.filled_quantity
            if remaining <= 0 or not self._marketable(row):
                continue
            order = self.ib_trades[entity].order
            if row.leg == "take_profit" and getattr(order, "displaySize", 0):
                if not self._target_script:
                    continue
                remaining = min(remaining, self._target_script.pop(0))
            self._fill(entity, remaining)

    def _active(self, entity):
        """A bracket child works only once its parent has filled."""
        parent_id = int(getattr(self.ib_trades[entity].order, "parentId", 0) or 0)
        if not parent_id:
            return True
        parent = next((e for e, t in self.ib_trades.items() if t.order.orderId == parent_id), None)
        return parent is None or self.orders[parent].status == "Filled"

    def _marketable(self, row):
        if row.order_type == "MKT":
            return True
        limit = getattr(self.ib_trades[row.order_entity_id].order, "lmtPrice", None)
        if row.conid not in self.quotes or limit is None:
            return False
        bid, ask = self.quotes[row.conid]
        return limit >= ask if row.action == "BUY" else limit <= bid

    def _fill(self, entity, quantity):
        row = self.orders[entity]
        filled = row.filled_quantity + quantity
        status = "Filled" if filled >= row.total_quantity else row.status
        self.set_status(entity, status, filled=filled)
        sign = 1.0 if row.action == "BUY" else -1.0
        self.held[row.conid] = self.held.get(row.conid, 0.0) + sign * quantity
        self._record_fill(entity, row, quantity)
        self._oca_after_fill(entity, quantity, status == "Filled")

    def _record_fill(self, entity, row, quantity):
        """An execution, as execDetails would bring it (the scoreboard projects round trips from these)."""
        from trader.data.broker_state import BrokerFillRow
        self._exec_seq += 1
        bid, ask = self.quotes.get(row.conid, (100.0, 100.0))
        now = self.now()
        self._fills.append((entity, getattr(self.ib_trades[entity].order, "orderRef", None), BrokerFillRow(
            account_id=ACCOUNT, exec_id=f"sim-exec-{self._exec_seq}", order_entity_id=entity,
            perm_id=self.perm.get(entity), client_order_id=None, session_epoch="", conid=row.conid,
            side=row.action, quantity=float(quantity), price=ask if row.action == "BUY" else bid,
            commission=1.0, commission_currency="USD", realized_pnl=None, fill_time=now, revision=1,
            source_timestamp=now)))

    def _oca_after_fill(self, entity, quantity, complete):
        order = self.ib_trades[entity].order
        group, oca_type = getattr(order, "ocaGroup", "") or "", int(getattr(order, "ocaType", 0) or 0)
        siblings = [e for e, t in self.ib_trades.items()
                    if e != entity and group and getattr(t.order, "ocaGroup", "") == group
                    and self.orders[e].status not in IB_DONE]
        for sibling in siblings:
            row = self.orders[sibling]
            if oca_type == 2 and not complete:
                self.set_status(sibling, row.status, total=row.total_quantity - quantity)
            else:
                self.set_status(sibling, "Cancelled")

    def _cancel_children(self, entity):
        order_id = self.ib_trades[entity].order.orderId
        for child, trade in self.ib_trades.items():
            if (int(getattr(trade.order, "parentId", 0) or 0) == order_id
                    and self.orders[child].status not in IB_DONE):
                self.set_status(child, "Cancelled")

    # -- promotion -----------------------------------------------------------------------------
    def promote(self, started_at=None):
        self._play()
        store, db = self.trader.broker_state_store, self.trader.journal_db
        staged, self._staged_generation = self._staged_generation, None
        changes, self._changes = self._changes, []
        fills, self._fills = self._fills, []
        now = self.now()

        def write(conn):
            gid = staged if staged is not None else store.open_generation_in_tx(
                conn, ("account",), started_at or et(11, 0))
            store.upsert_account_in_tx(conn, BrokerAccountRow(
                ACCOUNT, "paper", self.net_liquidation, None, None, None, None,
                {"DailyPnL:USD": str(self.daily_pnl)}, 1, now))
            for conid, quantity in self.held.items():
                price = self.quotes.get(conid, (100.0, 100.0))[0]      # marked at the bid when quoted
                store.upsert_position_in_tx(conn, BrokerPositionRow(
                    ACCOUNT, conid, SYMBOLS.get(conid, "AAPL"), "STK", "SMART", "USD", quantity, 90.0, price,
                    quantity * price, 0.0, 0.0, 0.0, quantity == 0, 1, et(11, 0)))
            for entity, row in self.orders.items():
                if entity in self.hidden:
                    continue
                store.upsert_order_in_tx(conn, row)
                store.bind_alias_in_tx(conn, "perm_id", str(self.perm[entity]), ACCOUNT, "", entity, et(11, 0))
            self._record_events_in_tx(conn, gid, changes, now)
            for entity, order_ref, fill in fills:
                if order_ref:
                    store.bind_alias_in_tx(conn, "order_ref", order_ref, ACCOUNT, "", entity, now)
                store.upsert_fill_in_tx(conn, fill)
            cursor = conn.execute("SELECT COALESCE(MAX(source_cursor), 0) FROM domain_event_journal").fetchone()[0]
            store.mark_generation_promoted_in_tx(conn, gid, int(cursor), et(11, 0))
            return gid
        self.generations += 1
        gid = db.transaction(write)
        if staged is not None:
            self.trader.broker_ingest.ready = True       # the re-enumeration is complete
        self._notify_saga(gid, now, changes)
        return gid

    def _record_events_in_tx(self, conn, gid, changes, now):
        """The ingest's broker order event history (Plan 6 ruling 14): one row per change, in order."""
        record = self.record_order_event_in_tx
        last = {}
        for entity, row in changes:
            if entity in self.hidden or last.get(entity) == self._state_of(row):
                continue
            last[entity] = self._state_of(row)
            record(conn, gid, entity, self.perm.get(entity), self.ib_trades[entity].order.orderId, row, now)

    @staticmethod
    def _state_of(row):
        return (row.status, row.filled_quantity, row.total_quantity, row.oca_type)

    def _notify_saga(self, gid, now, changes):
        """As broker ingest does: one saga event per order status change, in arrival order."""
        if self.saga is None:
            return
        for entity, row in changes:
            if entity in self.hidden or not row.order_group_id:
                continue
            state = self._state_of(row)
            if self._notified.get(entity) == state:
                continue
            self._notified[entity] = state
            event = BrokerOrderEvent(
                order_group_id=row.order_group_id, leg=row.leg or "entry", status=row.status,
                filled_quantity=row.filled_quantity, total_quantity=row.total_quantity,
                order_id=int(self.perm.get(entity) or 0),
                event_id=f"sim:{entity}:{row.status}:{row.filled_quantity}:{row.total_quantity}:{gid}",
                source_timestamp=now, order_entity_id=entity)
            try:
                self.saga.on_broker_event(event)
            except KeyError:
                pass


class Universe:
    def resolve_symbol(self, conid, **_kwargs):
        if conid not in SYMBOLS:
            return []
        return [SimpleNamespace(conId=conid, symbol=SYMBOLS[conid], secType="STK", exchange="SMART",
                                primaryExchange="NASDAQ", currency="USD")]


class SimQuotes:
    """``QuoteAuthority`` over the simulator's quote table, stamped with the stack clock."""

    def __init__(self, sim, clock):
        self.sim, self.clock = sim, clock

    def executable_quote(self, conid, *, side):
        from trader.trading.proposal_command_service import ExecutableQuote
        if int(conid) not in self.sim.quotes:
            return None
        bid, ask = self.sim.quotes[int(conid)]
        return ExecutableQuote(conid=int(conid), side=side, price=ask if side == "BUY" else bid,
                               market_timestamp=self.clock[0], feed_type="live", session_state="continuous",
                               bid=bid, ask=ask, bid_size=1_000.0, ask_size=1_000.0)


class SimMargin:
    def __init__(self, sim):
        self.sim = sim

    def what_if_margin(self, conid, side, quantity):
        return {"initMarginAfter": 5_000.0, "equityWithLoanAfter": self.sim.net_liquidation - 1_000.0}


class Composed:
    def __init__(self, tmp_path, loop_thread, clock, *, automation=False, sim=None, ai_paper=False,
                 kill_pct=None, acceptance_probe=False, identities=None, telegram=None, market=False):
        from trader.trading.command_stack import build_command_stack

        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        store = BrokerStateStore(db)
        store.migrate(migrator)
        trader = object.__new__(Trader)
        self.trader, self.clock, self.loop_thread, self.tmp_path = trader, clock, loop_thread, tmp_path
        self.sim = sim or BrokerSim(trader)
        self.sim.trader = trader
        trader.journal_db, trader.domain_journal, trader.broker_state_store = db, journal, store
        trader.broker_ingest = Ingest(self.sim)
        trader.risk_gate = RiskGate(RiskLimits(max_daily_loss=1000.0),
                                    event_store=SimpleNamespace(count_since=lambda **_k: 0))
        trader.universe_accessor = Universe()
        trader.portfolio = SimpleNamespace(get_positions=lambda: [], get_portfolio_items=lambda: [])
        trader.book = SimpleNamespace(get_open_order_count=lambda: 0, get_trades=self._book_trades)
        trader.client = SimpleNamespace(ib=self.sim, get_snapshot=self._snapshot)
        trader.executioner = self.sim
        trader.ib_account = ACCOUNT
        trader.paper_trading = True
        trader._main_loop = loop_thread.loop
        trader.get_pnl = lambda: [SimpleNamespace(dailyPnL=self.sim.daily_pnl)]
        if automation:
            enable_automation(trader, tmp_path)
        patches = []
        if ai_paper:
            from trader.automation.ai_paper_config import load_ai_paper_config
            section = {"enabled": True, "acceptance_probe": acceptance_probe}
            if kill_pct is not None:
                section["experiment_kill_drawdown_pct"] = kill_pct
            if telegram is not None:
                section["telegram"] = telegram
            trader.ai_paper_config = load_ai_paper_config(section, trading_mode="paper")
        if market:
            patches = self._play_the_market(tmp_path, identities)

        async def no_margin(*_a):
            raise RuntimeError("what-if is not part of this test")
        trader.check_order_margin = no_margin
        for patch in patches:
            patch.start()
        try:
            self.stack = build_command_stack(trader, CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0),
                                             now=lambda: clock[0])
        finally:
            for patch in patches:
                patch.stop()
        self.liquidation = self.stack.liquidation_service
        self.saga = self.stack.protective_order_saga
        if market:
            self.sim.saga = self.saga

    def _play_the_market(self, tmp_path, identities):
        """The acceptance shape (Plan 6): quotes, what-if, history and saga events come from the sim."""
        import trader.trading.command_stack as command_stack
        from tests.automation.ai_paper_fixtures import make_history

        trader, clock = self.trader, self.clock
        trader.data = make_history(str(tmp_path / "history.duckdb"))
        trader._ib_upstream_connected, trader._ib_upstream_error = True, None

        async def resolve_contract(contract):
            return Universe().resolve_symbol(int(contract.conId))
        trader.resolve_contract = resolve_contract
        if identities is not None:
            trader.rpc_identity = identities["trader"]
        self.sim.now = lambda: clock[0]
        return [mock.patch.object(command_stack, "TraderQuoteAuthority",
                                  lambda *a, **k: SimQuotes(self.sim, clock)),
                mock.patch.object(command_stack, "TraderBrokerAuthority", lambda *a, **k: SimMargin(self.sim))]

    def _book_trades(self):
        return {t.order.orderId: [t] for t in self.sim.ib_trades.values()}

    async def _snapshot(self, contract, delayed=False):
        bid, ask = self.sim.quotes.get(int(contract.conId), (float("nan"), float("nan")))
        return SimpleNamespace(contract=contract, bid=bid, ask=ask, last=ask, time=self.clock[0],
                               bidSize=1000.0, askSize=1000.0)

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


def enable_automation(trader, tmp_path):
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


def restart(composed, tmp_path, **kwargs):
    """Stop the old worker, build a new stack over the same journal and the same broker."""
    composed.liquidation.worker.shutdown()
    return Composed(tmp_path, composed.loop_thread, composed.clock, sim=composed.sim, **kwargs)


# ---------------------------------------------------------------------------
# The served stack: the production registry over typed RPC (Plan 6 Task 1)
# ---------------------------------------------------------------------------

def _role_of(method):
    from trader.messaging.principals import TRADER_ACL
    return "command" if ("command", method) in TRADER_ACL else "query"


class FixedFx:
    def evidence(self):
        from trader.scoreboard.ports import FxEvidence
        return FxEvidence("USD", 1.0, "base_is_usd", et(9, 0))


class ServedStack:
    """A ``Composed(ai_paper=True)`` stack behind typed RPC servers on free loopback ports."""

    def __init__(self, tmp_path, loop_thread, clock, *, sim=None, identities=None, **options):
        from tests.rpc_identity_fixtures import ServedStack as Sockets, make_identities
        from trader.automation.experiment_service import attach_production_identity
        from trader.domain.feed_service import DomainFeedService
        from trader.domain.snapshot_service import DomainSnapshotService
        from trader.messaging.production_api import build_production_registry

        self.tmp_path, self.loop_thread, self.clock, self.options = tmp_path, loop_thread, clock, options
        self.identities = identities or make_identities()
        self.composed = Composed(tmp_path, loop_thread, clock, sim=sim, ai_paper=True, market=True,
                                 identities=self.identities, **options)
        self.stack, self.sim, self.trader = self.composed.stack, self.composed.sim, self.composed.trader
        if self.stack.scoreboard is not None:
            self.stack.scoreboard.ledger.fx = FixedFx()
        self.registry = build_production_registry(
            self.trader, self.identities["trader"],
            snapshot_service=DomainSnapshotService(self.trader.domain_journal),
            feed_service=DomainFeedService(self.trader.domain_journal), command_stack=self.stack)
        attach_production_identity(self.stack.experiments, self.identities["trader"], self.registry)
        self.sockets = Sockets({("trader", "command"): self.registry, ("trader", "query"): self.registry},
                               self.identities)
        self._clients = {}
        self.calls: list[tuple[str, str]] = []

    # -- RPC ------------------------------------------------------------------------------------
    def client(self, principal, role="command"):
        key = (principal, role)
        if key not in self._clients:
            self._clients[key] = self.sockets.client(principal, "trader", role, timeout=30.0)
        return self._clients[key]

    def call(self, principal, method, body):
        self.calls.append((principal, method))
        return self.client(principal, _role_of(method)).call(method, body, dict)

    def principals_for(self, *methods):
        """Who signed the ledger rows of these commands (the server derives it from the key)."""
        rows = self.trader.journal_db.execute(
            "SELECT DISTINCT action, source FROM command_ledger WHERE action IN ({})".format(
                ",".join("?" * len(methods))), list(methods), fetch="all")
        return {row[1] for row in rows}

    # -- time and the broker ----------------------------------------------------------------------
    def now(self):
        return self.clock[0]

    def advance(self, seconds=0.0, *, minutes=0.0):
        self.clock[0] = self.clock[0] + dt.timedelta(seconds=seconds, minutes=minutes)

    def tick(self):
        """One liquidation tick, the reconciler (resolves commands from their close roots), the kill monitor."""
        self.composed.tick()
        self.stack.reconciler.run_due(self.clock[0])
        self.stack.experiments.monitor.tick()

    def advance_and_promote(self, seconds):
        """The acceptance port's ``sleep``: time passes, the broker moves, the trader catches up."""
        self.advance(seconds)
        self.sim.promote()
        self.tick()

    def run_session(self, at):
        return self.composed.run_session(at)

    def start_experiment(self, command_id="start-1"):
        self.sim.promote()
        out = self.call("cli", "start_experiment", {"command_id": command_id, "reason": "acceptance"})
        assert out["outcome"]["state"] == "ARMED", out
        self.stack.experiments.monitor.recover()           # trader_service does this before readiness
        return out["outcome"]["experiment_id"]

    @property
    def experiment_id(self):
        return self.stack.experiments.store.active().experiment_id

    def restart(self):
        """A new trader on the same journal and the same broker, served on new ports."""
        self.close(shutdown_only=True)
        again = ServedStack(self.tmp_path, self.loop_thread, self.clock, sim=self.sim,
                            identities=self.identities, **self.options)
        again.stack.experiments.monitor.recover()
        return again

    def close(self, shutdown_only=False):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.sockets.close()
        self.composed.liquidation.worker.shutdown()


def served_stack(tmp_path, loop_thread, monkeypatch, *, start=et(11, 0), arm=True, **options):
    yaml_path = tmp_path / "trader.yaml"          # paper automation status() reads it: never the developer's
    yaml_path.write_text("{}\n")
    monkeypatch.setenv("TRADER_CONFIG", str(yaml_path))
    served = ServedStack(tmp_path, loop_thread, [start], **options)
    if arm:
        served.start_experiment()
    return served
