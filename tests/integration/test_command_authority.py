"""[M1-F3] Task 9 -- the in-process command-authority integration gate.

Wires a REAL migrated DuckDB, ``DomainJournal``, ``ProposalRepository``,
``ProposalCommandService``, ``TradingControlStore``, ``CommandLedger``,
``TradingCommandCoordinator``, ``ApprovalCommandService`` and
``OutcomeReconciler`` behind FAKE quote/position/broker/strategy/order-dispatch
ports (the same fake-port shape the other F3 unit suites use). It proves the
full create -> approve -> submit -> resolve loop end to end, and that a crash
between claim and broker-ack is recovered by ``rescan_on_startup`` +
``reconcile_once`` after a coordinator restart over the same journal file --
without ever turning on live dispatch (this gate is entirely in-process).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerOrderRow, BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import ProposalRepository, apply_proposal_authority_migration
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import register_command_authority
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.trader_service import build_order_state_view
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    OutcomeReconciler,
    SubmittedOrders,
    apply_command_ledger_migration,
)
from trader.trading.order_correlation import encode_order_ref
from trader.trading.proposal_command_service import ExecutableQuote, ProposalCommandService
from trader.trading.trading_control import TradingControlStore, apply_trading_control_migration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
ACCOUNT = "DU111111"
CONID = 265598


class SimulatedCrash(BaseException):
    """A hard crash (SIGKILL-like) mid-dispatch.

    Deliberately a ``BaseException`` so it escapes the saga's ``except
    Exception`` dispatch guard AND the coordinator's own broad handler --
    modelling a process that simply dies after committing the claim but before
    persisting any dispatch outcome. Recovery is then ONLY possible via
    ``rescan_on_startup`` (a schedule call never ran), which is exactly the
    invariant this gate exercises.
    """


class FakeOrders:
    """``OrderDispatchPort`` double serving BOTH the approval saga (``submit``)
    and the reconciler (``find_by_order_ref``/``enumeration_complete``)."""

    def __init__(self):
        self._raise = None
        self._next_id = 3001
        self.submissions: list[SubmittedOrders] = []
        self._by_ref: dict[str, list] = {}
        self.enumeration_ok = True

    def raise_on_submit(self, exc):
        self._raise = exc

    def submit(self, proposal, order_ref, order_group_id):
        if self._raise is not None:
            raise self._raise
        submitted = SubmittedOrders(
            order_group_id=order_group_id, order_ref=order_ref, order_ids=[self._next_id])
        self._next_id += 1
        self.submissions.append(submitted)
        return submitted

    def cancel(self, order_entity_id, order_ref):  # pragma: no cover
        raise NotImplementedError

    def add_broker_order(self, *, order_ref, status, order_ids):
        self._by_ref.setdefault(order_ref, []).append(
            SimpleNamespace(order_ref=order_ref, status=status, order_ids=list(order_ids)))

    def find_by_order_ref(self, account_id, order_ref):
        return list(self._by_ref.get(order_ref, []))

    def enumeration_complete(self):
        return self.enumeration_ok


class FakeStrategyPort:
    def __init__(self):
        self.receipts: dict = {}

    def forward(self, request):  # pragma: no cover
        raise AssertionError("integration gate never forwards a strategy mutation")

    def get_receipt(self, command_id):
        return self.receipts.get(command_id)


class FakeAlerts:
    def __init__(self):
        self.raised: list[str] = []

    def raise_alert(self, command_id, detail):
        self.raised.append(command_id)


class FakeNonceGate:
    def __init__(self):
        self._consumed: set[str] = set()

    def consume_in_tx(self, conn, nonce, request):
        if not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


def _secdef(conid):
    return SimpleNamespace(
        conId=conid, symbol="AAPL", primaryExchange="NASDAQ", secType="STK",
        exchange="SMART", currency="USD")


class _Stack:
    """A live command-authority stack bound to one journal file. ``restart``
    rebuilds a fresh coordinator + reconciler over the SAME file (crash
    recovery), sharing only the durable DuckDB state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._now = lambda: NOW
        db = DuckDBConnection.get_instance(db_path)
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        apply_proposal_authority_migration(migrator)
        apply_command_ledger_migration(migrator)
        apply_trading_control_migration(migrator)

        self.db = db
        self.journal = journal
        self.repo = ProposalRepository(journal)
        self.ledger = CommandLedger(journal)
        self.controls = TradingControlStore(journal)
        db.transaction(lambda conn: self.controls.seed_in_tx(conn, [(ACCOUNT, "paper")], NOW))

        self.orders = FakeOrders()
        self.strategy = FakeStrategyPort()
        self.alerts = FakeAlerts()
        self.quotes = SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
            conid=conid, side=side, price=210.0 if side == "ask" else 209.5,
            market_timestamp=NOW, feed_type="live", session_state="continuous"))
        self.positions = SimpleNamespace(reducible_quantity=lambda account_id, conid: 0.0)
        self.broker = SimpleNamespace(capture=lambda account_id: SimpleNamespace(
            account_id=account_id, account_mode="paper", generation_id=1,
            source_cursor=1, open_order_count=0, daily_pnl=0.0,
            net_liquidation=100_000.0, working_orders=(),
            reducible_quantity=lambda conid: 0.0,
            position_value=lambda conid: 0.0))
        self.risk_gate = SimpleNamespace(
            check_instrument=lambda **_kw: SimpleNamespace(approved=True, reason=""),
            evaluate=lambda **_kw: SimpleNamespace(approved=True, reason=""))
        self.risk_producer = SimpleNamespace(publish_decision=lambda *a, **k: None)

        self.reconciler = OutcomeReconciler(
            journal=journal, ledger=self.ledger, orders=self.orders, strategy=self.strategy,
            alerts=self.alerts, repo=self.repo, now=self._now)

        self.proposal_service = ProposalCommandService(
            repository=self.repo, journal=journal, risk_gate=self.risk_gate, quotes=self.quotes,
            universe=SimpleNamespace(resolve_conid=lambda conid: _secdef(conid) if conid == CONID else None),
            account_id=ACCOUNT, account_mode="paper", now=self._now, controls=self.controls,
            positions=self.positions)
        self.approval_service = ApprovalCommandService(
            journal=journal, ledger=self.ledger, repo=self.repo, controls=self.controls,
            orders=self.orders, quotes=self.quotes,
            risk_gate=self.risk_gate, risk_producer=self.risk_producer, reconciler=self.reconciler,
            broker=self.broker, account_id=ACCOUNT, account_mode="paper", now=self._now)

        self.coordinator = _build_coordinator(
            journal, self.ledger, self._now, self.reconciler)
        self.registry = TypedRpcRegistry()
        register_command_authority(
            self.registry, self.coordinator, self.proposal_service, self.repo,
            account_id=ACCOUNT, account_mode="paper", controls=self.controls,
            resume_ready=lambda: True, reconciliation_complete=lambda command_id: True,
            approval_service=self.approval_service)

    def now(self):
        return NOW

    def get_command(self, command_id):
        return self.coordinator.get_command(command_id)

    def create_pending(self, *, conid=CONID, action="BUY"):
        created = self.coordinator.execute(_create_request("seed-%s" % conid, conid, action))
        return created.outcome["id"]

    def restart(self):
        return _Stack(self.db_path)


def _build_coordinator(journal, ledger, now, reconciler):
    from trader.trading.command_coordinator import TradingCommandCoordinator
    return TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=CommandAudit(journal),
        nonces=FakeNonceGate(), now=now, reconciler=reconciler)


def _create_request(command_id, conid=CONID, action="BUY"):
    return CommandRequest(
        command_id=command_id, action="create_proposal", account_id=ACCOUNT,
        target_type="proposal", target_id="", expected_version=None,
        body={"conid": conid, "action": action, "quantity": 10}, source="dashboard",
        preflight_nonce=None)


def _approve_request(command_id, proposal_id, expected_version):
    return CommandRequest(
        command_id=command_id, action="approve_proposal", account_id=ACCOUNT,
        target_type="proposal", target_id=str(proposal_id), expected_version=expected_version,
        body={"proposal_id": proposal_id}, source="dashboard",
        preflight_nonce=f"nonce-{command_id}")


@pytest.fixture
def stack(tmp_path):
    return _Stack(str(tmp_path / "authority.duckdb"))


def test_full_loop_create_approve_submit_resolve(stack):
    created = stack.coordinator.execute(_create_request("c-1", conid=CONID, action="BUY"))
    assert created.state == "RESOLVED"
    pid = created.outcome["id"]
    approved = stack.coordinator.execute(_approve_request("c-2", pid, expected_version=1))
    assert approved.state == "SUBMITTED"
    events = [e.event_type for e in stack.journal.read_after(0, 1000)]
    assert events.count("proposal.updated") >= 3        # create, claim, submit-link
    assert events.count("command.updated") >= 5         # two command lifecycles
    assert stack.get_command("c-2").state == "SUBMITTED"
    assert stack.repo.get(pid).status == "EXECUTED"


def test_journal_cursor_stream_is_gap_free(stack):
    # [M1-F1] invariant held under this plan's writers: with no rolled-back
    # transaction, the reader sees a contiguous, strictly-increasing cursor
    # stream across the whole create+approve lifecycle.
    created = stack.coordinator.execute(_create_request("c-1", conid=CONID, action="BUY"))
    pid = created.outcome["id"]
    stack.coordinator.execute(_approve_request("c-2", pid, expected_version=1))
    cursors = [e.source_cursor for e in stack.journal.read_after(0, 1000)]
    assert cursors == list(range(cursors[0], cursors[0] + len(cursors)))


def test_duplicate_create_command_yields_exactly_one_proposal(stack):
    first = stack.coordinator.execute(_create_request("dup-1", conid=CONID, action="BUY"))
    pid = first.outcome["id"]
    # An exact replay of the SAME command_id returns the recorded receipt and
    # never mints a second proposal row.
    replay = stack.coordinator.execute(_create_request("dup-1", conid=CONID, action="BUY"))
    assert replay.state == "RESOLVED"
    assert replay.outcome["id"] == pid
    pending = [r for r in stack.repo.list(status="PENDING", limit=100) if r.conid == CONID]
    assert len(pending) == 1


def test_crash_between_claim_and_dispatch_reconciles_after_restart(stack):
    pid = stack.create_pending(conid=CONID, action="BUY")
    stack.orders.raise_on_submit(SimulatedCrash("killed before IB ack"))
    with pytest.raises(SimulatedCrash):
        stack.coordinator.execute(_approve_request("c-9", pid, expected_version=1))

    # The proposal was CLAIMED (APPROVED) before the crash; the command ledger
    # is stuck mid-saga (SUBMITTING) with no dispatch outcome persisted.
    assert stack.repo.get(pid).status == "APPROVED"
    assert stack.ledger.get("c-9").state == "SUBMITTING"

    restarted = stack.restart()                          # new coordinator over the same DB
    assert restarted.reconciler.rescan_on_startup() == ["c-9"]
    restarted.orders.add_broker_order(
        order_ref=encode_order_ref("og-c-9"), status="Submitted", order_ids=[31])
    assert restarted.reconciler.reconcile_once("c-9", restarted.now()).resolved is True
    assert restarted.repo.get(pid).status == "EXECUTED"  # exactly one order, no resubmission
    assert restarted.orders.submissions == []            # reconciler never re-dispatched
    assert restarted.ledger.get("c-9").state == "RESOLVED"


def _broker_order(order_entity_id, *, status, deleted=False):
    return BrokerOrderRow(
        order_entity_id=order_entity_id, account_id=ACCOUNT, conid=CONID, symbol="AAPL",
        order_group_id=None, leg="entry", is_external=False, action="SELL", order_type="MKT",
        total_quantity=10.0, filled_quantity=0.0, avg_fill_price=None, limit_price=None,
        stop_price=None, tif="DAY", status=status, deleted=deleted, revision=1,
        source_timestamp=NOW)


def test_cancel_wedge_reconciles_against_the_real_broker_order_store(stack):
    # [M1-F3] MEDIUM-2 end to end: the production OrderStateView adapter
    # (trader_service.build_order_state_view over a REAL [M1-F2] BrokerStateStore)
    # resolves an ambiguous cancel_order by the TARGET order's authoritative
    # materialized status -- NOT the always-empty og-{command_id} lookup.
    store = BrokerStateStore(stack.db)
    store.migrate(SchemaMigrator(stack.db))
    trader_like = SimpleNamespace(broker_state_store=store, domain_journal=stack.journal)
    view = build_order_state_view(trader_like)
    assert view is not None

    reconciler = OutcomeReconciler(
        journal=stack.journal, ledger=stack.ledger, orders=stack.orders,
        strategy=stack.strategy, alerts=stack.alerts, repo=stack.repo,
        orders_view=view, now=stack._now)

    stack.ledger.insert_for_test(
        "cx-1", state="OUTCOME_UNKNOWN", updated_at=NOW, account_id=ACCOUNT,
        action="cancel_order", target_type="order", target_id="ord-1")

    # Still Submitted at the broker -> the cancel didn't take -> stays unknown.
    stack.db.transaction(lambda conn: store.upsert_order_in_tx(
        conn, _broker_order("ord-1", status="Submitted")))
    assert reconciler.reconcile_once("cx-1", stack.now()).resolved is False
    assert stack.ledger.get("cx-1").state == "OUTCOME_UNKNOWN"

    # Cancelled at the broker -> RESOLVED with the authoritative status.
    stack.db.transaction(lambda conn: store.upsert_order_in_tx(
        conn, _broker_order("ord-1", status="Cancelled")))
    result = reconciler.reconcile_once("cx-1", stack.now())
    assert result.resolved is True
    row = stack.ledger.get("cx-1")
    assert row.state == "RESOLVED"
    assert row.outcome["authoritative_status"] == "Cancelled"


def test_crash_with_no_broker_order_and_complete_enumeration_fails_cleanly(stack):
    pid = stack.create_pending(conid=CONID, action="BUY")
    stack.orders.raise_on_submit(SimulatedCrash("killed before IB ack"))
    with pytest.raises(SimulatedCrash):
        stack.coordinator.execute(_approve_request("c-9", pid, expected_version=1))

    restarted = stack.restart()
    assert restarted.reconciler.rescan_on_startup() == ["c-9"]
    # A fenced, complete broker enumeration with NO matching order proves the
    # dispatch never reached the broker -> clean FAILED, never a resubmission.
    restarted.orders.enumeration_ok = True
    assert restarted.reconciler.reconcile_once("c-9", restarted.now()).resolved is True
    assert restarted.repo.get(pid).status == "FAILED"
    assert restarted.ledger.get("c-9").outcome == {"submitted": False}
    assert restarted.orders.submissions == []
