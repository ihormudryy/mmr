"""Issue #121: a crashed command no evidence resolves must not hold reconciliation_safe() false for ever.

Experiment commands settle from their own transition row; the operator settles the other single-step actions
by hand, through an audited cli-only command that never overwrites a row the reconciler settled first.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.automation.experiment_fixtures import armed_record
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.experiment_evidence import ExperimentCommandEvidence
from trader.automation.experiment_service import record_view
from trader.automation.experiments import ExperimentStore, apply_experiment_migration
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import _settle_unknown_command_action
from trader.trading.command_coordinator import (
    CRITICAL_AFTER_SECONDS, EXPERIMENT_COMMAND_NOT_COMMITTED, OPERATOR_SETTLEABLE_ACTIONS, OPERATOR_SETTLED,
    RECEIVED_AT_RESTART, SAGA_ACTIONS, SETTLE_ACTION, SETTLE_NOT_COMMITTED, CommandAudit, CommandLedger,
    CommandRequest, OutcomeReconciler, TradingCommandCoordinator, apply_command_ledger_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
BEFORE = NOW - dt.timedelta(minutes=1)
ACCOUNT = "DU111111"
CONFIG = AiPaperConfig()


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


@pytest.fixture
def world(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_experiment_migration(migrator)
    ledger = CommandLedger(journal)
    store = ExperimentStore(db, ACCOUNT, lambda: NOW)
    alerts = Alerts()

    def reconciler(experiments="evidence"):
        """A trader process built now: its start snapshot is the RECEIVED rows already written."""
        evidence = ExperimentCommandEvidence(store, CONFIG) if experiments == "evidence" else experiments
        return OutcomeReconciler(journal=journal, ledger=ledger, orders=NoOrders(), strategy=NoStrategy(),
                                 alerts=alerts, now=lambda: NOW, experiments=evidence)

    return SimpleNamespace(db=db, journal=journal, ledger=ledger, store=store, alerts=alerts,
                           reconciler=reconciler)


def _row(world, command_id, action, *, state="RECEIVED", target_type="experiment", target_id=ACCOUNT,
         account_id=ACCOUNT, outcome=None):
    world.ledger.insert_for_test(command_id, state=state, updated_at=BEFORE, created_at=BEFORE,
                                 account_id=account_id, action=action, target_type=target_type,
                                 target_id=target_id, outcome=outcome)


# -- 1. experiment commands parked at restart settle from their transition row --------------------------

def _run_experiment(world):
    """start-1 arms, pause-1 pauses, resume-1 resumes, stop-1 stops: each writes its transition row."""
    armed = world.store.insert_armed(armed_record(ACCOUNT, start_command_id="start-1"), principal="cli",
                                     reason="go")
    for command_id, expected, to in (("pause-1", "ARMED", "PAUSED"), ("resume-1", "PAUSED", "ARMED"),
                                      ("stop-1", "ARMED", "STOPPED")):
        world.store.transition(armed.experiment_id, expected=frozenset({expected}), to=to, principal="cli",
                               command_id=command_id, reason="r")
    return armed.experiment_id


COMMITTED = {"start-1": ("start_experiment", "ARMED", 1), "pause-1": ("pause_experiment", "PAUSED", 2),
             "resume-1": ("resume_experiment", "ARMED", 3), "stop-1": ("stop_experiment", "STOPPED", 4)}
NEVER_COMMITTED = {"start-2": "start_experiment", "pause-noop": "pause_experiment",
                   "resume-noop": "resume_experiment", "stop-2": "stop_experiment"}


def test_crashed_experiment_commands_settle_from_their_transition_row_and_unblock_reconciliation(world):
    experiment_id = _run_experiment(world)
    for command_id, (action, _state, _revision) in COMMITTED.items():
        _row(world, command_id, action)
    for command_id, action in NEVER_COMMITTED.items():
        _row(world, command_id, action)
    reconciler = world.reconciler()
    assert set(reconciler.rescan_on_startup()) == set(COMMITTED) | set(NEVER_COMMITTED)
    assert world.ledger.get("pause-1").error_code == RECEIVED_AT_RESTART
    reconciler.run_due(NOW)

    view_now = record_view(world.store.get(experiment_id), CONFIG)
    for command_id, (_action, state, revision) in COMMITTED.items():
        row = world.ledger.get(command_id)
        assert (row.state, row.error_code) == ("RESOLVED", None), command_id
        assert row.outcome == {**view_now, "reconciled": "committed_transition", "committed_state": state,
                               "committed_revision": revision}
    for command_id in NEVER_COMMITTED:
        row = world.ledger.get(command_id)
        assert (row.state, row.error_code) == ("REJECTED", EXPERIMENT_COMMAND_NOT_COMMITTED), command_id
        assert row.outcome == {"experiment_changed": False, "reconciled": "never_committed"}
    assert world.ledger.unresolved_for_account(ACCOUNT) == []


class BrokenEvidence:
    def committed_outcome(self, action, command_id):
        raise RuntimeError("journal unreadable")


@pytest.mark.parametrize("evidence", [None, BrokenEvidence()], ids=["no_port", "unreadable"])
def test_unreadable_or_missing_experiment_evidence_keeps_the_command_unknown(world, evidence):
    _row(world, "pause-1", "pause_experiment")
    reconciler = world.reconciler(experiments=evidence)
    reconciler.rescan_on_startup()
    reconciler.run_due(NOW)
    reconciler.run_due(NOW + dt.timedelta(seconds=CRITICAL_AFTER_SECONDS))
    assert world.ledger.get("pause-1").state == "OUTCOME_UNKNOWN"
    assert world.alerts.raised == ["pause-1"]


def test_a_transition_row_of_another_action_is_no_proof(world):
    _run_experiment(world)
    _row(world, "start-1", "pause_experiment", state="OUTCOME_UNKNOWN")      # start-1 wrote ARMED, not PAUSED
    assert world.reconciler().reconcile_once("start-1", NOW).resolved is False
    assert world.ledger.get("start-1").state == "OUTCOME_UNKNOWN"


def test_a_transition_of_another_account_is_no_proof(world):
    other = ExperimentStore(world.db, "DU999999", lambda: NOW)
    other.insert_armed(armed_record("DU999999", start_command_id="start-x"), principal="cli", reason="go")
    _row(world, "start-x", "start_experiment", state="OUTCOME_UNKNOWN")
    assert world.reconciler().reconcile_once("start-x", NOW).resolved is False


# -- 2. the operator settle command ----------------------------------------------------------------------

def _coordinator(world, reconciler):
    coordinator = TradingCommandCoordinator(journal=world.journal, ledger=world.ledger,
                                            audit=CommandAudit(world.journal), nonces=NoNonces(), now=lambda: NOW)
    coordinator.register_action(SETTLE_ACTION, _settle_unknown_command_action(reconciler, ACCOUNT),
                                requires_preflight=False)
    return coordinator


def _settle(coordinator, target, outcome="rejected", *, command_id="settle-1", principal="cli"):
    return coordinator.execute(CommandRequest(
        command_id=command_id, action=SETTLE_ACTION, account_id=ACCOUNT, target_type="command",
        target_id=target, expected_version=None,
        body={"target_command_id": target, "outcome": outcome, "reason": "checked the control row"},
        source=principal, principal=principal))


def _audited(world, command_id):
    return world.journal.connect().execute(
        "SELECT action, target_id FROM command_audit WHERE command_id = ?", [command_id]).fetchall()


@pytest.mark.parametrize("outcome,state,error_code", [("resolved", "RESOLVED", None),
                                                       ("rejected", "REJECTED", OPERATOR_SETTLED)])
def test_the_operator_settles_an_unknown_row_and_reconciliation_is_safe_again(world, outcome, state, error_code):
    _row(world, "pause-1", "pause_trading", target_type="account")
    reconciler = world.reconciler()
    reconciler.rescan_on_startup()
    reconciler.run_due(NOW)
    assert [row.command_id for row in world.ledger.unresolved_for_account(ACCOUNT)] == ["pause-1"]

    receipt = _settle(_coordinator(world, reconciler), "pause-1", outcome)

    target = world.ledger.get("pause-1")
    assert (target.state, target.error_code) == (state, error_code)
    assert target.outcome == {"settled_by": "cli", "reason": "checked the control row",
                              "settle_command_id": "settle-1"}
    assert (receipt.state, receipt.error_code) == ("RESOLVED", None)
    assert receipt.outcome == {"command_id": "pause-1", "action": "pause_trading", "state": state,
                               "error_code": error_code, "settled_by": "cli", "reason": "checked the control row"}
    assert _audited(world, "settle-1") == [(SETTLE_ACTION, "pause-1")]
    assert world.ledger.unresolved_for_account(ACCOUNT) == []
    reconciler.run_due(NOW + dt.timedelta(seconds=CRITICAL_AFTER_SECONDS))
    assert world.alerts.raised == []                                       # the settled row left the schedule


@pytest.mark.parametrize("action", ["approve_proposal", "submit_ai_paper_decision"])
def test_an_action_with_a_broker_side_effect_is_never_settled_by_hand(world, action):
    _row(world, "cmd-1", action, state="OUTCOME_UNKNOWN", target_type="proposal")
    receipt = _settle(_coordinator(world, world.reconciler()), "cmd-1", "resolved")
    assert (receipt.state, receipt.error_code) == ("REJECTED", "SETTLE_ACTION_FORBIDDEN")
    assert world.ledger.get("cmd-1").state == "OUTCOME_UNKNOWN"
    assert _audited(world, "settle-1") == [(SETTLE_ACTION, "cmd-1")]


def test_a_row_that_is_not_unknown_is_refused(world):
    _row(world, "reject-1", "reject_proposal", state="RESOLVED", target_type="proposal")
    receipt = _settle(_coordinator(world, world.reconciler()), "reject-1")
    assert (receipt.state, receipt.error_code) == ("REJECTED", "SETTLE_NOT_UNKNOWN")
    assert world.ledger.get("reject-1").state == "RESOLVED"


@pytest.mark.parametrize("target", ["missing-1", "other-account-1"])
def test_an_unknown_or_foreign_command_is_refused(world, target):
    _row(world, "other-account-1", "pause_trading", state="OUTCOME_UNKNOWN", account_id="DU999999")
    receipt = _settle(_coordinator(world, world.reconciler()), target)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "SETTLE_UNKNOWN_COMMAND")
    assert world.ledger.get("other-account-1").state == "OUTCOME_UNKNOWN"


def test_only_the_cli_principal_settles_even_behind_the_rpc_allow_list(world):
    _row(world, "pause-1", "pause_trading", state="OUTCOME_UNKNOWN", target_type="account")
    receipt = _settle(_coordinator(world, world.reconciler()), "pause-1", principal="dashboard")
    assert (receipt.state, receipt.error_code) == ("REJECTED", "PRINCIPAL_FORBIDDEN")
    assert world.ledger.get("pause-1").state == "OUTCOME_UNKNOWN"


def test_a_row_the_reconciler_settled_first_is_never_overwritten(world, monkeypatch):
    _row(world, "pause-1", "pause_trading", state="OUTCOME_UNKNOWN", target_type="account")
    reconciler = world.reconciler()
    stale = world.ledger.get("pause-1")
    reconciler._resolve_command_only(stale, {"account_id": ACCOUNT, "new_exposure_paused": True}, NOW)
    real_get = world.ledger.get
    monkeypatch.setattr(world.ledger, "get", lambda cid: stale if cid == "pause-1" else real_get(cid))

    receipt = _settle(_coordinator(world, reconciler), "pause-1", "rejected")

    assert (receipt.state, receipt.error_code) == ("REJECTED", "SETTLE_NOT_UNKNOWN")
    row = real_get("pause-1")
    assert (row.state, row.outcome) == ("RESOLVED", {"account_id": ACCOUNT, "new_exposure_paused": True})


def test_a_crashed_settle_settles_from_its_target_row(world):
    _row(world, "pause-1", "pause_trading", state="REJECTED", target_type="account",
         outcome={"settled_by": "cli", "reason": "r", "settle_command_id": "settle-done"})
    _row(world, "settle-done", SETTLE_ACTION, target_type="command", target_id="pause-1")
    _row(world, "pause-2", "pause_trading", state="OUTCOME_UNKNOWN", target_type="account")
    _row(world, "settle-lost", SETTLE_ACTION, target_type="command", target_id="pause-2")
    reconciler = world.reconciler()
    reconciler.rescan_on_startup()
    reconciler.run_due(NOW)
    done, lost = world.ledger.get("settle-done"), world.ledger.get("settle-lost")
    assert (done.state, done.outcome["command_id"], done.outcome["settled_by"]) == ("RESOLVED", "pause-1", "cli")
    assert (lost.state, lost.error_code) == ("REJECTED", SETTLE_NOT_COMMITTED)
    assert [row.command_id for row in world.ledger.unresolved_for_account(ACCOUNT)] == ["pause-2"]


NEVER_SETTLED_BY_HAND = {
    "approve_proposal", "create_proposal", "cancel_order", "cancel_orders", "execute_automated_intent",
    "submit_ai_paper_decision", "liquidate_account", "acceptance_shrink_probe", "register_ai_deployment",
    "withdraw_ai_deployment", "start_experiment", "pause_experiment", "resume_experiment", "stop_experiment",
    "enable_strategy", "disable_strategy", "update_strategy_params", SETTLE_ACTION,
}


def test_the_settle_allow_list_holds_no_broker_or_evidence_settled_action():
    assert not OPERATOR_SETTLEABLE_ACTIONS & NEVER_SETTLED_BY_HAND
    assert OPERATOR_SETTLEABLE_ACTIONS & SAGA_ACTIONS == {"publish_ai_risk_policy"}   # journal only, no order
