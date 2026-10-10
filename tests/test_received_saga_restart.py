"""Issue #130: a saga command an earlier process left RECEIVED must not hold reconciliation_safe() false for ever.

A saga that writes nothing while RECEIVED never started, so the restart rejects it. A saga that commits work while
RECEIVED (the risk-policy revision, the liquidation claim) is parked and settled by its evidence or the operator.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import _settle_unknown_command_action
from trader.trading.command_coordinator import (
    CRITICAL_AFTER_SECONDS, LIQUIDATION_NOT_STARTED, OPERATOR_SETTLED, RECEIVED_AT_RESTART, SAGA_ACTIONS,
    SAGA_ACTIONS_COMMITTING_WHILE_RECEIVED, SAGA_ACTIONS_IDLE_WHILE_RECEIVED, SETTLE_ACTION, CommandAudit,
    CommandLedger, CommandReceipt, CommandRequest, OutcomeReconciler, TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.liquidation_service import CloseResolution

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
BEFORE = NOW - dt.timedelta(minutes=1)
ACCOUNT = "DU111111"


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


class NoNonces:
    def consume_in_tx(self, conn, nonce, request):
        return True


class Closes:
    """The liquidation store's view: the root a command joined, and that root's decided outcome."""

    def __init__(self, roots=None, resolutions=None, failures=0):
        self.roots = roots or {}
        self.resolutions = resolutions or {}
        self.failures = failures

    def root_for(self, command_id):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("liquidation store unreadable")
        return self.roots.get(command_id)

    def close_resolution(self, command_id):
        return self.resolutions.get(command_id)


@pytest.fixture
def world(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    ledger = CommandLedger(journal)
    alerts = Alerts()

    def reconciler(closes=None):
        """A trader process built now: its start snapshot is the RECEIVED rows already written."""
        return OutcomeReconciler(journal=journal, ledger=ledger, orders=NoOrders(), strategy=NoStrategy(),
                                 alerts=alerts, now=lambda: NOW, closes=closes)

    return SimpleNamespace(journal=journal, ledger=ledger, alerts=alerts, reconciler=reconciler)


def _received(world, command_id, action, *, error_code=None, state="RECEIVED"):
    world.ledger.insert_for_test(command_id, state=state, updated_at=BEFORE, created_at=BEFORE,
                                 account_id=ACCOUNT, action=action, target_type="account", target_id=ACCOUNT,
                                 error_code=error_code)


def _state(world, command_id):
    row = world.ledger.get(command_id)
    return row.state, row.error_code


def test_every_saga_action_has_exactly_one_restart_rule():
    assert SAGA_ACTIONS_IDLE_WHILE_RECEIVED | SAGA_ACTIONS_COMMITTING_WHILE_RECEIVED == SAGA_ACTIONS
    assert not SAGA_ACTIONS_IDLE_WHILE_RECEIVED & SAGA_ACTIONS_COMMITTING_WHILE_RECEIVED


@pytest.mark.parametrize("action", sorted(SAGA_ACTIONS_IDLE_WHILE_RECEIVED))
def test_a_saga_left_received_by_a_crash_is_rejected_as_never_started(world, action):
    _received(world, "cmd-old", action)
    reconciler = world.reconciler()
    _received(world, "cmd-live", action)                      # written by this process after its start

    reconciler.rescan_on_startup()

    assert _state(world, "cmd-old") == ("REJECTED", "CRASH_ORPHANED")
    assert _state(world, "cmd-live") == ("RECEIVED", None)
    assert [row.command_id for row in world.ledger.unresolved_for_account(ACCOUNT)] == ["cmd-live"]


class ProcessKilled(BaseException):
    """The process dies inside the handler: no except clause of the coordinator runs."""


def test_a_saga_killed_before_it_moved_its_row_is_rejected_by_the_next_process(world):
    def killed(cmd: CommandRequest) -> CommandReceipt:
        raise ProcessKilled()

    coordinator = TradingCommandCoordinator(journal=world.journal, ledger=world.ledger,
                                            audit=CommandAudit(world.journal), nonces=NoNonces(), now=lambda: BEFORE)
    coordinator.register_action("cancel_order", killed, requires_preflight=False, saga=True)
    with pytest.raises(ProcessKilled):
        coordinator.execute(CommandRequest(command_id="cancel-1", action="cancel_order", account_id=ACCOUNT,
                                           target_type="order", target_id="o-1", expected_version=None,
                                           body={"order_entity_id": "o-1"}, source="dashboard"))
    assert _state(world, "cancel-1") == ("RECEIVED", None)

    world.reconciler().rescan_on_startup()

    assert _state(world, "cancel-1") == ("REJECTED", "CRASH_ORPHANED")
    assert world.ledger.unresolved_for_account(ACCOUNT) == []


def _settle(world, reconciler, target, outcome):
    coordinator = TradingCommandCoordinator(journal=world.journal, ledger=world.ledger,
                                            audit=CommandAudit(world.journal), nonces=NoNonces(), now=lambda: NOW)
    coordinator.register_action(SETTLE_ACTION, _settle_unknown_command_action(reconciler, ACCOUNT),
                                requires_preflight=False)
    return coordinator.execute(CommandRequest(
        command_id="settle-1", action=SETTLE_ACTION, account_id=ACCOUNT, target_type="command",
        target_id=target, expected_version=None,
        body={"target_command_id": target, "outcome": outcome, "reason": "policy revision checked"},
        source="cli", principal="cli"))


def test_a_policy_publish_left_received_is_parked_and_the_operator_settles_it(world):
    _received(world, "policy-1", "publish_ai_risk_policy")
    reconciler = world.reconciler()
    reconciler.rescan_on_startup()
    reconciler.run_due(NOW)
    assert _state(world, "policy-1") == ("OUTCOME_UNKNOWN", RECEIVED_AT_RESTART)   # its revision may be live

    receipt = _settle(world, reconciler, "policy-1", "rejected")

    assert receipt.state == "RESOLVED"
    assert _state(world, "policy-1") == ("REJECTED", OPERATOR_SETTLED)
    assert world.ledger.unresolved_for_account(ACCOUNT) == []


def test_a_liquidation_left_received_without_a_close_root_never_started(world):
    _received(world, "liq-1", "liquidate_account")
    reconciler = world.reconciler(Closes())
    reconciler.rescan_on_startup()
    assert _state(world, "liq-1") == ("OUTCOME_UNKNOWN", RECEIVED_AT_RESTART)

    reconciler.run_due(NOW)

    assert _state(world, "liq-1") == ("REJECTED", LIQUIDATION_NOT_STARTED)
    assert world.ledger.get("liq-1").outcome == {"reconciled": "never_started"}
    assert world.ledger.unresolved_for_account(ACCOUNT) == []


def test_a_liquidation_left_received_after_its_claim_resolves_from_its_close_root(world):
    _received(world, "liq-1", "liquidate_account")
    closes = Closes(roots={"liq-1": "liq-1"})
    reconciler = world.reconciler(closes)
    reconciler.rescan_on_startup()

    reconciler.run_due(NOW)
    assert _state(world, "liq-1") == ("OUTCOME_UNKNOWN", RECEIVED_AT_RESTART)       # the root is still open

    closes.resolutions["liq-1"] = CloseResolution(command_id="liq-1", root_id="liq-1", state="FLAT",
                                                  success=True, outcome={"close_root_id": "liq-1"})
    reconciler.run_due(NOW + dt.timedelta(seconds=CRITICAL_AFTER_SECONDS))
    assert _state(world, "liq-1") == ("RESOLVED", None)
    assert world.ledger.get("liq-1").outcome == {"close_root_id": "liq-1"}


@pytest.mark.parametrize("closes", [None, Closes(failures=10**6)], ids=["no-store", "unreadable"])
def test_a_liquidation_without_readable_close_evidence_stays_unknown(world, closes):
    _received(world, "liq-1", "liquidate_account")
    reconciler = world.reconciler(closes)
    reconciler.rescan_on_startup()

    reconciler.run_due(NOW)
    reconciler.run_due(NOW + dt.timedelta(seconds=CRITICAL_AFTER_SECONDS))

    assert _state(world, "liq-1") == ("OUTCOME_UNKNOWN", RECEIVED_AT_RESTART)
    assert world.alerts.raised == ["liq-1"]


def test_an_unreadable_close_root_is_asked_again(world):
    _received(world, "liq-1", "liquidate_account")
    reconciler = world.reconciler(Closes(failures=1))
    reconciler.rescan_on_startup()

    reconciler.run_due(NOW)
    assert _state(world, "liq-1") == ("OUTCOME_UNKNOWN", RECEIVED_AT_RESTART)
    reconciler.run_due(NOW + dt.timedelta(seconds=CRITICAL_AFTER_SECONDS))
    assert _state(world, "liq-1") == ("REJECTED", LIQUIDATION_NOT_STARTED)


def test_a_pending_liquidation_without_a_root_is_never_rejected_as_not_started(world):
    """Only a row parked at the restart proves no handler runs it; a pending liquidation keeps its claim."""
    _received(world, "liq-1", "liquidate_account", state="OUTCOME_UNKNOWN", error_code="LIQUIDATION_PENDING")
    reconciler = world.reconciler(Closes())
    reconciler.rescan_on_startup()

    reconciler.run_due(NOW)

    assert _state(world, "liq-1") == ("OUTCOME_UNKNOWN", "LIQUIDATION_PENDING")
