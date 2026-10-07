"""SP1 Plan 4 Task 6: Plan 3's ai_paper path reads real experiments (entry block, dispatch gate)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import ACCOUNT, CONID, NOW
from tests.automation.ai_paper_world import World
from tests.automation.experiment_fixtures import armed_record
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.ai_paper_decision import AI_PAPER_ACTION
from trader.automation.ai_paper_experiment import ExperimentStateReader, ExperimentView, experiment_entry_refusal
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.experiments import ExperimentStore, apply_experiment_migration
from trader.automation.kill_monitor import KillLineMonitor
from trader.automation.session_controller import SessionController, apply_session_controller_migration
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_stack import _experiment_gate
from trader.trading.dispatch_guard import DispatchGuardError

CONFIG = AiPaperConfig(enabled=True, experiment_kill_drawdown_pct=20.0)


class _NoLiquidation:
    def receipt_for(self, root_id):
        return None


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.block_flatten_start = False

    def flatten_account_now(self, cause, deadline):
        if self.block_flatten_start:
            raise RuntimeError("liquidation worker busy")
        self.calls.append(cause)
        return cause


class ExperimentWorld(World):
    """Plan 3's world with the fake experiment port replaced by a real store, monitor and reader."""

    def __init__(self, tmp_path, *, real_liquidation=False, mode_conflict=None, recover=True):
        super().__init__(tmp_path, real_liquidation=real_liquidation)
        migrator = SchemaMigrator(self.db)
        apply_experiment_migration(migrator)
        apply_session_controller_migration(migrator)
        self.store = ExperimentStore(self.db, ACCOUNT, self.clock)
        self.store.insert_armed(armed_record(ACCOUNT, start_net_liquidation=1_000_000.0,
                                             peak_net_liquidation=1_000_000.0,
                                             kill_anchor_net_liquidation=1_000_000.0),
                                principal="cli", reason="go")
        if real_liquidation:
            self.session = SessionController(
                journal=self.journal, db=self.db, calendar=XNYSCalendarPolicy(), broker=self.broker,
                cancel=SimpleNamespace(cancel_working_entries=lambda **k: []), liquidation=self.liquidation,
                breaker=SimpleNamespace(record=lambda signal: None),
                time_exit=SimpleNamespace(request_exit=lambda **k: None), account_id=ACCOUNT, now=self.clock)
            monitor_liquidation = self.liquidation
        else:
            self.session = _FakeSession()
            monitor_liquidation = _NoLiquidation()
        self.monitor = KillLineMonitor(
            store=self.store, broker=self.broker, session=self.session, liquidation=monitor_liquidation,
            config=CONFIG, account_id=ACCOUNT, now=self.clock, reconciliation_safe=lambda: True)
        if recover:
            self.monitor.recover()
            self.monitor.tick()
        self.reader = ExperimentStateReader(self.store, self.monitor, mode_conflict=lambda: mode_conflict)
        self.service._experiments = self.reader
        self.guard._experiment_gate = _experiment_gate(self.reader, ACCOUNT)

    def kill_line_hit(self):
        self.broker.set(net_liquidation=790_000.0)
        self.monitor.tick()

    def close_body(self, **changes):
        body = self.body(action="CLOSE", side="SELL", deployment_digest=None, policy_revision=None,
                         stop_price=None, quantity=None)
        body.update(changes)
        return body


@pytest.fixture
def world(tmp_path):
    return ExperimentWorld(tmp_path)


@pytest.fixture
def world_with_position(tmp_path):
    w = ExperimentWorld(tmp_path, real_liquidation=True)
    w.owned(CONID, 300.0)
    return w


def test_an_armed_experiment_with_a_fresh_kill_line_admits_the_entry(world):
    assert world.reader.current(ACCOUNT) == ExperimentView(world.store.active().experiment_id, "ARMED", None)
    assert world.submit().state == "SUBMITTED"


def test_enter_is_refused_from_the_moment_killed_is_stored(world):
    world.kill_line_hit()                                        # monitor writes KILLED, flatten pending
    assert world.store.active().state == "KILLED"
    assert world.submit().error_code == "EXPERIMENT_NOT_ARMED"


def test_enter_refused_while_the_kill_line_is_unknown(world):
    world.broker.fail = True
    world.monitor.tick()
    world.clock.advance(seconds=31)
    assert world.submit().error_code == "KILL_LINE_UNKNOWN"
    assert world.dispatch.plans == []


def test_enter_refused_until_the_monitor_recovered(tmp_path):
    w = ExperimentWorld(tmp_path, recover=False)
    assert w.submit().error_code == "EXPERIMENT_MONITOR_NOT_READY"


def test_enter_refused_with_both_modes_armed_but_a_close_still_works(tmp_path):          # K17
    w = ExperimentWorld(tmp_path, real_liquidation=True)
    w.owned(CONID, 300.0)                                        # bought before the second mode was armed
    w.service._experiments = ExperimentStateReader(w.store, w.monitor, mode_conflict=lambda: "BOTH_MODES_ARMED")
    assert w.submit().error_code == "BOTH_MODES_ARMED"
    assert w.submit(w.close_body(decision_id="dec-00000002")).error_code == "CLOSE_PENDING"


def test_close_still_works_while_the_kill_line_is_unknown(world_with_position):
    world_with_position.broker.fail = True                     # the monitor's capture fails ...
    world_with_position.monitor.tick()
    world_with_position.broker.fail = False                    # ... a reduction uses its own capture
    world_with_position.clock.advance(seconds=31)
    assert world_with_position.reader.current(ACCOUNT).entry_block == "KILL_LINE_UNKNOWN"
    receipt = world_with_position.submit(world_with_position.close_body())
    assert receipt.error_code == "CLOSE_PENDING"


def test_entry_validated_before_the_kill_is_refused_at_dispatch(world):         # K20, spec edge
    world.on_before_guard(world.kill_line_hit)
    receipt = world.submit()
    assert receipt.error_code == "EXPERIMENT_NOT_ARMED" and world.dispatch.plans == []
    assert world.store.active().state == "KILLED"


def test_old_path_dispatch_ignores_the_experiment_gate(world):
    gate = _experiment_gate(world.reader, ACCOUNT)
    world.kill_line_hit()
    assert gate(SimpleNamespace(action="execute_automated_intent")) is None
    assert gate(SimpleNamespace(action="approve_proposal")) is None
    assert gate(SimpleNamespace(action=AI_PAPER_ACTION)) == "EXPERIMENT_NOT_ARMED"


def test_raising_gate_refuses(world):
    def broken(account_id):
        raise RuntimeError("journal unreadable")
    world.reader.current = broken
    with pytest.raises(DispatchGuardError) as exc:
        world.guard.revalidate(None, SimpleNamespace(account_id=ACCOUNT, action=AI_PAPER_ACTION), NOW)
    assert (exc.value.code, exc.value.retryable) == ("EXPERIMENT_STATE_UNAVAILABLE", True)


@pytest.mark.parametrize("state,code", [(None, "NO_EXPERIMENT"), ("PAUSED", "EXPERIMENT_NOT_ARMED"),
                                        ("STOPPED", "EXPERIMENT_NOT_ARMED")])
def test_entry_refusal_codes(state, code):
    view = None if state is None else ExperimentView("exp-1", state)
    assert experiment_entry_refusal(SimpleNamespace(current=lambda account: view), ACCOUNT) == code


def test_reader_is_scoped_to_its_account(world):
    assert world.reader.current("DU999") is None


def test_close_while_killed_joins_the_kill_flatten(world_with_position):       # Plan 3 R15 with the real kill
    world_with_position.kill_line_hit()
    kill_root = world_with_position.store.active().kill_flatten_root
    assert kill_root is not None
    receipt = world_with_position.submit(world_with_position.close_body(action="PARTIAL_CLOSE", quantity=100))
    assert receipt.outcome["close_root_id"] == kill_root
    assert world_with_position.liquidation_runs() == {kill_root}


def test_close_after_killed_but_before_the_flatten_is_retryable(tmp_path):
    w = ExperimentWorld(tmp_path)
    w.session.block_flatten_start = True                       # KILLED stored, no owner yet
    w.kill_line_hit()
    assert w.store.active().state == "KILLED" and w.store.active().kill_flatten_root is None
    w.held(CONID, 300.0)
    receipt = w.submit(w.close_body())
    assert (receipt.error_code, receipt.retryable) == ("KILL_FLATTEN_PENDING", True)


def test_decisions_refused_after_stop(world):
    world.kill_line_hit()
    rec = world.store.active()
    world.store.transition(rec.experiment_id, expected=frozenset({"KILLED"}), to="STOPPED", principal="cli",
                           command_id="s", reason="done")
    assert world.submit().error_code == "EXPERIMENT_STOPPED"
    assert world.submit(world.close_body(decision_id="dec-00000002")).error_code == "EXPERIMENT_STOPPED"


def test_plan3_views_without_entry_block_still_build():
    assert ExperimentView("exp1", "ARMED").entry_block is None
    with pytest.raises(ValueError):
        ExperimentView("exp1", "ARMED", entry_block="")


def _resume_service(world):
    from trader.automation.experiment_service import ArmingLock, ArmingPorts, ExperimentService
    ports = ArmingPorts(
        broker=world.broker, account_cash=lambda: {"base_currency": "USD", "currencies": {}},
        resume_ready=lambda: True, reconciliation_safe=lambda exclude: True, breaker_clear=lambda: True,
        exit_owners=SimpleNamespace(account_owner=lambda account_id: None), liquidation_roots=lambda: [],
        old_path_armed=lambda: None, ai_paper_built=lambda: True, kill_gate=world.monitor)
    return ExperimentService(store=world.store, ports=ports, lock=ArmingLock(), config=CONFIG,
                             account_id=ACCOUNT, account_mode="paper", now=world.clock)


def test_outage_resume_on_a_breached_generation_kills_and_admits_no_entry(world):   # review #32
    from trader.trading.command_coordinator import CommandRequest, CommandValidationError
    world.broker.fail = True                                     # outage: the monitor pauses the experiment
    world.monitor.tick()
    world.clock.advance(seconds=CONFIG.broker_outage_pause_seconds + 1)
    world.monitor.tick()
    record = world.store.active()
    assert (record.state, record.pause_cause) == ("PAUSED", "BROKER_DATA_OUTAGE")
    world.broker.fail = False                                    # fresh generation, 21% below the 1m start
    world.broker.set(net_liquidation=790_000.0, generation=world.broker.last + 1)
    resume = CommandRequest(
        command_id="resume-1", action="resume_experiment", account_id=ACCOUNT, target_type="experiment",
        target_id=ACCOUNT, expected_version=None, body={"experiment_id": record.experiment_id, "reason": "back"},
        source="cli", principal="cli")
    with pytest.raises(CommandValidationError, match="EXPERIMENT_KILLED"):
        _resume_service(world).resume(resume)
    assert world.store.active().state == "KILLED"
    assert world.session.calls == [world.store.active().kill_root_id]
    assert world.submit().error_code == "EXPERIMENT_NOT_ARMED"
    assert world.dispatch.plans == []
