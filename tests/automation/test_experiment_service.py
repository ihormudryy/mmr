"""SP1 Plan 4 Task 3: experiment start, pause, resume and stop with the arming checks."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.automation.experiment_fixtures import ACCOUNT, NOW
from tests.rpc_identity_fixtures import make_identities
from trader.automation.ai_paper_config import load_ai_paper_config
from trader.automation.experiment_service import (
    ArmingLock, ArmingPorts, ExperimentService, StartFx, ai_supervisor_identity_problem, start_fx_from_cash,
)
from trader.automation.experiments import (
    ExperimentRefused, ExperimentStore, apply_experiment_migration, experiment_id_for,
)
from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot, BrokerRiskSnapshotError
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import sha256_digest
from trader.trading.command_coordinator import CommandRequest, CommandValidationError

CONFIG = load_ai_paper_config({"enabled": True, "experiment_kill_drawdown_pct": 20.0}, trading_mode="paper")


class _Crash(BaseException):
    """Simulated process death."""


def position(quantity, conid=1):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        quantity=quantity, average_cost=None, market_price=None, market_value=None, unrealized_pnl=None,
        realized_pnl=None, daily_pnl=None, deleted=False, revision=1, source_timestamp=NOW)


def working(entity="ord-1", external=False):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=1, symbol="AAPL",
        order_group_id=None if external else "og-x", leg=None if external else "entry", is_external=external,
        action="BUY", order_type="LMT", total_quantity=10.0, filled_quantity=0.0, avg_fill_price=None,
        limit_price=100.0, stop_price=None, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW)


class Broker:
    def __init__(self):
        self.fields = dict(generation_id=7, net_liquidation=100_000.0, positions=(), working=(),
                           account=ACCOUNT, mode="paper")
        self.calls = 0
        self.fail = None
        self.after_first = None

    def set(self, **changes):
        self.fields.update(changes)

    def capture(self, account_id):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        f = self.fields
        snap = BrokerRiskSnapshot(
            generation_id=f["generation_id"], source_cursor=f["generation_id"], promoted_at=NOW,
            account_id=f["account"], account_mode=f["mode"], net_liquidation=f["net_liquidation"], daily_pnl=0.0,
            positions=tuple(f["positions"]), working_orders=tuple(f["working"]))
        if self.after_first is not None:
            self.after_first()
            self.after_first = None
        return snap


class ExitOwners:
    def __init__(self):
        self.owner = None

    def account_owner(self, account_id):
        return self.owner


class Registry:
    def __init__(self, allowed=frozenset({"ai_supervisor"})):
        self.allowed = allowed

    def resolve(self, role, method):
        if self.allowed is None:
            return None
        return SimpleNamespace(allowed_principals=self.allowed)


class CrashingStore(ExperimentStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.crash_next_insert = False

    def insert_armed(self, record, **kwargs):
        if self.crash_next_insert:
            self.crash_next_insert = False
            raise _Crash()
        return super().insert_armed(record, **kwargs)


def cmd(action, principal="cli", command_id=None, **body):
    return CommandRequest(
        command_id=command_id or f"{action}-1", action=action, account_id=ACCOUNT, target_type="experiment",
        target_id=ACCOUNT, expected_version=None, body=body, source=principal, principal=principal)


class World:
    def __init__(self, tmp_path, *, config=CONFIG, account_mode="paper"):
        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        apply_experiment_migration(SchemaMigrator(db))
        self.db = db
        self.ids = make_identities()
        self.registry = Registry()
        self.identity = self.ids["trader"]
        self.store = CrashingStore(db, ACCOUNT, lambda: NOW)
        self.exit_owners = ExitOwners()
        self.roots: list = []
        self.cash = {"base_currency": "USD", "currencies": {}}
        self.ports = ArmingPorts(
            broker=Broker(), account_cash=lambda: self.cash, resume_ready=lambda: True,
            reconciliation_safe=lambda exclude: True, breaker_clear=lambda: True, exit_owners=self.exit_owners,
            liquidation_roots=lambda: list(self.roots), old_path_armed=lambda: None, ai_paper_built=lambda: True)
        self.service = ExperimentService(store=self.store, ports=self.ports, lock=ArmingLock(), config=config,
                                         account_id=ACCOUNT, account_mode=account_mode, now=lambda: NOW)
        self.service.attach_identity_check(lambda: ai_supervisor_identity_problem(self.identity, self.registry))

    # shorthands used by the tests
    def __getattr__(self, name):
        return getattr(self.service, name)

    @property
    def id(self):
        return self.store.active().experiment_id

    @property
    def pause_generation_id(self):
        return self.store.active().pause_generation_id

    def open(self, thing):
        if thing == "position":
            self.ports.broker.set(positions=(position(100),))
        elif thing == "working_order":
            self.ports.broker.set(working=(working(),))
        elif thing == "account_owner":
            self.exit_owners.owner = SimpleNamespace(root_id="flat-1")
        elif thing == "liquidation_root":
            self.roots.append("flat-1")


def _raises(exc):
    def port(*_args):
        raise exc
    return port


def _apply_setup(world, setup):
    ports, broker = world.ports, world.ports.broker
    simple = {
        "ai_paper_disabled": lambda: setattr(world.service, "_config", replace(CONFIG, enabled=False)),
        "live_account": lambda: setattr(world.service, "_account_mode", "live"),
        "legacy_hmac_identity": lambda: setattr(world, "identity", SimpleNamespace(principal="trader")),
        "keyring_without_ai_supervisor": lambda: setattr(world, "identity", world.ids["strategy"]),
        "registry_not_attached": lambda: setattr(world, "registry", None),
        "acl_grants_cli_too": lambda: setattr(world, "registry", Registry(frozenset({"ai_supervisor", "cli"}))),
        "one_strategy_armed": lambda: setattr(ports, "old_path_armed", lambda: "ONE_STRATEGY_ARMED"),
        "broker_not_ready": lambda: setattr(ports, "resume_ready", lambda: False),
        "breaker_tripped": lambda: setattr(ports, "breaker_clear", lambda: False),
        "unresolved_command": lambda: setattr(ports, "reconciliation_safe", lambda exclude: False),
        "capture_staging": lambda: setattr(broker, "fail", BrokerRiskSnapshotError("GENERATION_STAGING", "x")),
        "capture_no_generation": lambda: setattr(broker, "fail",
                                                 BrokerRiskSnapshotError("NO_PROMOTED_GENERATION", "x")),
        "snapshot_live_mode": lambda: broker.set(mode="live"),
        "leftover_position": lambda: broker.set(positions=(position(5),)),
        "working_order": lambda: broker.set(working=(working(),)),
        "external_working_order": lambda: broker.set(working=(working("ext-1", external=True),)),
        "active_exit_owner": lambda: setattr(world.exit_owners, "owner", SimpleNamespace(root_id="r")),
        "unfinished_liquidation_root": lambda: world.roots.append("flat-9"),
        "eur_base_without_usd_rate": lambda: setattr(world, "cash", {"base_currency": "EUR", "currencies": {}}),
        "position_appears_on_second_capture": lambda: setattr(
            broker, "after_first", lambda: broker.set(positions=(position(5),))),
        "insert_crashes_once": lambda: setattr(world.store, "crash_next_insert", True),
        "ai_paper_built_raises": lambda: setattr(ports, "ai_paper_built", _raises(RuntimeError("x"))),
        "ai_paper_built_returns_non_bool": lambda: setattr(ports, "ai_paper_built", lambda: 1),
        "account_mode_raises": lambda: setattr(world.service, "_account_mode", _raises(RuntimeError("x"))),
        "account_mode_returns_non_bool": lambda: setattr(world.service, "_account_mode", lambda: True),
        "identity_check_raises": lambda: world.service.attach_identity_check(_raises(RuntimeError("x"))),
        "identity_check_returns_non_bool": lambda: world.service.attach_identity_check(lambda: True),
        "old_path_armed_raises": lambda: setattr(ports, "old_path_armed", _raises(RuntimeError("x"))),
        "old_path_armed_returns_non_bool": lambda: setattr(ports, "old_path_armed", lambda: True),
        "broker_capture_raises": lambda: setattr(broker, "fail", RuntimeError("x")),
        "broker_capture_returns_non_bool": lambda: setattr(broker, "capture", lambda account: True),
    }
    if not setup.startswith("principal_"):
        simple[setup]()


@pytest.fixture
def svc(tmp_path):
    return World(tmp_path)


@pytest.fixture
def svc_with(tmp_path):
    def build(setup):
        world = World(tmp_path)
        _apply_setup(world, setup)
        return world
    return build


def _started(world, command_id="s1"):
    world.service.start(cmd("start_experiment", command_id=command_id, reason="go"))
    return world


@pytest.fixture
def armed(svc):
    return _started(svc)


@pytest.fixture
def paused(armed):
    armed.service.pause(cmd("pause_experiment", experiment_id=armed.id, reason="p"))
    return armed


@pytest.fixture
def killed(armed):
    rec = armed.store.active()
    armed.store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="KILLED", principal="kill_monitor",
                           command_id=None, reason="hit", changes={"killed_at": NOW, "kill_seq": 1})
    return armed


@pytest.fixture
def outage_paused(armed):
    rec = armed.store.active()
    armed.store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED", principal="kill_monitor",
                           command_id=None, reason="broker data unavailable for 300 s",
                           changes={"pause_cause": "BROKER_DATA_OUTAGE", "pause_generation_id": 7})
    return armed


def principal_for(setup):
    return {"principal_ai_supervisor": "ai_supervisor", "principal_ai_research": "ai_research"}.get(setup, "cli")


# -- start -------------------------------------------------------------------

def test_start_records_the_start_net_liquidation_and_frozen_kill_line(svc):
    view = svc.start(cmd("start_experiment", reason="first run"))
    rec = svc.store.active()
    assert (view["state"], rec.start_net_liquidation, rec.start_generation_id) == ("ARMED", 100_000.0, 7)
    assert (rec.kill_drawdown_pct, rec.kill_basis, rec.base_currency, rec.start_usd_per_base) == (20.0, "start", "USD", 1.0)
    assert rec.peak_net_liquidation == rec.kill_anchor_net_liquidation == 100_000.0
    assert rec.config_digest == sha256_digest("mmr.ai-paper-config.v1", dict(CONFIG.raw_section))
    assert rec.experiment_id == experiment_id_for(ACCOUNT, "start_experiment-1")
    assert view["effective_kill_pct"] == 20.0 and view["started_at"] == NOW.isoformat()


@pytest.mark.parametrize("setup,code", [
    ("principal_ai_supervisor", "PRINCIPAL_FORBIDDEN"), ("principal_ai_research", "PRINCIPAL_FORBIDDEN"),
    ("ai_paper_disabled", "AI_PAPER_DISABLED"), ("live_account", "ACCOUNT_NOT_PAPER"),
    ("legacy_hmac_identity", "AI_SUPERVISOR_KEY_MISSING"), ("keyring_without_ai_supervisor", "AI_SUPERVISOR_KEY_MISSING"),
    ("registry_not_attached", "AI_ALLOW_LIST_NOT_LOADED"), ("acl_grants_cli_too", "AI_ALLOW_LIST_NOT_LOADED"),
    ("one_strategy_armed", "ONE_STRATEGY_ARMED"), ("broker_not_ready", "TRADER_NOT_READY"),
    ("breaker_tripped", "BREAKER_TRIPPED"), ("unresolved_command", "RECONCILIATION_INCOMPLETE"),
    ("capture_staging", "BROKER_SNAPSHOT_UNAVAILABLE"), ("capture_no_generation", "BROKER_SNAPSHOT_UNAVAILABLE"),
    ("snapshot_live_mode", "ACCOUNT_NOT_PAPER"), ("leftover_position", "NOT_FLAT"),
    ("working_order", "NOT_FLAT"), ("external_working_order", "NOT_FLAT"), ("active_exit_owner", "NOT_FLAT"),
    ("unfinished_liquidation_root", "NOT_FLAT"), ("eur_base_without_usd_rate", "START_FX_UNAVAILABLE")])
def test_each_arming_check_refuses_with_its_own_code(svc_with, setup, code):
    service = svc_with(setup)
    with pytest.raises(CommandValidationError) as exc:
        service.start(cmd("start_experiment", principal=principal_for(setup), reason="go"))
    assert exc.value.code == code
    assert service.store.latest() is None


def test_no_attached_identity_check_refuses(svc):
    svc.service._identity_check = None
    with pytest.raises(CommandValidationError, match="AI_ALLOW_LIST_NOT_LOADED"):
        svc.start(cmd("start_experiment", reason="go"))


def test_double_arm_is_refused(svc):                                  # spec edge "double arm"
    svc.start(cmd("start_experiment", command_id="s1", reason="go"))
    with pytest.raises(CommandValidationError, match="EXPERIMENT_ACTIVE"):
        svc.start(cmd("start_experiment", command_id="s2", reason="again"))


def test_arm_with_stale_evidence_is_refused(svc_with):                # spec edge "stale evidence"
    service = svc_with("broker_not_ready")                            # resume_ready() False: no current generation
    with pytest.raises(CommandValidationError, match="TRADER_NOT_READY"):
        service.start(cmd("start_experiment", reason="go"))


def test_flat_is_checked_on_the_same_capture_that_records_the_start(svc_with):
    service = svc_with("position_appears_on_second_capture")          # capture() called once only
    service.start(cmd("start_experiment", reason="go"))
    assert service.ports.broker.calls == 1


def test_crash_after_insert_leaves_a_complete_armed_experiment(svc, monkeypatch):     # K21
    monkeypatch.setattr(svc.service, "_view", lambda rec: (_ for _ in ()).throw(_Crash()))
    with pytest.raises(_Crash):
        svc.start(cmd("start_experiment", command_id="s1", reason="go"))
    monkeypatch.undo()
    with pytest.raises(CommandValidationError, match="EXPERIMENT_ACTIVE"):
        svc.start(cmd("start_experiment", command_id="s2", reason="go"))
    assert svc.store.active().state == "ARMED"


def test_crash_before_insert_leaves_nothing(svc_with):
    service = svc_with("insert_crashes_once")
    with pytest.raises(_Crash):
        service.start(cmd("start_experiment", command_id="s1", reason="go"))
    assert service.store.latest() is None
    assert service.start(cmd("start_experiment", command_id="s2", reason="go"))["state"] == "ARMED"


def test_a_held_arming_lock_refuses_busy(svc):
    lock = svc.service._lock
    with lock.hold():
        with pytest.raises(ExperimentRefused, match="ARMING_BUSY"):
            with lock.hold(timeout=0.01):
                pass


@pytest.mark.parametrize("cash,expected", [
    ({"base_currency": "USD", "currencies": {}}, StartFx("USD", 1.0, "base_is_usd")),
    ({"base_currency": "CAD", "currencies": {"USD": {"exchange_rate": 1.25}}}, StartFx("CAD", 0.8, "ib_account_values"))])
def test_start_fx(cash, expected):
    assert start_fx_from_cash(cash) == expected


@pytest.mark.parametrize("cash", [{}, {"base_currency": None}, {"base_currency": "eur"},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": None}}},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": True}}},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": math.inf}}},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": 0.0}}}, None])
def test_start_fx_refuses_without_evidence(cash):
    with pytest.raises(ExperimentRefused, match="START_FX_UNAVAILABLE"):
        start_fx_from_cash(cash)


def test_non_usd_base_records_the_rate(svc):
    svc.cash = {"base_currency": "CAD", "currencies": {"USD": {"exchange_rate": 1.25}}}
    svc.start(cmd("start_experiment", reason="go"))
    rec = svc.store.active()
    assert (rec.base_currency, rec.start_usd_per_base, rec.start_fx_source) == ("CAD", 0.8, "ib_account_values")


# -- pause / resume / stop ----------------------------------------------------

@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_supervisor"])
def test_pause_by_allowed_principals(armed, principal):
    assert armed.pause(cmd("pause_experiment", principal=principal, experiment_id=armed.id, reason="p"))["state"] == "PAUSED"


@pytest.mark.parametrize("principal", ["ai_research", "strategy", "trader"])
def test_pause_by_other_principals_is_refused(armed, principal):
    with pytest.raises(CommandValidationError, match="PRINCIPAL_FORBIDDEN"):
        armed.pause(cmd("pause_experiment", principal=principal, experiment_id=armed.id, reason="p"))


def test_pause_of_a_paused_experiment_returns_its_view(paused):
    before = len(paused.store.transitions(paused.id))
    assert paused.pause(cmd("pause_experiment", command_id="p2", experiment_id=paused.id, reason="p"))["state"] == "PAUSED"
    assert len(paused.store.transitions(paused.id)) == before


def test_pause_while_killed_is_refused(killed):
    with pytest.raises(CommandValidationError, match="EXPERIMENT_KILLED"):
        killed.pause(cmd("pause_experiment", experiment_id=killed.id, reason="p"))


@pytest.mark.parametrize("principal", ["ai_supervisor", "ai_research", "strategy", "trader"])
def test_ai_principals_cannot_resume_or_stop(paused, principal):
    for action, fn in (("resume_experiment", paused.resume), ("stop_experiment", paused.stop)):
        with pytest.raises(CommandValidationError, match="PRINCIPAL_FORBIDDEN"):
            fn(cmd(action, principal=principal, experiment_id=paused.id, reason="x"))


def test_resume_from_paused_rearms_with_positions_open(paused):
    paused.ports.broker.set(positions=(position(100),))
    assert paused.resume(cmd("resume_experiment", experiment_id=paused.id, reason="r"))["state"] == "ARMED"


@pytest.mark.parametrize("flat", [True, False])
def test_resume_from_killed_is_refused(killed, flat):                                    # K10, owner answer
    killed.ports.broker.set(positions=() if flat else (position(100),))
    with pytest.raises(CommandValidationError, match="EXPERIMENT_KILLED"):
        killed.resume(cmd("resume_experiment", experiment_id=killed.id, reason="r"))
    assert killed.store.active().state == "KILLED"


def test_after_a_kill_stop_then_start_records_a_new_baseline(killed):                  # K10 flow
    old_id = killed.id
    killed.ports.broker.set(positions=(), net_liquidation=78_000.0)
    killed.stop(cmd("stop_experiment", experiment_id=old_id, reason="done"))
    view = killed.start(cmd("start_experiment", command_id="s2", reason="new run"))
    assert view["experiment_id"] != old_id and view["start_net_liquidation"] == 78_000.0


def test_resume_after_an_outage_pause_needs_fresh_evidence(outage_paused):              # K11, K23
    outage_paused.ports.broker.set(generation_id=outage_paused.pause_generation_id)     # nothing newer yet
    with pytest.raises(CommandValidationError, match="RESUME_EVIDENCE_STALE"):
        outage_paused.resume(cmd("resume_experiment", experiment_id=outage_paused.id, reason="r"))
    outage_paused.ports.broker.set(generation_id=outage_paused.pause_generation_id + 1)
    outage_paused.ports.reconciliation_safe = lambda exclude: False
    with pytest.raises(CommandValidationError, match="RECONCILIATION_INCOMPLETE"):
        outage_paused.resume(cmd("resume_experiment", experiment_id=outage_paused.id, reason="r"))
    outage_paused.ports.reconciliation_safe = lambda exclude: True
    outage_paused.ports.resume_ready = lambda: False
    with pytest.raises(CommandValidationError, match="TRADER_NOT_READY"):
        outage_paused.resume(cmd("resume_experiment", experiment_id=outage_paused.id, reason="r"))
    outage_paused.ports.resume_ready = lambda: True
    view = outage_paused.resume(cmd("resume_experiment", experiment_id=outage_paused.id, reason="r"))
    assert (view["state"], view["pause_cause"]) == ("ARMED", None)
    assert outage_paused.store.active().pause_generation_id is None


def test_resume_requires_ai_paper_enabled(paused):
    paused.service._config = replace(CONFIG, enabled=False)
    with pytest.raises(CommandValidationError, match="AI_PAPER_DISABLED"):
        paused.resume(cmd("resume_experiment", experiment_id=paused.id, reason="r"))


def test_pause_and_stop_work_with_ai_paper_disabled(armed):                              # K15
    armed.service._config = replace(CONFIG, enabled=False)
    experiment_id = armed.id
    armed.pause(cmd("pause_experiment", experiment_id=experiment_id, reason="p"))
    assert armed.stop(cmd("stop_experiment", experiment_id=experiment_id, reason="s"))["state"] == "STOPPED"


def test_resume_or_stop_with_the_wrong_experiment_id(paused):
    for fn, action in ((paused.resume, "resume_experiment"), (paused.stop, "stop_experiment"),
                       (paused.pause, "pause_experiment")):
        with pytest.raises(CommandValidationError, match="EXPERIMENT_MISMATCH"):
            fn(cmd(action, experiment_id="exp-" + "0" * 20, reason="r"))


def test_commands_without_any_experiment(svc):
    with pytest.raises(CommandValidationError, match="NO_EXPERIMENT"):
        svc.pause(cmd("pause_experiment", experiment_id="exp-" + "0" * 20, reason="p"))


@pytest.mark.parametrize("open_thing", ["position", "working_order", "account_owner", "liquidation_root"])
def test_stop_refused_while_not_flat(killed, open_thing):
    killed.open(open_thing)
    with pytest.raises(CommandValidationError, match="NOT_FLAT"):
        killed.stop(cmd("stop_experiment", experiment_id=killed.id, reason="s"))
    assert killed.store.active().state == "KILLED"


def test_stop_refused_without_broker_evidence(killed):
    killed.ports.broker.fail = BrokerRiskSnapshotError("NO_PROMOTED_GENERATION", "x")
    with pytest.raises(CommandValidationError, match="BROKER_SNAPSHOT_UNAVAILABLE"):
        killed.stop(cmd("stop_experiment", experiment_id=killed.id, reason="s"))


def test_stop_moves_killed_to_stopped_without_rearming_and_releases_the_lock(killed):
    experiment_id = killed.id
    view = killed.stop(cmd("stop_experiment", experiment_id=experiment_id, reason="done"))
    assert view["state"] == "STOPPED" and killed.store.active() is None
    assert killed.store.transitions(experiment_id)[-1]["from_state"] == "KILLED"
    with pytest.raises(CommandValidationError, match="EXPERIMENT_STOPPED"):
        killed.resume(cmd("resume_experiment", experiment_id=experiment_id, reason="r"))


@pytest.mark.parametrize("port,code", [("ai_paper_built", "AI_PAPER_CONFIG_UNREADABLE"),
    ("account_mode", "ACCOUNT_MODE_UNREADABLE"), ("identity_check", "IDENTITY_CHECK_UNREADABLE"),
    ("old_path_armed", "ONE_STRATEGY_STATE_UNREADABLE"), ("broker_capture", "BROKER_SNAPSHOT_UNAVAILABLE")])
@pytest.mark.parametrize("failure", ["raises", "returns_non_bool"])
def test_unreadable_arming_evidence_refuses_with_its_own_code(svc_with, port, code, failure):   # K12, owner answer
    service = svc_with(f"{port}_{failure}")
    with pytest.raises(CommandValidationError) as exc:
        service.start(cmd("start_experiment", reason="go"))
    assert exc.value.code == code and service.store.latest() is None


@pytest.mark.parametrize("body", [{"reason": True}, {"reason": ""}, {"reason": "x" * 201},
    {"experiment_id": 7, "reason": "r"}, {"experiment_id": "exp-1", "reason": "r"},
    {"experiment_id": "exp-" + "a" * 20, "reason": "r", "extra": 1}, {"experiment_id": "exp-" + "a" * 20}])
def test_bodies_are_strict_in_process_too(paused, body):
    with pytest.raises(CommandValidationError, match="EXPERIMENT_REQUEST_INVALID"):
        paused.pause(replace(cmd("pause_experiment", experiment_id=paused.id, reason="p"), body=body))


@pytest.mark.parametrize("body", [{}, {"reason": 1}, {"reason": "go", "experiment_id": "exp-" + "a" * 20}])
def test_start_body_is_strict(svc, body):
    with pytest.raises(CommandValidationError, match="EXPERIMENT_REQUEST_INVALID"):
        svc.start(replace(cmd("start_experiment", reason="go"), body=body))
    assert svc.store.latest() is None


def test_reconciliation_excludes_the_command_being_executed(svc):
    seen = []
    svc.ports.reconciliation_safe = lambda exclude: seen.append(exclude) or True
    svc.start(cmd("start_experiment", command_id="s9", reason="go"))
    assert seen == ["s9"]
