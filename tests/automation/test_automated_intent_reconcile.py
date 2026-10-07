"""Issue #60: an automated BUY must not stay SUBMITTED once the broker shows its orders.

A SUBMITTED command blocks every later check that reads ``ledger.unresolved_for_account``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.automation.test_automated_command_boundary import (
    ACCOUNT, ARTIFACT_DIGEST, NOW, _build_stack, intent_to_request_body, make_intent,
)
from trader.trading.command_coordinator import CommandRequest, OutcomeReconciler
from trader.trading.order_correlation import encode_order_ref


class BrokerOrders:
    """The reconciler's read seam: broker rows by order ref, plus the enumeration fence."""

    def __init__(self, rows=(), *, complete=True):
        self.rows = list(rows)
        self.complete = complete
        self.refs: list[str] = []

    def find_by_order_ref(self, account_id, order_ref):
        self.refs.append(order_ref)
        return list(self.rows)

    def enumeration_complete(self):
        return self.complete


def order(status, *, filled=0.0, leg="entry"):
    return SimpleNamespace(status=status, filled_quantity=filled, leg=leg)


@pytest.fixture
def submitted_buy(tmp_path):
    stack = _build_stack(tmp_path)
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir()
    intent = make_intent()
    receipt = stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service",
    ))
    assert receipt.state == "SUBMITTED"
    stack.command_id = intent.command_id
    return stack


def reconcile(stack, orders):
    reconciler = OutcomeReconciler(
        journal=stack.journal, ledger=stack.ledger, orders=orders, strategy=SimpleNamespace(),
        alerts=SimpleNamespace(raise_alert=lambda *args: None), now=lambda: NOW,
    )
    return reconciler.reconcile_once(stack.command_id, NOW)


def test_a_submitted_buy_is_scheduled_for_reconciliation(submitted_buy):
    assert submitted_buy.schedule.calls >= 1


@pytest.mark.parametrize("status", ["Submitted", "PreSubmitted", "PendingSubmit"])
def test_a_buy_the_broker_shows_working_is_resolved(submitted_buy, status):
    orders = BrokerOrders([order(status), order("PreSubmitted", leg="stop")])
    assert reconcile(submitted_buy, orders).resolved
    row = submitted_buy.ledger.get(submitted_buy.command_id)
    assert row.state == "RESOLVED" and row.outcome["broker_acknowledged"] is True
    assert row.outcome["order_group_id"] == f"og-{submitted_buy.command_id}"
    assert orders.refs == [encode_order_ref(f"og-{submitted_buy.command_id}")]


def test_a_filled_buy_is_resolved(submitted_buy):
    orders = BrokerOrders([order("Filled", filled=10.0), order("Cancelled", leg="stop")])
    assert reconcile(submitted_buy, orders).resolved
    assert submitted_buy.ledger.get(submitted_buy.command_id).state == "RESOLVED"


@pytest.mark.parametrize("status", ["Inactive", "Cancelled", "ApiCancelled"])
def test_a_buy_the_broker_rejected_or_cancelled_with_no_fill_is_failed(submitted_buy, status):
    orders = BrokerOrders([order(status), order("Cancelled", leg="stop")])
    assert reconcile(submitted_buy, orders).resolved
    row = submitted_buy.ledger.get(submitted_buy.command_id)
    assert (row.state, row.error_code) == ("REJECTED", "BROKER_REJECTED")
    assert row.outcome["broker_acknowledged"] is False


def test_a_failed_verdict_needs_a_complete_enumeration(submitted_buy):
    orders = BrokerOrders([order("Inactive")], complete=False)
    assert not reconcile(submitted_buy, orders).resolved
    assert submitted_buy.ledger.get(submitted_buy.command_id).state == "SUBMITTED"


def test_a_buy_with_no_broker_evidence_stays_unresolved(submitted_buy):
    assert not reconcile(submitted_buy, BrokerOrders([])).resolved
    assert submitted_buy.ledger.get(submitted_buy.command_id).state == "SUBMITTED"


def test_an_unreadable_order_status_stays_unresolved(submitted_buy):
    orders = BrokerOrders([SimpleNamespace(leg="entry")])
    assert not reconcile(submitted_buy, orders).resolved
    assert submitted_buy.ledger.get(submitted_buy.command_id).state == "SUBMITTED"


def test_a_resolved_buy_no_longer_blocks_later_checks(submitted_buy):
    assert [r.command_id for r in submitted_buy.ledger.unresolved_for_account(ACCOUNT)] == [submitted_buy.command_id]
    reconcile(submitted_buy, BrokerOrders([order("Submitted")]))
    assert submitted_buy.ledger.unresolved_for_account(ACCOUNT) == []
