"""SP1 Plan 4: experiments in the composed command stack (K15, K17, Task 6 wiring)."""
from __future__ import annotations

import logging

import pytest

from tests.automation.experiment_fixtures import armed_record
from tests.test_command_stack import NOW, _automation_key_ring, _policy, _trader
from trader.automation.experiments import ExperimentStore, apply_experiment_migration
from trader.data.schema_migrations import SchemaMigrator

ACCOUNT = "DU111111"


@pytest.fixture(autouse=True)
def _isolated_trader_yaml(tmp_path, monkeypatch):
    """status() reads trader.yaml: never the developer's file."""
    path = tmp_path / "trader.yaml"
    path.write_text("{}\n")
    monkeypatch.setenv("TRADER_CONFIG", str(path))


def _with_experiment(trader, state):
    apply_experiment_migration(SchemaMigrator(trader.journal_db))
    store = ExperimentStore(trader.journal_db, ACCOUNT, lambda: NOW)
    rec = store.insert_armed(armed_record(ACCOUNT), principal="cli", reason="go")
    for to in {"ARMED": [], "PAUSED": ["PAUSED"], "KILLED": ["KILLED"], "STOPPED": ["STOPPED"]}[state]:
        rec = store.transition(rec.experiment_id, expected=frozenset({rec.state}), to=to, principal="cli",
                               command_id=None, reason="setup")
    return store


def _arm_old_path(trader, tmp_path):
    trader.automation_enabled = True
    trader.automation_live_enabled = False
    trader.automation_public_key_ring_path = _automation_key_ring(tmp_path)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-test-1"
    (tmp_path / "artifacts").mkdir()


def paper_trader_with_old_path_configured(tmp_path, *, experiment, old_path):
    trader = _trader(tmp_path)
    if experiment is not None:
        _with_experiment(trader, experiment)
    if old_path == "armed":
        _arm_old_path(trader, tmp_path)
    return trader


def _conflict_events(trader):
    return trader.journal_db.execute(
        "SELECT count(*) FROM domain_event_journal WHERE event_type = 'automation.mode_conflict'", fetch="one")[0]


def test_both_modes_configured_but_not_both_armed_starts_normally(tmp_path):              # K17
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment="ARMED", old_path="disabled")
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack.mode_conflict is None and _conflict_events(trader) == 0


def test_old_path_armed_without_an_experiment_starts_normally(tmp_path):
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment=None, old_path="armed")
    assert build_command_stack(trader, _policy(), now=lambda: NOW).mode_conflict is None


def test_a_stopped_experiment_does_not_conflict(tmp_path):
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment="STOPPED", old_path="armed")
    assert build_command_stack(trader, _policy(), now=lambda: NOW).mode_conflict is None


@pytest.mark.parametrize("state", ["ARMED", "PAUSED", "KILLED"])
def test_a_paused_or_killed_experiment_counts_as_armed_for_the_conflict(tmp_path, state, caplog):   # K17
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment=state, old_path="armed")
    with caplog.at_level(logging.ERROR):
        stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack is not None and stack.mode_conflict == "BOTH_MODES_ARMED"
    assert any(r.levelname == "ERROR" and "BOTH_MODES_ARMED" in r.getMessage() for r in caplog.records)
    assert _conflict_events(trader) == 1                                         # durable journal incident
    assert stack.automated_intent_service._entry_refusal() == "BOTH_MODES_ARMED"


def test_both_modes_armed_never_disarms_either_mode(tmp_path):                             # K17
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment="ARMED", old_path="armed")
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack.paper_automation_service.status().lifecycle != "disabled"
    assert ExperimentStore(trader.journal_db, ACCOUNT, lambda: NOW).active().state == "ARMED"
    assert stack.automated_intent_service is not None


def test_the_operator_clears_the_conflict_by_deactivating_the_old_path(tmp_path):        # K17
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment="ARMED", old_path="armed")
    assert build_command_stack(trader, _policy(), now=lambda: NOW).mode_conflict == "BOTH_MODES_ARMED"
    trader.automation_enabled = False                       # the operator's deliberate act, then a restart
    assert build_command_stack(trader, _policy(), now=lambda: NOW).mode_conflict is None


def test_live_stack_has_no_experiment_parts(tmp_path):
    from trader.trading.command_policy import CommandAuthorityPolicy
    from trader.trading.command_stack import build_command_stack
    trader = _trader(tmp_path)
    trader.ib_account = "U111111"
    trader.paper_trading = False
    stack = build_command_stack(trader, CommandAuthorityPolicy(
        enabled=True, live_enabled=True, live_account_id="U111111", max_order_notional=25_000.0),
        now=lambda: NOW)
    assert stack.experiments is None and stack.mode_conflict is None
    assert stack.paper_automation_service._experiment_lock is None


def test_migration_70_is_applied(tmp_path):
    from trader.trading.command_stack import build_command_stack
    trader = _trader(tmp_path)
    build_command_stack(trader, _policy(), now=lambda: NOW)
    assert trader.journal_db.execute("SELECT version FROM schema_migrations WHERE version = 70", fetch="one")


def test_activation_service_gets_the_experiment_lock(tmp_path):
    from trader.trading.command_stack import build_command_stack
    trader = paper_trader_with_old_path_configured(tmp_path, experiment="ARMED", old_path="disabled")
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack.paper_automation_service._experiment_lock.blocking_state() == "ARMED"
    assert stack.paper_hot_arm._experiment_lock is stack.paper_automation_service._experiment_lock
