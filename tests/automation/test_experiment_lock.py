"""SP1 Plan 4 Task 4: the lock in both directions and BOTH_MODES_ARMED at startup (K17)."""
from __future__ import annotations

import datetime as dt
import threading
from types import SimpleNamespace

import pytest

from tests.automation.experiment_fixtures import armed_record
from tests.automation.test_automated_command_boundary import (
    ARTIFACT_DIGEST, _build_stack, _execute_buy, _execute_sell, _FakeBrokerSnapshot, _FakeCloseLiquidation,
)
from tests.automation.test_paper_activation import _service, _with_eligible_bundle
from trader.automation.experiment_service import ArmingLock, ExperimentLock
from trader.automation.experiments import ExperimentStore, apply_experiment_migration
from trader.automation.paper_activation import PaperAutomationActivationError
from trader.automation.paper_hot_arm import ProductionPaperHotArmPorts, RecordingHotArmPorts
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
ACCOUNT = "DU1"


def _store(tmp_path, state):
    db = DuckDBConnection.get_instance(str(tmp_path / "experiments.duckdb"))
    apply_experiment_migration(SchemaMigrator(db))
    store = ExperimentStore(db, ACCOUNT, lambda: NOW)
    if state is None:
        return store
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    path = {"ARMED": [], "PAUSED": ["PAUSED"], "KILLED": ["KILLED"], "STOPPED": ["STOPPED"]}[state]
    for to in path:
        rec = store.transition(rec.experiment_id, expected=frozenset({rec.state}), to=to, principal="cli",
                               command_id=None, reason="setup")
    return store


@pytest.fixture
def activation_with_experiment(tmp_path):
    def build(state, mode):
        service = _service(tmp_path)
        service._experiment_lock = ExperimentLock(_store(tmp_path, state), ArmingLock())
        if mode == "hot_arm":
            service._hot_arm = RecordingHotArmPorts()
        return service
    return build


@pytest.mark.parametrize("state", ["ARMED", "PAUSED", "KILLED"])
@pytest.mark.parametrize("mode", ["hot_arm", "restart_required"])
def test_activate_is_refused_while_an_experiment_is_not_stopped(activation_with_experiment, state, mode, tmp_path):
    service = activation_with_experiment(state, mode)
    before = (tmp_path / "config" / "trader.yaml").read_text()
    with _with_eligible_bundle(tmp_path), pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="x")
    assert exc.value.code == "EXPERIMENT_ACTIVE"
    assert service.status().lifecycle == "disabled"            # nothing written to trader.yaml
    assert (tmp_path / "config" / "trader.yaml").read_text() == before
    if mode == "hot_arm":
        assert service._hot_arm.calls == []


@pytest.mark.parametrize("state", ["STOPPED", None])
def test_activate_allowed_after_stop(activation_with_experiment, tmp_path, state):   # STOPPED releases the lock
    service = activation_with_experiment(state, "restart_required")
    with _with_eligible_bundle(tmp_path):
        assert service.activate(strategy_name="orb_gld", reason="x")["lifecycle"] == "restart_required"


def test_unreadable_experiment_state_refuses_activation(activation_with_experiment, tmp_path):
    service = activation_with_experiment(None, "restart_required")

    class Broken:
        def blocking_state(self):
            raise RuntimeError("db gone")

        def hold(self, timeout=10.0):
            return ArmingLock().hold(timeout)
    service._experiment_lock = Broken()
    with _with_eligible_bundle(tmp_path), pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="x")
    assert exc.value.code == "EXPERIMENT_STATE_UNREADABLE"


def test_activate_waits_for_the_arming_lock(activation_with_experiment, tmp_path):
    service = activation_with_experiment(None, "restart_required")
    lock = ArmingLock()
    service._experiment_lock = ExperimentLock(service._experiment_lock._store, lock)
    with lock.hold():
        original = lock.hold
        lock.hold = lambda timeout=10.0: original(0.01)
        with _with_eligible_bundle(tmp_path), pytest.raises(PaperAutomationActivationError) as exc:
            service.activate(strategy_name="orb_gld", reason="x")
    assert exc.value.code == "ARMING_BUSY"


@pytest.mark.parametrize("state", ["ARMED", "PAUSED", "KILLED"])
def test_hot_arm_trader_commit_refuses_too(tmp_path, state):
    built = []
    ports = ProductionPaperHotArmPorts(
        trader=SimpleNamespace(), stack=None, account_id=ACCOUNT, account_mode="paper", now=lambda: NOW,
        build_intent_service=lambda trader: built.append(trader),
        experiment_lock=ExperimentLock(_store(tmp_path, state), ArmingLock()))
    with pytest.raises(PaperAutomationActivationError) as exc:
        ports.trader_commit(strategy_name="orb_gld", artifact_id="a" * 64, artifact_bundle_path="/x",
                            public_key_ring_path="/k")
    assert exc.value.code == "EXPERIMENT_ACTIVE"
    assert built == []


def test_activate_and_start_race_one_wins(tmp_path):                 # shared ArmingLock
    from tests.automation.test_experiment_service import World, cmd
    from trader.trading.command_stack import _one_strategy_armed

    experiments = World(tmp_path / "exp")
    activation = _service(tmp_path)
    lock = experiments.service._lock
    activation._experiment_lock = ExperimentLock(experiments.store, lock)
    experiments.ports.old_path_armed = lambda: _one_strategy_armed(activation)
    results = {}
    barrier = threading.Barrier(2)

    def activate():
        barrier.wait()
        try:
            activation.activate(strategy_name="orb_gld", reason="x")
            results["activate"] = "ok"
        except PaperAutomationActivationError as exc:
            results["activate"] = exc.code

    def start():
        barrier.wait()
        from trader.trading.command_coordinator import CommandValidationError
        try:
            experiments.service.start(cmd("start_experiment", reason="go"))
            results["start"] = "ok"
        except CommandValidationError as exc:
            results["start"] = exc.code

    with _with_eligible_bundle(tmp_path):
        threads = [threading.Thread(target=activate), threading.Thread(target=start)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert sorted(results.values()) in (["EXPERIMENT_ACTIVE", "ok"], ["ONE_STRATEGY_ARMED", "ok"])


# -- K17: the old path's BUY refusal ------------------------------------------

def test_both_modes_armed_refuses_an_old_path_buy_but_not_a_sell(tmp_path):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    stack.service._entry_block = lambda: "BOTH_MODES_ARMED"
    buy = _execute_buy(stack, tmp_path)
    assert (buy.state, buy.error_code) == ("REJECTED", "BOTH_MODES_ARMED")
    assert stack.dispatch.calls == []
    sell, _intent = _execute_sell(stack, tmp_path, None)
    assert (sell.state, sell.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")


def test_a_raising_entry_block_refuses_the_buy(tmp_path):
    stack = _build_stack(tmp_path)
    stack.service._entry_block = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    buy = _execute_buy(stack, tmp_path)
    assert (buy.state, buy.error_code) == ("REJECTED", "ENTRY_BLOCK_UNAVAILABLE")
