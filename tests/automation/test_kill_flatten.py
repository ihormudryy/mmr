"""SP1 Plan 4 Task 5: the kill flatten through Plan 1's account flatten, flat proof, rounds, notices, recovery.

The world is a real LiquidationService + ExitOwnerRegistry + SessionController over one DuckDB file,
with migration 70 and a real ExperimentStore. Only the broker and the order dispatch are fakes.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

from tests.automation.experiment_fixtures import NOW, armed_record
from tests.test_liquidation_service import _Breaker, _Dispatch, _order, _position, _row
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.experiments import ExperimentStore, apply_experiment_migration
from trader.automation.kill_monitor import KillLineMonitor, KillSessionEnd, kill_alert_text
from trader.automation.session_controller import SessionController, apply_session_controller_migration
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import LiquidationRunStore, LiquidationService, apply_liquidation_migration

ACCOUNT = "DU123"                      # the account of tests/test_liquidation_service.py rows
ET = ZoneInfo("America/New_York")
ET_DATE = NOW.astimezone(ET).date()
CONFIG = AiPaperConfig(enabled=True, experiment_kill_drawdown_pct=20.0)


class _Crash(BaseException):
    """Simulated process death."""


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += dt.timedelta(**delta)


class Broker:
    """One current snapshot, shared by the liquidation service and the monitor. ``last`` and ``held``
    are what tests/test_liquidation_service.py's _Dispatch reads."""

    def __init__(self):
        self.current = self.snap(8)
        self.held = False

    @staticmethod
    def snap(generation, positions=(), working=(), nlv=100_000.0):
        return BrokerRiskSnapshot(
            generation_id=generation, source_cursor=generation, promoted_at=NOW, account_id=ACCOUNT,
            account_mode="paper", net_liquidation=nlv, daily_pnl=0.0, positions=tuple(positions),
            working_orders=tuple(working))

    @property
    def last(self):
        return self.current.generation_id

    def capture(self, account_id):
        return self.current

    def show(self, *, generation=None, positions=None, working=None, nlv=None):
        c = self.current
        self.current = self.snap(c.generation_id + 1 if generation is None else generation,
                                 c.positions if positions is None else positions,
                                 c.working_orders if working is None else working,
                                 c.net_liquidation if nlv is None else nlv)


class Outbox:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    @property
    def sent(self):
        return self.calls

    def enqueue(self, event_id, kind, text):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("telegram outbox down")
        self.calls.append((event_id, kind, text))
        return True


class SessionEnd:
    def __init__(self):
        self.calls = []

    def record_session_end(self, end):
        self.calls.append(end)


class _Cancel:
    def cancel_working_entries(self, *, root_command_id, orders):
        return []


class _SessionBreaker:
    def record(self, signal):
        return SimpleNamespace(state="TRIPPED")


class World:
    def __init__(self, tmp_path, *, notices=True):
        self.db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(self.db)
        self.journal = DomainJournal(self.db)
        self.journal.migrate(migrator)
        for apply in (apply_exit_owner_migration, apply_liquidation_migration, apply_experiment_migration,
                      apply_session_controller_migration):
            apply(migrator)
        self.clock = Clock()
        self.broker = Broker()
        self.dispatch = _Dispatch(self.broker)
        self.breaker = _Breaker()
        self.outbox = Outbox()
        self.session_end = SessionEnd()
        self.notices = notices
        self.reconciliation_safe = True
        self.crash_point: Optional[str] = None
        ExperimentStore(self.db, ACCOUNT, self.clock).insert_armed(armed_record(ACCOUNT), principal="cli",
                                                                    reason="go")
        self._build()

    def _build(self):
        self.store = ExperimentStore(self.db, ACCOUNT, self.clock)
        self.registry = ExitOwnerRegistry(self.db)
        self.liquidation = LiquidationService(
            self.broker, self.dispatch, store=LiquidationRunStore(self.db), registry=self.registry,
            now=self.clock, breaker=self.breaker, schedule_reconcile=lambda command_id: None)
        self.session = SessionController(
            journal=self.journal, db=self.db, calendar=XNYSCalendarPolicy(), broker=self.broker, cancel=_Cancel(),
            liquidation=self.liquidation, breaker=_SessionBreaker(),
            time_exit=SimpleNamespace(request_exit=lambda **kwargs: None), account_id=ACCOUNT, now=self.clock)
        self.monitor = KillLineMonitor(
            store=self.store, broker=self.broker, session=self.session, liquidation=self.liquidation,
            config=CONFIG, account_id=ACCOUNT, now=self.clock, journal=self.journal,
            reconciliation_safe=lambda: self.reconciliation_safe)
        if self.notices:
            self.monitor.attach_notices(alerts=self.outbox, session_end=self.session_end)
        self.monitor.recover()

    def restart(self):
        self._build()
        return self

    @property
    def id(self):
        return self.store.active().experiment_id

    def next_generation(self):
        return self.broker.current.generation_id + 1

    def hold_position(self, *, conid, quantity, nlv):
        positions = tuple(p for p in self.broker.current.positions if p.conid != conid) + (
            _position(quantity, conid=conid),)
        self.broker.show(positions=positions, nlv=nlv)

    def working_entry(self, *, conid, quantity, filled=0.0, group):
        order = _order(entity=f"entry:{conid}", group=group, conid=conid, leg="entry", filled=filled, total=quantity)
        self.broker.show(working=self.broker.current.working_orders + (order,))
        return order

    def broker_shows_flat(self, generation):
        self.broker.show(generation=generation, positions=(), working=())

    def fill_reduces(self):
        for call in self.dispatch.calls:
            if call[0] == "reduce":
                _kind, _conid, _side, quantity, child_id = call
                self.dispatch.rows[child_id] = [_row("Filled", filled=quantity, total=quantity)]

    def run_liquidation(self):
        return self.liquidation.rescan()

    def drive_kill_to_flat(self):
        if self.store.active().state != "KILLED":
            self.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
            self.monitor.tick()
        self.monitor.tick()                       # a new round starts its root on this tick
        self.fill_reduces()
        self.broker_shows_flat(self.next_generation())
        self.run_liquidation()
        self.broker_shows_flat(self.next_generation())
        self.run_liquidation()
        self.monitor.tick()

    def miss_the_flatten_deadline(self):
        self.clock.advance(seconds=301)
        self.broker.show()
        self.run_liquidation()

    def liquidation_roots(self):
        return {row[0] for row in self.db.execute("SELECT cause_command_id FROM liquidation_runs", fetch="all")}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def world_without_notices(tmp_path):
    return World(tmp_path, notices=False)


def test_kill_flattens_through_the_account_owner_and_reports_flat_only_on_broker_evidence(world):
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.monitor.tick()
    rec = world.store.active()
    owner = world.registry.account_owner(ACCOUNT)
    assert (owner.root_id, owner.kind) == (rec.kill_flatten_root, "account_flatten")
    assert rec.kill_flat_state == "PENDING"                              # reduce sent, not proven
    assert [c[0] for c in world.dispatch.calls] == ["reduce"]
    world.fill_reduces()
    world.broker_shows_flat(world.next_generation())
    world.run_liquidation()
    world.monitor.tick()
    assert world.store.active().kill_flat_state == "PENDING"              # the fill is observed, not proven
    world.broker_shows_flat(world.next_generation())
    world.run_liquidation()
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.kill_flat_state, rec.kill_flat_generation) == ("FLAT", world.broker.current.generation_id)
    assert world.liquidation.receipt_for(rec.kill_flatten_root).state == "FLAT"


def test_flat_is_not_reported_on_a_capture_older_than_the_root_proof(world):
    world.drive_kill_to_flat()
    rec = world.store.active()
    assert rec.kill_flat_state == "FLAT"
    proof = world.liquidation.receipt_for(rec.kill_flatten_root).generation_id
    # Replay the proof step with a capture older than the root's proof.
    world.store.update_kill_progress(rec.experiment_id, expected_state="KILLED",
                                     changes={"kill_flat_state": "PENDING", "kill_flat_generation": None,
                                              "kill_flat_at": None})
    world.broker.current = Broker.snap(proof - 1)
    world.monitor.tick()
    assert world.store.active().kill_flat_state == "PENDING"
    world.broker.current = Broker.snap(proof)
    world.monitor.tick()
    assert world.store.active().kill_flat_state == "FLAT"


def test_flat_is_not_reported_while_a_command_is_unresolved(world):
    world.reconciliation_safe = False
    world.drive_kill_to_flat()
    assert world.store.active().kill_flat_state == "PENDING"
    world.reconciliation_safe = True
    world.monitor.tick()
    assert world.store.active().kill_flat_state == "FLAT"


def test_kill_during_an_open_entry_cancels_it_before_reducing(world):          # spec edge
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.working_entry(conid=2, quantity=100, group="og-aip-dec-00000001")
    world.monitor.tick()
    world.dispatch.entities["entry:2"] = _row("Cancelled", total=100)
    world.broker.show(working=())
    world.run_liquidation()
    kinds = [call[0] for call in world.dispatch.calls]
    assert kinds.index("cancel") < kinds.index("reduce")


def test_kill_during_a_partial_fill_cancels_the_rest_and_reduces_the_filled_part(world):     # spec edge
    world.hold_position(conid=2, quantity=40.0, nlv=79_000.0)
    world.working_entry(conid=2, quantity=100, filled=40, group="og-aip-dec-00000001")
    world.monitor.tick()
    world.dispatch.entities["entry:2"] = _row("Cancelled", filled=40, total=100)
    world.broker.show(working=())
    world.run_liquidation()
    reduces = [c for c in world.dispatch.calls if c[0] == "reduce"]
    cancels = [c[1] for c in world.dispatch.calls if c[0] == "cancel"]
    assert [(c[1], c[2], c[3]) for c in reduces] == [(2, "SELL", 40.0)] and cancels == ["entry:2"]


def test_late_entry_after_flat_starts_a_new_round(world):                        # K6
    world.drive_kill_to_flat()
    world.hold_position(conid=2, quantity=100.0, nlv=78_000.0)                   # entry acknowledged late
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.kill_round, rec.kill_flat_state, rec.kill_flatten_root) == (1, "PENDING", None)
    assert rec.kill_root_id.endswith("-1-1")
    world.monitor.tick()
    assert world.store.active().kill_flatten_root.endswith("-1-1")
    assert world.registry.account_owner(ACCOUNT).root_id.endswith("-1-1")


def test_a_late_fill_close_and_a_new_round_make_one_root(world):                 # K6 reuses Plan 1's rule
    world.drive_kill_to_flat()
    kill_root = world.store.active().kill_flatten_root
    reduce_child = [c[4] for c in world.dispatch.calls if c[0] == "reduce"][0]
    world.dispatch.rows[reduce_child] = [_row("Filled", filled=350.0, total=350.0)]   # 50 more filled later
    world.hold_position(conid=1, quantity=-50.0, nlv=78_000.0)
    world.run_liquidation()                                  # Plan 1 starts its late-fill close first
    late_root = f"{kill_root}-late-1"
    assert world.registry.account_owner(ACCOUNT).root_id == late_root
    world.monitor.tick()                                     # exposure after FLAT: new round
    world.monitor.tick()                                     # the round joins the late close
    rec = world.store.active()
    assert (rec.kill_round, rec.kill_flatten_root) == (1, late_root)
    assert world.liquidation_roots() == {kill_root, late_root}


def test_failed_safe_flatten_ends_the_kill_without_a_new_round(world):
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.monitor.tick()
    world.miss_the_flatten_deadline()
    world.monitor.tick()
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.kill_flat_state, rec.kill_round) == ("FAILED_SAFE", 0)
    assert world.breaker.calls                                                   # tripped by LiquidationService
    assert world.session_end.calls == [KillSessionEnd(ACCOUNT, ET_DATE, "KILLED", rec.kill_flat_at)]
    assert rec.kill_session_end_state == "RECORDED"


def test_session_end_notice_once_after_flat(world):
    world.drive_kill_to_flat()
    world.monitor.tick()
    world.monitor.tick()
    assert len(world.session_end.calls) == 1 and world.session_end.calls[0].state == "KILLED"
    assert world.session_end.calls[0].session_date == ET_DATE


def test_alert_goes_through_the_outbox_once(world):
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.monitor.tick()
    world.monitor.tick()
    rec = world.store.active()
    assert world.outbox.calls == [(f"kill_started:{rec.experiment_id}:1", "kill_started", kill_alert_text(rec))]
    assert kill_alert_text(rec).startswith("PAPER") and "never resumed" in kill_alert_text(rec)
    assert rec.kill_alert_state == "ENQUEUED"


def test_alert_failure_never_blocks_the_flatten_and_retries_once(world):         # Review Focus 5
    world.outbox.fail_next = True
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.monitor.tick()
    assert world.registry.account_owner(ACCOUNT) is not None
    assert world.store.active().kill_alert_state == "PENDING"
    world.monitor.tick()
    assert world.store.active().kill_alert_state == "ENQUEUED" and len(world.outbox.sent) == 1
    world.monitor.tick()
    assert len(world.outbox.sent) == 1


def test_without_plan5_ports_nothing_is_left_pending(world_without_notices):
    world_without_notices.drive_kill_to_flat()
    rec = world_without_notices.store.active()
    assert (rec.kill_alert_state, rec.kill_session_end_state) == ("NO_OUTBOX", "NO_SINK")


def test_kill_while_the_session_flatten_owns_the_account_joins_it(world):       # spec edge, K3
    world.hold_position(conid=1, quantity=300.0, nlv=100_000.0)
    session_root = world.session.flatten_account_now("session-flatten-abc", NOW + dt.timedelta(minutes=10))
    world.broker.show(nlv=79_000.0)
    world.monitor.tick()
    rec = world.store.active()
    assert rec.state == "KILLED" and rec.kill_flatten_root == session_root
    assert world.liquidation_roots() == {session_root}


def test_session_flatten_after_the_kill_joins_the_kill_root(world):
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.monitor.tick()
    kill_root = world.store.active().kill_flatten_root
    world.session.recover(NOW)
    flatten_start = dt.datetime(2026, 7, 17, 15, 45, tzinfo=ET).astimezone(dt.timezone.utc)
    world.clock.now = flatten_start
    state = world.session.run_due(flatten_start)
    assert state.flatten_command_id == kill_root


@pytest.mark.parametrize("crash_point", ["after_killed_write", "after_start_before_root_saved", "after_root_saved"])
def test_restart_while_killed_resumes_the_same_flatten(world, crash_point):     # K5
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    real_flatten = world.session.flatten_account_now

    def crash_before_start(cause, deadline):
        raise _Crash()

    def crash_after_start(cause, deadline):
        real_flatten(cause, deadline)
        raise _Crash()
    if crash_point == "after_killed_write":
        world.session.flatten_account_now = crash_before_start
    elif crash_point == "after_start_before_root_saved":
        world.session.flatten_account_now = crash_after_start
    else:
        world.outbox.enqueue = lambda *a: (_ for _ in ()).throw(_Crash())
    with pytest.raises(_Crash):
        world.monitor.tick()
    world.outbox = Outbox()                                       # the new process has a working outbox
    assert world.store.active().state == "KILLED"
    restarted = world.restart()
    restarted.liquidation.rescan()
    restarted.drive_kill_to_flat()
    rec = restarted.store.active()
    assert rec.kill_flat_state == "FLAT" and rec.kill_flatten_root == rec.kill_root_id
    assert len(restarted.liquidation_roots()) == 1                # never a second root for one round


def test_resume_racing_a_kill_never_loses_the_kill(world):                     # Review Focus 3
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    real = world.store.transition
    paused = []

    def pause_first(experiment_id, **kwargs):
        if kwargs.get("to") == "KILLED" and not paused:
            paused.append(real(experiment_id, expected=frozenset({"ARMED"}), to="PAUSED", principal="cli",
                               command_id="p", reason="p"))
        return real(experiment_id, **kwargs)
    world.store.transition = pause_first
    world.monitor.tick()
    assert paused and world.store.active().state == "KILLED"                     # kill CAS accepts PAUSED as well


def test_no_order_before_killed_is_committed(world):
    real = world.store.transition
    failed = []

    def fail_once(experiment_id, **kwargs):
        if kwargs.get("to") == "KILLED" and not failed:
            failed.append(True)
            raise RuntimeError("journal write failed")
        return real(experiment_id, **kwargs)
    world.store.transition = fail_once
    world.hold_position(conid=1, quantity=300.0, nlv=79_000.0)
    world.monitor.tick()
    assert world.dispatch.calls == [] and world.registry.account_owner(ACCOUNT) is None
    assert world.store.active().state == "ARMED"
    world.monitor.tick()                                                          # the next tick kills
    assert world.store.active().state == "KILLED" and world.dispatch.calls


def test_a_stop_during_the_kill_ends_the_tick_quietly(world):
    world.drive_kill_to_flat()
    rec = world.store.active()
    world.store.transition(rec.experiment_id, expected=frozenset({"KILLED"}), to="STOPPED", principal="cli",
                           command_id="s", reason="done")
    world.monitor.tick()
    assert world.store.active() is None
