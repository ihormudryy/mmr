"""Issue #142: a publish_ai_risk_policy a crash left RECEIVED settles from its revision row.

The revision is written, with the command id, in one store transaction, and nothing durable is written before it
while the ledger row is RECEIVED. So the revision proves the commit and its absence proves the publish never ran.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trader.automation.ai_risk_policy import AiRiskPolicyService, apply_ai_risk_policy_migration
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.risk_limits import PAPER_LIMITS
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import (
    CRITICAL_AFTER_SECONDS, OPERATOR_SETTLEABLE_ACTIONS, POLICY_NOT_COMMITTED, RECEIVED_AT_RESTART,
    CommandLedger, OperatorSettleRefused, OutcomeReconciler, apply_command_ledger_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)          # Friday, mid-session
BEFORE = NOW - dt.timedelta(minutes=1)
ACCOUNT = "DU111111"
OTHER_ACCOUNT = "DU999999"
CEILING = replace(PAPER_LIMITS, gross_fraction=0.10)
LOOSE = replace(PAPER_LIMITS, gross_fraction=0.08)
TIGHT = replace(PAPER_LIMITS, gross_fraction=0.04, position_fraction=0.04)


class Alerts:
    def __init__(self):
        self.raised: list[str] = []

    def raise_alert(self, command_id, detail):
        self.raised.append(command_id)


class NoOrders:
    def find_by_order_ref(self, account_id, order_ref):
        return []

    def enumeration_complete(self):
        return False


class NoStrategy:
    def get_receipt(self, command_id):
        return None


class BrokenPolicies:
    def committed_outcome(self, command_id):
        raise RuntimeError("journal unreadable")


def _policy(db, account=ACCOUNT):
    return AiRiskPolicyService(db=db, account_id=account, ceiling=CEILING, calendar=XNYSCalendarPolicy(),
                               now=lambda: NOW)


def _publish(policy, limits, command_id, *, account=ACCOUNT):
    broker = BrokerRiskSnapshot(generation_id=7, source_cursor=3, promoted_at=NOW, account_id=account,
                                account_mode="paper", net_liquidation=1_000_000, daily_pnl=0, positions=(),
                                working_orders=())
    return policy.publish(limits, reason="test", principal="cli", command_id=command_id, broker=broker)


@pytest.fixture
def world(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_ai_risk_policy_migration(migrator)
    ledger = CommandLedger(journal)
    alerts = Alerts()

    def reconciler(policies="policy"):
        """A trader process built now: its start snapshot is the RECEIVED rows already written."""
        port = _policy(db) if policies == "policy" else policies
        return OutcomeReconciler(journal=journal, ledger=ledger, orders=NoOrders(), strategy=NoStrategy(),
                                 alerts=alerts, now=lambda: NOW, policies=port)

    return SimpleNamespace(db=db, ledger=ledger, alerts=alerts, reconciler=reconciler)


def _received(world, command_id, *, state="RECEIVED"):
    world.ledger.insert_for_test(command_id, state=state, updated_at=BEFORE, created_at=BEFORE,
                                 account_id=ACCOUNT, action="publish_ai_risk_policy", target_type="account",
                                 target_id=ACCOUNT)


def test_a_crashed_publish_settles_from_its_revision_row_after_a_restart(world):
    _publish(_policy(world.db), LOOSE, "pol-1")                  # committed; the ledger move to RESOLVED was lost
    _publish(_policy(world.db), TIGHT, "pol-2")
    _received(world, "pol-1")
    _received(world, "pol-2")
    _received(world, "pol-lost")                                 # its transaction never committed
    reconciler = world.reconciler()

    assert set(reconciler.rescan_on_startup()) == {"pol-1", "pol-2", "pol-lost"}
    assert world.ledger.get("pol-1").error_code == RECEIVED_AT_RESTART
    reconciler.run_due(NOW)

    for command_id, revision in (("pol-1", 1), ("pol-2", 2)):
        row = world.ledger.get(command_id)
        assert (row.state, row.error_code) == ("RESOLVED", None), command_id
        assert row.outcome == {"revision": revision, "reconciled": "committed_revision"}
    lost = world.ledger.get("pol-lost")
    assert (lost.state, lost.error_code) == ("REJECTED", POLICY_NOT_COMMITTED)
    assert lost.outcome == {"published": False, "reconciled": "never_committed"}
    assert world.ledger.unresolved_for_account(ACCOUNT) == []
    assert _policy(world.db).latest_published_revision() == 2    # the lost command published nothing


@pytest.mark.parametrize("policies", [None, BrokenPolicies()], ids=["no_port", "unreadable"])
def test_unreadable_or_missing_policy_evidence_keeps_the_publish_unknown(world, policies):
    _received(world, "pol-1")
    reconciler = world.reconciler(policies=policies)
    reconciler.rescan_on_startup()
    reconciler.run_due(NOW)
    reconciler.run_due(NOW + dt.timedelta(seconds=CRITICAL_AFTER_SECONDS))
    assert world.ledger.get("pol-1").state == "OUTCOME_UNKNOWN"
    assert world.alerts.raised == ["pol-1"]


def test_a_revision_of_another_account_is_no_proof(world):
    _publish(_policy(world.db, OTHER_ACCOUNT), LOOSE, "pol-x", account=OTHER_ACCOUNT)
    _received(world, "pol-x", state="OUTCOME_UNKNOWN")

    assert world.reconciler().reconcile_once("pol-x", NOW).resolved is True

    row = world.ledger.get("pol-x")
    assert (row.state, row.error_code) == ("REJECTED", POLICY_NOT_COMMITTED)


def test_the_committed_receipt_names_only_what_the_revision_row_proves(world):
    _publish(_policy(world.db), LOOSE, "pol-1")
    outcome = _policy(world.db).committed_outcome("pol-1")
    assert outcome == {"revision": 1, "reconciled": "committed_revision"}
    assert "applied_now" not in outcome and "queued" not in outcome
    assert _policy(world.db).committed_outcome("never-published") is None


def test_no_operator_settles_a_policy_publish_by_hand(world):
    assert "publish_ai_risk_policy" not in OPERATOR_SETTLEABLE_ACTIONS
    _received(world, "pol-1", state="OUTCOME_UNKNOWN")
    with pytest.raises(OperatorSettleRefused) as refused:
        world.reconciler().settle_by_operator("pol-1", resolved=False, reason="x", principal="cli",
                                              settle_command_id="settle-1", account_id=ACCOUNT)
    assert refused.value.code == "SETTLE_ACTION_FORBIDDEN"
