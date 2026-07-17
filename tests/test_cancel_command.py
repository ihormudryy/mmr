"""[M1-F3] Task 6 — working-order cancel authority.

Step-1 contract tests for ``classify_cancel`` and the ``cancel_order`` /
``cancel_orders`` saga, driven through the ``TradingCommandCoordinator`` (the
sole production mutation boundary) via fakes for every collaborator that
would otherwise talk to [M1-F2]'s broker-state store or the broker itself.

Governed by ``.superpowers/sdd/m1f3-task-6-addendum.md`` over the base brief
(``.superpowers/sdd/m1f3-task-6-brief.md``):
  - consumes the REAL ``trader.data.broker_state.BrokerOrderRow`` (real field
    names: ``leg``, ``total_quantity``; no ``is_terminal`` field — terminality
    is derived from ``status``/``deleted``), not the brief's illustrative
    (and partly wrong) parallel dataclass;
  - ``cancel_orders`` children use colon-free ids (``f"{root}-{index}"``),
    not the brief's colon-bearing ``f"{root}:{order_entity_id}"`` scheme.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerOrderRow
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import (
    CancelCommandService,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    RiskDirection,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
    classify_cancel,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
ACCOUNT_ID = "DU111111"


# ---------------------------------------------------------------------------
# Test helper: build a real BrokerOrderRow (all fields, sensible defaults),
# keyed by the REAL leg/status/deleted semantics (addendum §1/§2/§3).
# ---------------------------------------------------------------------------

def _order(
    order_entity_id: str,
    *,
    leg,
    status: str,
    conid: int = 265598,
    is_terminal: bool = False,
) -> BrokerOrderRow:
    return BrokerOrderRow(
        order_entity_id=order_entity_id,
        account_id=ACCOUNT_ID,
        conid=conid,
        symbol="AAPL",
        order_group_id=f"og-{order_entity_id}",
        leg=leg,
        is_external=False,
        action="SELL",
        order_type="MKT",
        total_quantity=10.0,
        filled_quantity=0.0,
        avg_fill_price=None,
        limit_price=None,
        stop_price=None,
        tif="DAY",
        status=status,
        deleted=is_terminal,
        revision=1,
        source_timestamp=NOW,
    )


# ---------------------------------------------------------------------------
# Fakes for every collaborator the saga touches (none reach IB / [M1-F2]).
# ---------------------------------------------------------------------------

class FakeOrdersView:
    """Test double for ``OrderStateView`` -- a conn-free ``get_order`` seam
    backed by real ``BrokerOrderRow`` instances."""

    def __init__(self):
        self._orders: dict[str, BrokerOrderRow] = {}

    def add(self, order: BrokerOrderRow) -> None:
        self._orders[order.order_entity_id] = order

    def get_order(self, order_entity_id: str):
        return self._orders.get(order_entity_id)


class FakeDispatch:
    """Test double for ``OrderDispatchPort``: records cancel correlation as
    ``(order_entity_id, order_ref)`` tuples, can be told to raise once."""

    def __init__(self):
        self.cancelled: list[tuple[str, str]] = []
        self._raise = None

    def raise_on_cancel(self, exc: Exception) -> None:
        self._raise = exc

    def cancel(self, order_entity_id, order_ref):
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        self.cancelled.append((order_entity_id, order_ref))
        return SimpleNamespace(order_entity_id=order_entity_id, cancelled=True)

    def submit(self, proposal, order_ref, order_group_id):  # pragma: no cover - not exercised
        raise NotImplementedError

    def find_by_order_ref(self, account_id, order_ref):  # pragma: no cover - not exercised
        return []

    def enumeration_complete(self):  # pragma: no cover - not exercised
        return True


class FakeReconciler:
    def __init__(self):
        self.scheduled: list[str] = []

    def schedule(self, command_id, now):
        self.scheduled.append(command_id)


class FakeRiskProducer:
    def __init__(self):
        # (risk_id, payload, correlation_id) -- correlation_id is captured so a
        # fan-out child's decision can be checked to carry the ROOT correlation
        # while retaining its own per-child risk_id (Fix 5).
        self.decisions: list[tuple[str, dict, "str | None"]] = []

    def publish_decision(self, command_id, payload, correlation_id=None):
        self.decisions.append((command_id, payload, correlation_id))


class FakeNonceGate:
    """Test double for ``PreflightNonceGate``: each nonce string is single-use."""

    def __init__(self):
        self._consumed: set[str] = set()

    def consume_in_tx(self, conn, nonce, request) -> bool:
        if not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _build_cancel(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)

    ledger = CommandLedger(journal)
    now = lambda: NOW  # noqa: E731

    orders_view = FakeOrdersView()
    dispatch = FakeDispatch()
    reconciler = FakeReconciler()
    nonces = FakeNonceGate()
    risk_producer = FakeRiskProducer()

    coordinator = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=CommandAudit(journal),
        nonces=nonces, now=now,
    )
    service = CancelCommandService(
        journal=journal, ledger=ledger, orders_view=orders_view, dispatch=dispatch,
        nonces=nonces, risk_producer=risk_producer, reconciler=reconciler,
        coordinator=coordinator, now=now,
    )
    coordinator.register_action(
        "cancel_order", service.cancel_order, requires_preflight=False, saga=True,
    )
    coordinator.register_action(
        "cancel_orders", service.cancel_orders, requires_preflight=False,
    )

    def execute(action, body, *, command_id, nonce=None):
        request = CommandRequest(
            command_id=command_id, action=action, account_id=ACCOUNT_ID,
            target_type="order" if action == "cancel_order" else "order_group",
            target_id=body.get("order_entity_id", ""), expected_version=None,
            body=body, source="dashboard", preflight_nonce=nonce,
        )
        return coordinator.execute(request)

    def journal_correlations(command_id: str) -> set:
        events = journal.read_after(0, 10_000)
        return {
            event.correlation_id for event in events
            if event.entity_type == "command" and event.entity_id == command_id
        }

    return SimpleNamespace(
        db=db, journal=journal, ledger=ledger, coordinator=coordinator, service=service,
        orders_view=orders_view, dispatch=dispatch, reconciler=reconciler, nonces=nonces,
        risk_producer=risk_producer, execute=execute, journal_correlations=journal_correlations,
        now=now,
    )


@pytest.fixture
def cancel(tmp_path):
    return _build_cancel(tmp_path)


@pytest.fixture
def cancel_live(tmp_path):
    return _build_cancel(tmp_path)


# ---------------------------------------------------------------------------
# Step-1 contract tests (base brief, with addendum's field/id corrections).
# ---------------------------------------------------------------------------

def test_entry_cancel_is_risk_reducing_and_immediate(cancel):
    cancel.orders_view.add(_order("ord-1", leg="entry", status="Submitted"))
    receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-1"}, command_id="c1")
    assert receipt.state == "SUBMITTED"
    assert cancel.dispatch.cancelled == [("ord-1", "c1")]      # dispatch correlation carries the command_id
    assert classify_cancel(cancel.orders_view.get_order("ord-1")) is RiskDirection.REDUCING
    # Fix 6 (test-honesty): the docstring promises the risk decision is
    # published exactly once on a saga-path cancel -- pin it so deleting the
    # call can no longer keep the suite green.
    assert len(cancel.risk_producer.decisions) == 1
    risk_id, payload, _correlation = cancel.risk_producer.decisions[0]
    assert risk_id == "c1"
    assert payload["decision"] == "cancel"


def test_protective_leg_cancel_is_risk_increasing_and_names_the_position(cancel_live):
    cancel_live.orders_view.add(_order("ord-2", leg="stop", status="Submitted", conid=265598))
    without_nonce = cancel_live.execute(
        "cancel_order", {"order_entity_id": "ord-2"}, command_id="c1", nonce=None)
    assert without_nonce.error_code == "PREFLIGHT_REQUIRED"     # §9.1 ceremony for a risk-increasing cancel
    assert without_nonce.retryable is True                      # Fix 6: mint a fresh nonce + retry
    with_nonce = cancel_live.execute(
        "cancel_order", {"order_entity_id": "ord-2"}, command_id="c1b", nonce="n-1")
    assert with_nonce.state == "SUBMITTED"
    assert with_nonce.outcome["unprotected_conid"] == 265598


def test_unclassifiable_order_is_treated_as_protective(cancel_live):
    # Addendum §2: a None leg (external / no group) is unclassifiable and
    # treated as protective -- fail safe toward requiring the ceremony.
    cancel_live.orders_view.add(_order("ord-3", leg=None, status="Submitted"))
    receipt = cancel_live.execute(
        "cancel_order", {"order_entity_id": "ord-3"}, command_id="c1", nonce=None)
    assert receipt.error_code == "PREFLIGHT_REQUIRED"
    assert classify_cancel(cancel_live.orders_view.get_order("ord-3")) is RiskDirection.INCREASING


def test_cancelling_a_terminal_order_is_a_noop_reporting_state(cancel):
    cancel.orders_view.add(_order("ord-4", leg="entry", status="Filled", is_terminal=True))
    receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-4"}, command_id="c1")
    assert receipt.state == "RESOLVED"
    assert receipt.outcome == {"noop": True, "authoritative_status": "Filled"}
    assert cancel.dispatch.cancelled == []


def test_cancel_all_expands_under_one_correlation_id(cancel):
    cancel.orders_view.add(_order("ord-5", leg="entry", status="Submitted"))
    cancel.orders_view.add(_order("ord-6", leg="entry", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-5", "ord-6"]}, command_id="root-1")
    assert receipt.correlation_id == "root-1"
    children = [cancel.ledger.get(c) for c in receipt.outcome["child_command_ids"]]
    assert {c.state for c in children} == {"SUBMITTED"}
    assert all(cancel.journal_correlations(c.command_id) == {"root-1"} for c in children)


def test_cancel_timeout_reconciles_like_other_order_commands(cancel):
    cancel.orders_view.add(_order("ord-7", leg="entry", status="Submitted"))
    cancel.dispatch.raise_on_cancel(TimeoutError("ack lost"))
    receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-7"}, command_id="c1")
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert cancel.reconciler.scheduled == ["c1"]


# ---------------------------------------------------------------------------
# Additional coverage beyond the Step-1 list: guards not exercised above.
# ---------------------------------------------------------------------------

def test_missing_order_is_rejected_never_blind_cancelled(cancel):
    receipt = cancel.execute("cancel_order", {"order_entity_id": "does-not-exist"}, command_id="c1")
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "ORDER_NOT_FOUND"
    assert receipt.retryable is True                            # Fix 6: ORDER_NOT_FOUND is retryable
    assert cancel.dispatch.cancelled == []


def test_classify_cancel_of_missing_order_is_increasing():
    assert classify_cancel(None) is RiskDirection.INCREASING


def test_cancel_orders_rejects_empty_list(cancel):
    receipt = cancel.execute("cancel_orders", {"order_entity_ids": []}, command_id="root-empty")
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "ORDER_ENTITY_IDS_REQUIRED"


def test_post_dispatch_finish_failure_degrades_to_ambiguous(cancel):
    # Mirrors approve's fix2 (FINISH-TX-AFTER-DISPATCH): the broker cancel is
    # already live (dispatch.cancel returned) when the post-dispatch ledger
    # transition fails -- this must degrade to OUTCOME_UNKNOWN/
    # DISPATCH_AMBIGUOUS and schedule the reconciler, never a raw exception.
    cancel.orders_view.add(_order("ord-8", leg="entry", status="Submitted"))
    real_transition_in_tx = cancel.ledger.transition_in_tx

    def flaky_transition(conn, command_id, from_state, to_state, **kwargs):
        if to_state == "SUBMITTED":
            raise RuntimeError("duckdb IOException")
        return real_transition_in_tx(conn, command_id, from_state, to_state, **kwargs)

    cancel.ledger.transition_in_tx = flaky_transition
    try:
        receipt = cancel.execute("cancel_order", {"order_entity_id": "ord-8"}, command_id="c1")
    finally:
        cancel.ledger.transition_in_tx = real_transition_in_tx

    assert receipt.state == "OUTCOME_UNKNOWN"
    assert receipt.error_code == "DISPATCH_AMBIGUOUS"
    assert receipt.retryable is False
    assert cancel.dispatch.cancelled == [("ord-8", "c1")]   # the cancel WAS dispatched
    assert cancel.reconciler.scheduled == ["c1"]


def test_outcome_unknown_cancel_replay_is_not_retryable(cancel):
    cancel.orders_view.add(_order("ord-9", leg="entry", status="Submitted"))
    cancel.dispatch.raise_on_cancel(TimeoutError("ack lost"))
    first = cancel.execute("cancel_order", {"order_entity_id": "ord-9"}, command_id="c1")
    assert first.state == "OUTCOME_UNKNOWN" and first.retryable is False

    fetched = cancel.coordinator.get_command("c1")
    assert fetched.state == "OUTCOME_UNKNOWN"
    assert fetched.retryable is False


# ---------------------------------------------------------------------------
# Fix 1 — batch ceremony: the nonce is consumed ONCE at the root, not per child.
# ---------------------------------------------------------------------------

def test_cancel_all_protective_legs_dispatch_under_one_ceremony(cancel):
    # Two protective (risk-INCREASING) legs in one batch, sharing ONE nonce.
    # §9.7: a cancel-all expands under ONE correlation id and ONE confirmation
    # -- so BOTH children must dispatch on the single ceremony. Before the fix
    # the root forwarded the same single-use nonce to each child, so the 2nd
    # protective child re-consumed an already-spent nonce and was rejected
    # PREFLIGHT_REQUIRED (only the FIRST dispatched).
    cancel.orders_view.add(_order("ord-p1", leg="stop", status="Submitted"))
    cancel.orders_view.add(_order("ord-p2", leg="take_profit", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-p1", "ord-p2"]},
        command_id="root-prot", nonce="n-batch")
    assert receipt.state == "RESOLVED"
    children = [cancel.ledger.get(c) for c in receipt.outcome["child_command_ids"]]
    assert {c.state for c in children} == {"SUBMITTED"}
    assert sorted(oid for oid, _ref in cancel.dispatch.cancelled) == ["ord-p1", "ord-p2"]


def test_cancel_all_protective_legs_missing_nonce_rejects_whole_batch(cancel):
    # A batch that needs the ceremony but carries no nonce rejects the WHOLE
    # batch at the root (before any fan-out) -- no child command is minted and
    # no broker cancel is dispatched.
    cancel.orders_view.add(_order("ord-p3", leg="stop", status="Submitted"))
    cancel.orders_view.add(_order("ord-p4", leg="stop", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-p3", "ord-p4"]},
        command_id="root-nonce", nonce=None)
    assert receipt.state == "REJECTED"
    assert receipt.error_code == "PREFLIGHT_REQUIRED"
    assert cancel.dispatch.cancelled == []
    assert cancel.ledger.get("root-nonce-0") is None    # no child was ever minted
    assert cancel.ledger.get("root-nonce-1") is None


def test_cancel_all_all_reducing_needs_no_ceremony(cancel):
    # An all-REDUCING batch needs no ceremony: it dispatches with no nonce and
    # never touches the nonce gate.
    cancel.orders_view.add(_order("ord-r1", leg="entry", status="Submitted"))
    cancel.orders_view.add(_order("ord-r2", leg="entry", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-r1", "ord-r2"]},
        command_id="root-red", nonce=None)
    assert receipt.state == "RESOLVED"
    children = [cancel.ledger.get(c) for c in receipt.outcome["child_command_ids"]]
    assert {c.state for c in children} == {"SUBMITTED"}
    assert len(cancel.dispatch.cancelled) == 2


# ---------------------------------------------------------------------------
# Fix 2 — the root outcome reflects per-child truth (no masked partial failure).
# ---------------------------------------------------------------------------

def test_cancel_orders_outcome_reflects_partial_failure(cancel):
    # One child dispatches cleanly; the other hits an ambiguous dispatch
    # (TimeoutError) -> OUTCOME_UNKNOWN. The root outcome must carry each
    # child's real state and flag partial_failure -- before the fix the root
    # resolved identically whether children succeeded or not.
    cancel.orders_view.add(_order("ord-m1", leg="entry", status="Submitted"))
    cancel.orders_view.add(_order("ord-m2", leg="entry", status="Submitted"))
    cancel.dispatch.raise_on_cancel(TimeoutError("ack lost"))   # first cancel raises once
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-m1", "ord-m2"]}, command_id="root-mix")
    assert receipt.state == "RESOLVED"
    assert receipt.outcome["partial_failure"] is True
    children = receipt.outcome["children"]
    assert children["ord-m1"]["state"] == "OUTCOME_UNKNOWN"
    assert children["ord-m1"]["error_code"] == "DISPATCH_AMBIGUOUS"
    assert children["ord-m1"]["classification"] == "REDUCING"
    assert children["ord-m2"]["state"] == "SUBMITTED"


def test_cancel_orders_all_success_is_not_partial_failure(cancel):
    cancel.orders_view.add(_order("ord-s1", leg="entry", status="Submitted"))
    cancel.orders_view.add(_order("ord-s2", leg="entry", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-s1", "ord-s2"]}, command_id="root-ok")
    assert receipt.outcome["partial_failure"] is False
    assert {c["state"] for c in receipt.outcome["children"].values()} == {"SUBMITTED"}


# ---------------------------------------------------------------------------
# Fix 3 — per-child fault isolation: one child's exception doesn't abort the batch.
# ---------------------------------------------------------------------------

def test_cancel_orders_child_execute_exception_is_isolated(cancel):
    # The MIDDLE child's execute raises. Before the fix that aborted the whole
    # loop, dropping the later order with no record. Now the failure is
    # isolated: the later child still dispatches and the failed child is marked.
    for oid in ["ord-a", "ord-b", "ord-c"]:
        cancel.orders_view.add(_order(oid, leg="entry", status="Submitted"))
    real_execute = cancel.coordinator.execute

    def flaky_execute(request):
        if request.command_id == "root-iso-1":      # the middle child (ord-b)
            raise RuntimeError("child blew up")
        return real_execute(request)

    cancel.coordinator.execute = flaky_execute
    try:
        receipt = cancel.execute(
            "cancel_orders", {"order_entity_ids": ["ord-a", "ord-b", "ord-c"]},
            command_id="root-iso")
    finally:
        cancel.coordinator.execute = real_execute

    assert receipt.state == "RESOLVED"
    children = receipt.outcome["children"]
    assert children["ord-a"]["state"] == "SUBMITTED"
    assert children["ord-b"]["state"] == "FAILED"
    assert children["ord-c"]["state"] == "SUBMITTED"      # later child still reached
    assert receipt.outcome["partial_failure"] is True
    assert ("ord-c", "root-iso-2") in cancel.dispatch.cancelled


# ---------------------------------------------------------------------------
# Fix 4 — dedupe repeated order_entity_ids.
# ---------------------------------------------------------------------------

def test_cancel_orders_dedupes_repeated_order_ids(cancel):
    cancel.orders_view.add(_order("ord-1", leg="entry", status="Submitted"))
    cancel.orders_view.add(_order("ord-2", leg="entry", status="Submitted"))
    receipt = cancel.execute(
        "cancel_orders", {"order_entity_ids": ["ord-1", "ord-1", "ord-2"]},
        command_id="root-dup")
    assert len(receipt.outcome["child_command_ids"]) == 2
    assert len(cancel.dispatch.cancelled) == 2
    assert sorted(oid for oid, _ref in cancel.dispatch.cancelled) == ["ord-1", "ord-2"]


# ---------------------------------------------------------------------------
# Fix 5 — a fan-out child's risk decision carries the ROOT correlation id
# while retaining its own per-child risk_id.
# ---------------------------------------------------------------------------

def test_fanout_child_decision_carries_root_correlation(cancel):
    cancel.orders_view.add(_order("ord-x", leg="entry", status="Submitted"))
    cancel.execute("cancel_orders", {"order_entity_ids": ["ord-x"]}, command_id="root-corr")
    assert len(cancel.risk_producer.decisions) == 1
    risk_id, payload, correlation_id = cancel.risk_producer.decisions[0]
    assert risk_id == "root-corr-0"          # per-child risk_id (its own command_id) is retained
    assert correlation_id == "root-corr"     # decision correlates to the root, not the child
    assert payload["decision"] == "cancel"
