"""SP1 Plan 4 Task 4: kill detection, KILLED before any order, the entry block and the outage pause."""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

import pytest

from tests.automation.experiment_fixtures import ACCOUNT, NOW, armed_record
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.experiments import ExperimentStore, apply_experiment_migration
from trader.automation.kill_monitor import KillLineMonitor
from trader.data.broker_state import BrokerRiskSnapshot, BrokerRiskSnapshotError
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

CONFIG = AiPaperConfig(enabled=True, experiment_kill_drawdown_pct=20.0)


def snap(generation=8, nlv=100_000.0, mode="paper", account=ACCOUNT, positions=(), working=()):
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=generation, promoted_at=NOW, account_id=account,
        account_mode=mode, net_liquidation=nlv, daily_pnl=0.0, positions=tuple(positions),
        working_orders=tuple(working))


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += dt.timedelta(**delta)


class Broker:
    def __init__(self):
        self.current = snap()
        self.error: Optional[Exception] = None

    def push(self, *, nlv=None, generation=None, **changes):
        fields = {"nlv": self.current.net_liquidation if nlv is None else nlv,
                  "generation": self.current.generation_id if generation is None else generation}
        self.current = snap(**fields, **changes)
        self.error = None

    def fail_with(self, fault):
        if fault == "live_mode":
            self.current = dataclasses.replace(self.current, account_mode="live")
        elif fault == "ACCOUNT_MISMATCH":
            self.current = dataclasses.replace(self.current, account_id="DU999")
        else:
            self.error = BrokerRiskSnapshotError(fault, "forced by the test")

    def capture(self, account_id):
        if self.error is not None:
            raise self.error
        return self.current


class Session:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.on_flatten = None

    def flatten_account_now(self, cause, deadline):
        if self.on_flatten is not None:
            self.on_flatten(cause, deadline)
        self.calls.append((cause, deadline))
        return cause


class Liquidation:
    def __init__(self):
        self.receipts = {}

    def receipt_for(self, root_id):
        return self.receipts.get(root_id)


class Outbox:
    def __init__(self):
        self.calls = []

    def enqueue(self, event_id, kind, text):
        self.calls.append((event_id, kind, text))
        return True

    def kinds(self):
        return [kind for _e, kind, _t in self.calls]


class World:
    def __init__(self, tmp_path, *, config=CONFIG, record=None, recover=True):
        self.tmp_path = tmp_path
        self.config = config
        self.db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(self.db)
        self.journal = DomainJournal(self.db)
        self.journal.migrate(migrator)
        apply_experiment_migration(migrator)
        self.clock = Clock()
        self.broker = Broker()
        self.outbox = Outbox()
        self.store = ExperimentStore(self.db, ACCOUNT, self.clock)
        if record is not None:
            self.store.insert_armed(record, principal="cli", reason="go")
        self.seen = []
        self._build(recover)

    def _build(self, recover):
        self.session = Session(self.store)
        self.liquidation = Liquidation()
        self.monitor = KillLineMonitor(store=self.store, broker=self.broker, session=self.session,
                                       liquidation=self.liquidation, config=self.config, account_id=ACCOUNT, now=self.clock,
                                       journal=self.journal)
        self.monitor.attach_notices(alerts=self.outbox)
        if recover:
            self.monitor.recover()

    def restart(self):
        self.store = ExperimentStore(self.db, ACCOUNT, self.clock)
        self._build(recover=False)
        return self

    def pause(self):
        rec = self.store.active()
        self.store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED", principal="cli",
                              command_id="p", reason="p")

    def events(self, kind):
        return self.db.execute("SELECT count(*) FROM domain_event_journal WHERE event_type = ?", [kind],
                               fetch="one")[0]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, record=armed_record())


@pytest.fixture
def world_without_kill_line(tmp_path):
    return World(tmp_path, config=AiPaperConfig(enabled=True), record=armed_record(kill_drawdown_pct=None))


@pytest.fixture
def world_peak(tmp_path):
    return World(tmp_path, record=armed_record(kill_basis="peak"))


@pytest.fixture
def world_fresh(tmp_path):
    return World(tmp_path, record=armed_record(), recover=False)


def test_kill_line_hit_writes_killed_before_the_first_flatten_call(world):
    world.broker.push(nlv=79_000.0)
    world.session.on_flatten = lambda cause, deadline: world.seen.append(world.store.active().state)
    world.monitor.tick()
    rec = world.store.active()
    assert world.seen == ["KILLED"]
    assert (rec.state, rec.kill_seq, rec.kill_root_id, rec.kill_flatten_root) == (
        "KILLED", 1, f"experiment-kill-{rec.experiment_id}-1-0", f"experiment-kill-{rec.experiment_id}-1-0")
    assert (rec.kill_net_liquidation, rec.kill_observed_drawdown_pct) == (79_000.0, pytest.approx(21.0))
    assert (rec.kill_generation_id, rec.killed_at) == (8, NOW)
    assert world.session.calls == [(rec.kill_root_id, NOW + dt.timedelta(seconds=300))]
    assert world.store.transitions(rec.experiment_id)[-1]["principal"] == "kill_monitor"


def test_no_kill_above_the_line(world):
    world.broker.push(nlv=80_500.0)
    world.monitor.tick()
    assert world.store.active().state == "ARMED" and world.session.calls == []
    assert world.monitor.last_evaluation.drawdown_pct == pytest.approx(19.5)


def test_kill_line_off_never_kills(world_without_kill_line):
    world_without_kill_line.broker.push(nlv=10_000.0)
    world_without_kill_line.monitor.tick()
    assert world_without_kill_line.store.active().state == "ARMED"
    assert world_without_kill_line.monitor.entry_block(world_without_kill_line.store.active()) is None


def test_a_tighter_loaded_config_kills_earlier(tmp_path):                         # K9
    w = World(tmp_path, config=AiPaperConfig(enabled=True, experiment_kill_drawdown_pct=10.0),
              record=armed_record(kill_drawdown_pct=20.0))
    w.broker.push(nlv=89_000.0)
    w.monitor.tick()
    assert w.store.active().state == "KILLED"


def test_peak_basis_kills_from_the_highest_value_since_start(world_peak):
    for nlv in (100_000.0, 125_000.0, 101_000.0, 100_000.0):
        world_peak.broker.push(nlv=nlv)
        world_peak.monitor.tick()
    assert world_peak.store.active().state == "KILLED"          # 20% below 125k


def test_start_basis_ignores_the_peak(world):
    for nlv in (100_000.0, 125_000.0, 100_000.0):
        world.broker.push(nlv=nlv)
        world.monitor.tick()
    rec = world.store.active()
    assert rec.state == "ARMED" and rec.peak_net_liquidation == 125_000.0


def test_paused_experiment_is_killed_too(world):
    world.pause()
    world.broker.push(nlv=70_000.0)
    world.monitor.tick()
    assert world.store.active().state == "KILLED"


def test_peak_rises_while_paused(world):                                            # K8
    world.pause()
    world.broker.push(nlv=110_000.0)
    world.monitor.tick()
    assert world.store.active().peak_net_liquidation == 110_000.0


@pytest.mark.parametrize("fault", ["NO_PROMOTED_GENERATION", "GENERATION_STAGING", "INVALID_NET_LIQUIDATION",
                                   "DAILY_PNL_UNAVAILABLE", "ACCOUNT_MISMATCH", "live_mode"])
def test_invalid_net_liquidation_never_kills_and_blocks_entries(world, fault):           # Review Focus 1, K7
    world.broker.push(nlv=50_000.0)
    world.broker.fail_with(fault)
    world.monitor.tick()
    rec = world.store.active()
    assert rec.state == "ARMED" and world.session.calls == []
    world.clock.advance(seconds=31)
    world.monitor.tick()
    assert world.monitor.entry_block(rec) == "KILL_LINE_UNKNOWN"
    assert world.events("experiment.kill_line_unknown") == 1


@pytest.mark.parametrize("nlv", [0.0, float("nan"), -1.0])
def test_a_bad_net_liquidation_value_is_unknown_not_a_kill(world, nlv):
    world.broker.current = dataclasses.replace(world.broker.current, net_liquidation=nlv)
    world.monitor.tick()
    assert world.store.active().state == "ARMED" and world.session.calls == []


def test_entry_block_clears_within_the_stale_window(world):
    world.monitor.tick()
    rec = world.store.active()
    assert world.monitor.entry_block(rec) is None
    world.clock.advance(seconds=30)
    assert world.monitor.entry_block(rec) is None
    world.clock.advance(seconds=1)
    assert world.monitor.entry_block(rec) == "KILL_LINE_UNKNOWN"


def test_unknown_streak_ends_on_the_next_good_capture(world):
    world.broker.fail_with("NO_PROMOTED_GENERATION")
    world.monitor.tick()
    world.clock.advance(seconds=31)
    assert world.monitor.entry_block(world.store.active()) == "KILL_LINE_UNKNOWN"
    world.broker.push(nlv=100_000.0)
    world.monitor.tick()
    assert world.monitor.entry_block(world.store.active()) is None
    world.broker.fail_with("NO_PROMOTED_GENERATION")
    world.monitor.tick()
    assert world.events("experiment.kill_line_unknown") == 2                     # one per streak


def test_long_outage_pauses_durably_alerts_and_never_flattens(world):              # K23, owner answer
    world.broker.fail_with("NO_PROMOTED_GENERATION")
    world.monitor.tick()
    world.clock.advance(seconds=299)
    world.monitor.tick()
    assert world.store.active().state == "ARMED"
    world.clock.advance(seconds=2)
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.state, rec.pause_cause, rec.pause_generation_id) == ("PAUSED", "BROKER_DATA_OUTAGE", 7)
    assert world.session.calls == [] and world.outbox.kinds() == ["broker_outage"]
    event_id, _kind, text = world.outbox.calls[0]
    assert event_id == f"broker_outage:{rec.experiment_id}:{rec.revision}" and text.startswith("PAPER")
    world.monitor.tick()
    assert world.outbox.kinds() == ["broker_outage"]                              # once per streak
    world.restart().monitor.recover()
    assert world.store.active().state == "PAUSED"                                  # durable


def test_outage_pause_records_the_last_good_generation(world):
    world.broker.push(generation=11)
    world.monitor.tick()
    world.broker.fail_with("GENERATION_STAGING")
    world.monitor.tick()
    world.clock.advance(seconds=301)
    world.monitor.tick()
    assert world.store.active().pause_generation_id == 11


def test_outage_with_an_operator_pause_alerts_but_keeps_the_pause(world):
    world.pause()
    before = world.store.active()
    world.broker.fail_with("NO_PROMOTED_GENERATION")
    world.monitor.tick()
    world.clock.advance(seconds=301)
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.state, rec.pause_cause, rec.revision) == ("PAUSED", None, before.revision)
    assert world.outbox.kinds() == ["broker_outage"]


def test_outage_alert_without_an_outbox_is_not_pending(world):
    world.monitor.attach_notices(alerts=None)
    world.broker.fail_with("NO_PROMOTED_GENERATION")
    world.monitor.tick()
    world.clock.advance(seconds=301)
    world.monitor.tick()
    world.monitor.tick()
    assert world.store.active().state == "PAUSED"


def test_outage_pause_seconds_comes_from_config(tmp_path):
    w = World(tmp_path, config=AiPaperConfig(enabled=True, experiment_kill_drawdown_pct=20.0,
                                             broker_outage_pause_seconds=60), record=armed_record())
    w.broker.fail_with("NO_PROMOTED_GENERATION")
    w.monitor.tick()
    w.clock.advance(seconds=60)
    w.monitor.tick()
    assert w.store.active().state == "PAUSED"


def test_generation_regression_is_unknown(world):                               # Review Focus 2
    world.broker.push(generation=9, nlv=100_000.0)
    world.monitor.tick()
    world.broker.push(generation=8, nlv=50_000.0)
    world.monitor.tick()
    assert world.store.active().state == "ARMED"
    assert world.store.active().peak_net_liquidation == 100_000.0


def test_entries_blocked_until_recovered(world_fresh):
    assert world_fresh.monitor.entry_block(world_fresh.store.active()) == "EXPERIMENT_MONITOR_NOT_READY"
    world_fresh.monitor.recover()
    world_fresh.monitor.tick()
    assert world_fresh.monitor.entry_block(world_fresh.store.active()) is None


def test_entry_block_is_none_for_a_non_armed_record(world):
    world.pause()
    assert world.monitor.entry_block(world.store.active()) is None


def test_kill_line_survives_a_restart(world):
    world.broker.push(nlv=79_000.0)
    restarted = world.restart()                                   # new store + monitor on the same file
    restarted.monitor.recover()
    restarted.monitor.tick()
    assert restarted.store.active().state == "KILLED"


def test_a_killed_experiment_stays_killed_after_a_restart(world):
    world.broker.push(nlv=79_000.0)
    world.monitor.tick()
    restarted = world.restart()
    restarted.monitor.recover()
    assert restarted.store.active().state == "KILLED"             # a restart never clears KILLED


def test_no_experiment_no_capture(tmp_path):
    w = World(tmp_path)
    w.broker.fail_with("NO_PROMOTED_GENERATION")
    w.monitor.tick()
    assert w.events("experiment.kill_line_unknown") == 0
