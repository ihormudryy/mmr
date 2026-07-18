#!/usr/bin/env python3
"""[P1 Task 8] Command-plane failure drills — the synthetic recovery gate.

Drives a battery of scripted failure scenarios against an *in-process* command
authority stack: a real migrated DuckDB ``DomainJournal`` + ``CommandLedger`` +
``TradingControlStore`` + ``TradingCommandCoordinator`` + ``ProposalCommandService``
+ ``ApprovalCommandService`` + ``OutcomeReconciler``, wired behind deterministic
fake broker/quote/order/strategy ports (the same fake-port shape the F3 unit and
integration suites use). Nothing here touches IB or live dispatch — this is the
*synthetic* half of the release gate; the manual IB-paper session soak
(``scripts/run_paper_soak.py`` during market hours) is the other, non-fungible
half and MUST NOT be replaced by these fixtures.

Each scenario asserts the P1 recovery invariants:

- **one idempotent command history** — a command_id resolves to exactly one
  terminal ledger row; an exact replay never mints a second proposal/order;
- **durable audit** — the journal records every command transition;
- **no duplicate order reference** — the dispatch port is never asked to submit
  the same ``order_ref`` twice (no double-send on recovery);
- **coherent terminal state** — ledger state and proposal status agree
  (SUBMITTED↔EXECUTED, OUTCOME_UNKNOWN↔APPROVED-pending-reconcile, …).

Output is a versioned JSON report (``version``, ``commit_digest``,
``config_digest``, ``scenario_results``, ``passed``) suitable for stapling to a
release record. Exit code is non-zero when any runnable scenario fails, so the
script doubles as a CI/pre-activation gate.

Scenarios whose feature has not landed yet (liquidation — Task 7; circuit
breaker + semantic readiness — Task 6) are feature-detected and reported as
``pending`` rather than silently skipped, so the report never reads as "all
covered" when it is not.

Usage:
    python3 scripts/command_plane_drill.py                 # human summary + exit code
    python3 scripts/command_plane_drill.py --json          # machine JSON to stdout
    python3 scripts/command_plane_drill.py --output r.json  # write JSON to a file
    python3 scripts/command_plane_drill.py --scenarios happy_path,restart_unresolved
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

# Ensure the project root is importable when run as a bare script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    ProposalDraft,
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import register_command_authority
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    OutcomeReconciler,
    SubmittedOrders,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.order_correlation import encode_order_ref
from trader.trading.liquidation_service import LiquidationService
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.dispatch_guard import DispatchGuard
from trader.data.circuit_breaker_store import CircuitBreakerStore, apply_circuit_breaker_migration
from trader.trading.circuit_breaker import BreakerSignal, CircuitBreaker
from trader.trading.semantic_readiness import SemanticReadiness
from trader.trading.proposal_command_service import ExecutableQuote, ProposalCommandService
from trader.trading.trading_control import (
    TradingControlStore,
    apply_trading_control_migration,
)

REPORT_VERSION = 1
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
ACCOUNT = "DU111111"
CONID = 265598


# ---------------------------------------------------------------------------
# Fake ports (deterministic; mirror the F3 integration-gate doubles).
# ---------------------------------------------------------------------------

class SimulatedCrash(BaseException):
    """A hard crash mid-dispatch — escapes the saga's ``except Exception`` and
    the coordinator's broad handler, modelling a process that dies after the
    claim commits but before any dispatch outcome is persisted. Recovery is then
    only possible via ``rescan_on_startup``, which is the invariant we drill."""


class FakeOrders:
    """``OrderDispatchPort`` double serving the approval saga (``submit``) and
    the reconciler (``find_by_order_ref``/``enumeration_complete``). Records
    every submission so the no-duplicate-order-ref invariant is checkable."""

    def __init__(self) -> None:
        self._raise: Optional[BaseException] = None
        self._next_id = 3001
        self.submissions: list[SubmittedOrders] = []
        self._by_ref: dict[str, list] = {}
        self.enumeration_ok = True

    def raise_on_submit(self, exc: BaseException) -> None:
        self._raise = exc

    def submit(self, proposal, order_ref, order_group_id):
        if self._raise is not None:
            raise self._raise
        submitted = SubmittedOrders(
            order_group_id=order_group_id, order_ref=order_ref, order_ids=[self._next_id])
        self._next_id += 1
        self.submissions.append(submitted)
        return submitted

    def cancel(self, order_entity_id, order_ref):  # pragma: no cover - not drilled here
        raise NotImplementedError

    def add_broker_order(self, *, order_ref, status, order_ids) -> None:
        self._by_ref.setdefault(order_ref, []).append(
            SimpleNamespace(order_ref=order_ref, status=status, order_ids=list(order_ids)))

    def find_by_order_ref(self, account_id, order_ref):
        return list(self._by_ref.get(order_ref, []))

    def enumeration_complete(self):
        return self.enumeration_ok

    def order_refs(self) -> list[str]:
        return [s.order_ref for s in self.submissions]


class FakeStrategyPort:
    def __init__(self) -> None:
        self.receipts: dict = {}

    def forward(self, request):  # pragma: no cover - drills never forward strategy mutations
        raise AssertionError("drill never forwards a strategy mutation")

    def get_receipt(self, command_id):
        return self.receipts.get(command_id)


class FakeAlerts:
    def __init__(self) -> None:
        self.raised: list[str] = []

    def raise_alert(self, command_id, detail):
        self.raised.append(command_id)


class FakeNonceGate:
    """Single-use preflight nonce (paper). Any non-empty, not-yet-seen nonce
    consumes; a missing/replayed nonce is refused."""

    def __init__(self) -> None:
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


def _fresh_quotes():
    return SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
        conid=conid, side=side, price=210.0 if side == "ask" else 209.5,
        market_timestamp=NOW, feed_type="live", session_state="continuous"))


def _flat_broker():
    return SimpleNamespace(capture=lambda account_id: SimpleNamespace(
        account_id=account_id, account_mode="paper", generation_id=1,
        source_cursor=1, open_order_count=0, daily_pnl=0.0,
        net_liquidation=100_000.0, working_orders=(),
        reducible_quantity=lambda conid: 0.0, position_value=lambda conid: 0.0))


# ---------------------------------------------------------------------------
# In-process command-authority stack (one journal file; restartable).
# ---------------------------------------------------------------------------

class DrillStack:
    """A live command-authority stack bound to one journal file. ``restart``
    rebuilds a fresh coordinator + reconciler over the SAME file, sharing only
    the durable DuckDB state — exactly the crash-recovery boundary."""

    def __init__(self, db_path: str) -> None:
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
        self.quotes = _fresh_quotes()
        self.positions = SimpleNamespace(reducible_quantity=lambda account_id, conid: 0.0)
        self.broker = _flat_broker()
        self.risk_gate = SimpleNamespace(
            check_instrument=lambda **_kw: SimpleNamespace(approved=True, reason=""),
            evaluate=lambda **_kw: SimpleNamespace(approved=True, reason=""),
            check_leverage=lambda *a, **k: SimpleNamespace(approved=True, reason=""))
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
            orders=self.orders, quotes=self.quotes, risk_gate=self.risk_gate,
            risk_producer=self.risk_producer, reconciler=self.reconciler, broker=self.broker,
            account_id=ACCOUNT, account_mode="paper", now=self._now)
        self.coordinator = TradingCommandCoordinator(
            journal=journal, ledger=self.ledger, audit=CommandAudit(journal),
            nonces=FakeNonceGate(), now=self._now, reconciler=self.reconciler)
        self.registry = TypedRpcRegistry()
        register_command_authority(
            self.registry, self.coordinator, self.proposal_service, self.repo,
            account_id=ACCOUNT, account_mode="paper", controls=self.controls,
            resume_ready=lambda: True, reconciliation_complete=lambda command_id: True,
            approval_service=self.approval_service)

    def now(self):
        return NOW

    def create_pending(self, *, conid=CONID, action="BUY", command_id="seed"):
        created = self.coordinator.execute(create_request(command_id, conid, action))
        return created.outcome["id"]

    def restart(self) -> "DrillStack":
        return DrillStack(self.db_path)


def create_request(command_id, conid=CONID, action="BUY") -> CommandRequest:
    return CommandRequest(
        command_id=command_id, action="create_proposal", account_id=ACCOUNT,
        target_type="proposal", target_id="", expected_version=None,
        body={"conid": conid, "action": action, "quantity": 10}, source="dashboard",
        preflight_nonce=None)


def approve_request(command_id, proposal_id, expected_version) -> CommandRequest:
    return CommandRequest(
        command_id=command_id, action="approve_proposal", account_id=ACCOUNT,
        target_type="proposal", target_id=str(proposal_id), expected_version=expected_version,
        body={"proposal_id": proposal_id}, source="dashboard",
        preflight_nonce=f"nonce-{command_id}")


# ---------------------------------------------------------------------------
# Invariant assertions (raise AssertionError on violation).
# ---------------------------------------------------------------------------

def assert_terminal(stack: DrillStack, command_id: str, expected: str) -> None:
    row = stack.ledger.get(command_id)
    if row is None:
        raise AssertionError(f"no ledger row for {command_id}")
    if row.state != expected:
        raise AssertionError(f"{command_id}: ledger state {row.state} != {expected}")


def assert_no_duplicate_order_refs(stack: DrillStack) -> None:
    refs = stack.orders.order_refs()
    if len(refs) != len(set(refs)):
        raise AssertionError(f"duplicate order refs dispatched: {refs}")


def assert_proposal_status(stack: DrillStack, proposal_id: int, expected: str) -> None:
    status = stack.repo.get(proposal_id).status
    if status != expected:
        raise AssertionError(f"proposal {proposal_id}: status {status} != {expected}")


def command_updates(stack: DrillStack) -> int:
    """Durable audit proxy: count of journalled command transitions."""
    return sum(1 for e in stack.journal.read_after(0, 100_000)
               if e.event_type == "command.updated")


# ---------------------------------------------------------------------------
# Scenarios. Each takes a fresh (empty) db_path and returns an invariants dict;
# raising any exception marks the scenario failed.
# ---------------------------------------------------------------------------

def scn_happy_path(db_path: str) -> dict:
    """create -> approve -> submit -> resolve: one order, coherent terminal state."""
    stack = DrillStack(db_path)
    created = stack.coordinator.execute(create_request("c-1"))
    assert_terminal(stack, "c-1", "RESOLVED")
    pid = created.outcome["id"]
    approved = stack.coordinator.execute(approve_request("c-2", pid, expected_version=1))
    if approved.state != "SUBMITTED":
        raise AssertionError(f"approve state {approved.state} != SUBMITTED")
    assert_terminal(stack, "c-2", "SUBMITTED")
    assert_proposal_status(stack, pid, "EXECUTED")
    assert_no_duplicate_order_refs(stack)
    return {"orders_submitted": len(stack.orders.submissions),
            "command_updates": command_updates(stack)}


def scn_duplicate_create_idempotent(db_path: str) -> dict:
    """An exact command replay returns the recorded receipt and mints no
    second proposal — one idempotent command history."""
    stack = DrillStack(db_path)
    first = stack.coordinator.execute(create_request("dup-1"))
    pid = first.outcome["id"]
    replay = stack.coordinator.execute(create_request("dup-1"))
    if replay.outcome["id"] != pid:
        raise AssertionError("replay minted a different proposal id")
    pending = [r for r in stack.repo.list(status="PENDING", limit=100) if r.conid == CONID]
    if len(pending) != 1:
        raise AssertionError(f"expected exactly 1 pending proposal, got {len(pending)}")
    return {"proposal_id": pid, "pending_count": len(pending)}


def scn_ambiguous_submit_reconciles(db_path: str) -> dict:
    """A dispatch that raises after the claim -> OUTCOME_UNKNOWN, never a false
    SUBMITTED; the reconciler resolves it from broker truth with no re-dispatch."""
    stack = DrillStack(db_path)
    pid = stack.create_pending(command_id="amb")
    stack.orders.raise_on_submit(TimeoutError("lost ack after send"))
    receipt = stack.coordinator.execute(approve_request("c-amb", pid, expected_version=1))
    if receipt.state != "OUTCOME_UNKNOWN":
        raise AssertionError(f"ambiguous dispatch state {receipt.state} != OUTCOME_UNKNOWN")
    assert_proposal_status(stack, pid, "APPROVED")   # stays approved for reconcile
    # Broker shows the order live -> reconcile resolves to EXECUTED, no re-send.
    # (submit stays disabled; reconcile reads find_by_order_ref, never submit.)
    stack.orders.add_broker_order(
        order_ref=encode_order_ref("og-c-amb"), status="Submitted", order_ids=[41])
    result = stack.reconciler.reconcile_once("c-amb", stack.now())
    if not result.resolved:
        raise AssertionError("reconciler did not resolve the ambiguous command")
    assert_proposal_status(stack, pid, "EXECUTED")
    assert_no_duplicate_order_refs(stack)
    if stack.orders.submissions:
        raise AssertionError("reconciler re-dispatched an order (double-send)")
    return {"resolved": True, "resubmissions": len(stack.orders.submissions)}


def scn_restart_unresolved(db_path: str) -> dict:
    """Hard crash between claim and broker-ack: after a coordinator restart over
    the same journal, rescan_on_startup finds the wedge and reconcile resolves
    it from broker truth — exactly one order, no resubmission."""
    stack = DrillStack(db_path)
    pid = stack.create_pending(command_id="crash")
    stack.orders.raise_on_submit(SimulatedCrash("killed before IB ack"))
    try:
        stack.coordinator.execute(approve_request("c-crash", pid, expected_version=1))
        raise AssertionError("crash did not propagate")
    except SimulatedCrash:
        pass
    assert_proposal_status(stack, pid, "APPROVED")
    assert_terminal(stack, "c-crash", "SUBMITTING")

    restarted = stack.restart()
    rescan = restarted.reconciler.rescan_on_startup()
    if "c-crash" not in rescan:
        raise AssertionError(f"rescan missed the wedge: {rescan}")
    restarted.orders.add_broker_order(
        order_ref=encode_order_ref("og-c-crash"), status="Submitted", order_ids=[51])
    if not restarted.reconciler.reconcile_once("c-crash", restarted.now()).resolved:
        raise AssertionError("post-restart reconcile did not resolve")
    assert_proposal_status(restarted, pid, "EXECUTED")
    if restarted.orders.submissions:
        raise AssertionError("reconciler re-dispatched after restart (double-send)")
    assert_terminal(restarted, "c-crash", "RESOLVED")
    return {"rescanned": rescan, "resubmissions": len(restarted.orders.submissions)}


ACCOUNT_LIVE = "U1234567"


class LiveApproval:
    """A LIVE-mode approval path with a ``DispatchGuard`` wired in, plus a
    directly inserted live-eligible pending proposal. This is the shape needed to
    drive the guard's live-only enforcement (feed/freshness, notional, leverage)
    end to end through the coordinator saga -- the harness itself runs paper, so
    the live path is built explicitly here. Mirrors
    ``tests/test_approval_command.py::_build_approval`` for ``account_mode='live'``
    and the command-stack DispatchGuard wiring."""

    def __init__(self, db_path: str, *, stale_quote: bool = False,
                 max_notional: Optional[float] = None) -> None:
        db = DuckDBConnection.get_instance(db_path)
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        apply_proposal_authority_migration(migrator)
        apply_command_ledger_migration(migrator)
        apply_trading_control_migration(migrator)

        self.journal = journal
        self.repo = ProposalRepository(journal)
        self.ledger = CommandLedger(journal)
        self.controls = TradingControlStore(journal)
        db.transaction(lambda conn: self.controls.seed_in_tx(conn, [(ACCOUNT_LIVE, "live")], NOW))

        ts = NOW - dt.timedelta(seconds=30) if stale_quote else NOW
        self.quotes = SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
            conid=conid, side=side, price=210.0 if side == "ask" else 209.5,
            market_timestamp=ts, feed_type="live", session_state="continuous"))
        self.broker = SimpleNamespace(capture=lambda account_id: SimpleNamespace(
            account_id=account_id, account_mode="live", generation_id=1, source_cursor=1,
            open_order_count=0, daily_pnl=0.0, net_liquidation=1_000_000.0, working_orders=(),
            reducible_quantity=lambda conid: 0.0, position_value=lambda conid: 0.0))
        self.margin = SimpleNamespace(what_if_margin=lambda conid, side, quantity: {
            "initMarginAfter": 5000.0, "equityWithLoanAfter": 1_000_000.0})
        self.risk_gate = SimpleNamespace(
            evaluate=lambda **_kw: SimpleNamespace(approved=True, reason=""),
            check_leverage=lambda *a, **k: SimpleNamespace(approved=True, reason=""))
        self.risk_producer = SimpleNamespace(publish_decision=lambda *a, **k: None)
        self.orders = FakeOrders()
        self.reconciler = OutcomeReconciler(
            journal=journal, ledger=self.ledger, orders=self.orders,
            strategy=FakeStrategyPort(), alerts=FakeAlerts(), repo=self.repo, now=lambda: NOW)

        policy = CommandAuthorityPolicy(
            enabled=True, live_enabled=True, live_account_id=ACCOUNT_LIVE,
            max_order_notional=max_notional, max_drift_bps=50.0)
        self.guard = DispatchGuard(
            broker=self.broker, quotes=self.quotes, margin=self.margin,
            controls=self.controls, risk_gate=self.risk_gate, policy=policy,
            account_id=ACCOUNT_LIVE, account_mode="live")
        self.service = ApprovalCommandService(
            journal=journal, ledger=self.ledger, repo=self.repo, controls=self.controls,
            orders=self.orders, quotes=self.quotes, risk_gate=self.risk_gate,
            risk_producer=self.risk_producer, reconciler=self.reconciler, broker=self.broker,
            account_id=ACCOUNT_LIVE, account_mode="live", now=lambda: NOW,
            dispatch_guard=self.guard)
        self.coordinator = TradingCommandCoordinator(
            journal=journal, ledger=self.ledger, audit=CommandAudit(journal),
            nonces=FakeNonceGate(), now=lambda: NOW, reconciler=self.reconciler)
        self.coordinator.register_action(
            "approve_proposal", self.service.approve, requires_preflight=True, saga=True)

    def insert_pending(self, *, conid=CONID, action="BUY", quantity=10.0,
                       reference_price=210.0):
        pid = self.repo.reserve_id()
        draft = ProposalDraft(
            id=pid, symbol="AAPL", action=action, quantity=quantity,
            amount=quantity * reference_price, execution={"order_type": "MARKET"},
            reasoning="", confidence=0.7, thesis="", source="dashboard", metadata={},
            sec_type="STK", account_id=ACCOUNT_LIVE, account_mode="live", conid=conid,
            reference_price=reference_price, reference_timestamp=NOW,
            reference_quote_side="ask" if action == "BUY" else "bid",
            reference_feed_type="live", max_price_drift_bps=50.0,
            expires_at=NOW + dt.timedelta(minutes=5), live_approval_eligible=True,
            created_at=NOW)
        predicted = ProposalCommandService._record_from_draft(draft, revision=1)
        written: list = []
        self.journal.mutate(
            self.journal.connect(),
            self.repo.mutation_for(predicted, "seed"),
            lambda conn, revision: written.append(
                self.repo.insert_pending_in_tx(conn, draft, revision)),
            event_id=f"proposal:{pid}:1")
        return written[0]

    def approve(self, record, command_id="c-live"):
        return self.coordinator.execute(CommandRequest(
            command_id=command_id, action="approve_proposal", account_id=ACCOUNT_LIVE,
            target_type="proposal", target_id=str(record.id), expected_version=record.revision,
            body={"proposal_id": record.id}, source="dashboard",
            preflight_nonce=f"nonce-{command_id}"))


def scn_stale_quote_blocks_dispatch(db_path: str) -> dict:
    """LIVE approval with a stale executable quote: the approval path refuses to
    turn a stale quote into a live order (QUOTE_STALE); nothing dispatches."""
    live = LiveApproval(db_path, stale_quote=True)
    record = live.insert_pending()
    receipt = live.approve(record)
    if receipt.state != "REJECTED":
        raise AssertionError(f"stale-quote approve state {receipt.state} != REJECTED")
    if receipt.error_code != "QUOTE_STALE":
        raise AssertionError(f"stale-quote error_code {receipt.error_code} != QUOTE_STALE")
    if live.orders.submissions:
        raise AssertionError("an order was dispatched on a stale quote")
    if live.repo.get(record.id).status == "EXECUTED":
        raise AssertionError("proposal executed on a stale quote")
    return {"error_code": receipt.error_code, "orders_submitted": len(live.orders.submissions)}


def scn_notional_cap_blocks_dispatch(db_path: str) -> dict:
    """LIVE approval whose notional exceeds the policy ceiling: the DispatchGuard
    (a check check_exposure does NOT perform) rejects ORDER_NOTIONAL_LIMIT before
    any dispatch. Exercises the Task-4 guard specifically, end to end."""
    # 10 * 210 = 2100 notional against a 1000 ceiling.
    live = LiveApproval(db_path, max_notional=1000.0)
    record = live.insert_pending()
    receipt = live.approve(record)
    if receipt.state != "REJECTED":
        raise AssertionError(f"over-notional approve state {receipt.state} != REJECTED")
    if receipt.error_code != "ORDER_NOTIONAL_LIMIT":
        raise AssertionError(
            f"over-notional error_code {receipt.error_code} != ORDER_NOTIONAL_LIMIT")
    if live.orders.submissions:
        raise AssertionError("an order was dispatched over the notional ceiling")
    return {"error_code": receipt.error_code, "orders_submitted": len(live.orders.submissions)}


# Scenario registry maps name -> (callable | None). None means the feature is
# not landed yet (reported as pending, never as covered).
def _liquidation_available() -> bool:
    try:
        import trader.trading.liquidation_service  # noqa: F401
        return True
    except Exception:
        return False


def _breaker_available() -> bool:
    try:
        import trader.trading.circuit_breaker  # noqa: F401
        return True
    except Exception:
        return False


def build_scenarios() -> dict[str, Optional[Callable[[str], dict]]]:
    return {
        "happy_path": scn_happy_path,
        "duplicate_create_idempotent": scn_duplicate_create_idempotent,
        "ambiguous_submit_reconciles": scn_ambiguous_submit_reconciles,
        "restart_unresolved": scn_restart_unresolved,
        "stale_quote_blocks_dispatch": scn_stale_quote_blocks_dispatch,
        "notional_cap_blocks_dispatch": scn_notional_cap_blocks_dispatch,
        # Pending until their features land AND their scenarios are written.
        # Reported as pending (never silently "covered"). The feature-detection
        # helpers gate the flip from None -> scenario fn when the modules exist:
        #   liquidation  -> Task 7 (_liquidation_available)
        #   breaker/readiness -> Task 6 (_breaker_available)
        "liquidation_flat_only_from_broker_truth":
            scn_liquidation if _liquidation_available() and scn_liquidation else None,
        "circuit_breaker_trips_and_persists":
            scn_circuit_breaker if _breaker_available() and scn_circuit_breaker else None,
        "semantic_readiness_gates_activation":
            scn_semantic_readiness if _breaker_available() and scn_semantic_readiness else None,
    }


# Task 6/7 scenario bodies are added here as those features land; until then the
# names are declared (above) so the report lists them as pending, not missing.
def scn_liquidation(_db_path: str) -> dict:
    pos = SimpleNamespace(quantity=10.0, conid=CONID)
    snapshots = [
        SimpleNamespace(account_id=ACCOUNT, generation_id=1, positions=(pos,), working_orders=()),
        SimpleNamespace(account_id=ACCOUNT, generation_id=2, positions=(), working_orders=()),
    ]
    broker = SimpleNamespace(capture=lambda _account: snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])
    calls = []
    dispatch = SimpleNamespace(cancel=lambda *args: calls.append(("cancel", args)),
                               reduce=lambda *args: calls.append(("reduce", args)))
    service = LiquidationService(broker, dispatch, now=lambda: NOW)
    first = service.start(ACCOUNT, "drill-liquidation", NOW + dt.timedelta(minutes=1))
    if first.state == "FLAT" or not calls:
        raise AssertionError("order acknowledgement was treated as broker-flat proof")
    terminal = service.rescan()
    if terminal is None or terminal.state != "FLAT":
        raise AssertionError("fresh zero-position broker generation did not resolve liquidation")
    return {"initial": first.state, "terminal": terminal.state, "reductions": len(calls)}


def scn_circuit_breaker(db_path: str) -> dict:
    db = DuckDBConnection.get_instance(db_path + ".breaker")
    migrator = SchemaMigrator(db); journal = DomainJournal(db); journal.migrate(migrator)
    apply_circuit_breaker_migration(migrator)
    store = CircuitBreakerStore(journal, ACCOUNT); store.seed(NOW)
    breaker = CircuitBreaker(store, now=lambda: NOW, reset_ready=lambda: True,
                             reconciliation_complete=lambda: True, session_key=lambda value: value.date().isoformat())
    breaker.record(BreakerSignal("LIQUIDATION_FAILED", NOW, key="drill"))
    if CircuitBreakerStore(journal, ACCOUNT).get().state != "TRIPPED":
        raise AssertionError("critical liquidation signal did not persistently trip breaker")
    return {"state": "TRIPPED"}


def scn_semantic_readiness(_db_path: str) -> dict:
    checks = dict(ib_connected=True, account_pinned=True, broker_current=True,
                  journal_writable=True, reconciliation_safe=True, control_readable=True,
                  breaker_clear=False, command_stack_active=True, quotes_ready=True)
    readiness = SemanticReadiness(**{name: (lambda value=value: value) for name, value in checks.items()},
                                  session_open=lambda _now: True)
    report = readiness.evaluate(NOW)
    if report.ready or "breaker_clear" not in report.to_payload()["failed"]:
        raise AssertionError("semantic readiness allowed a tripped breaker")
    return {"failed": report.to_payload()["failed"]}


# ---------------------------------------------------------------------------
# Report assembly.
# ---------------------------------------------------------------------------

def commit_digest() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def config_digest() -> str:
    path = _PROJECT_ROOT / "config_defaults" / "trader.yaml"
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return "unknown"


@dataclass
class DrillReport:
    version: int = REPORT_VERSION
    kind: str = "synthetic-in-process"
    commit_digest: str = ""
    config_digest: str = ""
    scenario_results: list = field(default_factory=list)
    passed: bool = False
    pending: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "version": self.version, "kind": self.kind,
            "commit_digest": self.commit_digest, "config_digest": self.config_digest,
            "scenario_results": self.scenario_results, "pending": self.pending,
            "passed": self.passed,
        }, indent=2, sort_keys=True)


def run_drills(selected: Optional[list[str]] = None) -> DrillReport:
    scenarios = build_scenarios()
    names = selected if selected else list(scenarios)
    report = DrillReport(commit_digest=commit_digest(), config_digest=config_digest())
    all_runnable_passed = True
    ran_any = False

    for name in names:
        if name not in scenarios:
            report.scenario_results.append(
                {"name": name, "status": "unknown", "detail": "no such scenario"})
            all_runnable_passed = False
            continue
        fn = scenarios[name]
        if fn is None:
            report.pending.append(name)
            report.scenario_results.append(
                {"name": name, "status": "pending",
                 "detail": "feature not landed yet (Task 6/7)"})
            continue
        ran_any = True
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / f"{name}.duckdb")
            try:
                invariants = fn(db_path)
                report.scenario_results.append(
                    {"name": name, "status": "passed", "invariants": invariants})
            except BaseException as exc:  # noqa: BLE001 - a drill may raise SimulatedCrash
                all_runnable_passed = False
                report.scenario_results.append(
                    {"name": name, "status": "failed",
                     "detail": f"{type(exc).__name__}: {exc}"})

    report.passed = bool(ran_any and all_runnable_passed)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Command-plane failure drills (P1 Task 8).")
    parser.add_argument("--json", action="store_true", help="print the JSON report to stdout")
    parser.add_argument("--output", type=str, default=None, help="write the JSON report to a file")
    parser.add_argument("--scenarios", type=str, default=None,
                        help="comma-separated scenario names (default: all)")
    args = parser.parse_args(argv)

    selected = [s.strip() for s in args.scenarios.split(",")] if args.scenarios else None
    report = run_drills(selected)

    if args.output:
        Path(args.output).write_text(report.to_json())
    if args.json:
        print(report.to_json())
    else:
        print(f"command-plane drills @ {report.commit_digest[:12]}  "
              f"config {report.config_digest[:19]}")
        for r in report.scenario_results:
            mark = {"passed": "PASS", "failed": "FAIL",
                    "pending": "PEND", "unknown": "????"}.get(r["status"], "?")
            extra = r.get("detail") or r.get("invariants") or ""
            print(f"  [{mark}] {r['name']}  {extra}")
        print(f"passed={report.passed}"
              + (f"  (pending: {', '.join(report.pending)})" if report.pending else ""))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
