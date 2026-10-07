"""SP1 Plan 4 Task 1: the durable experiment table, record and state machine."""
from __future__ import annotations

import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from trader.automation.experiments import (
    ExperimentRefused, ExperimentStore, apply_experiment_migration, experiment_id_for,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

from tests.automation.experiment_fixtures import ACCOUNT, NOW, armed_record


@pytest.fixture
def db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_experiment_migration(SchemaMigrator(db))
    return db


@pytest.fixture
def store(db):
    return ExperimentStore(db, ACCOUNT, lambda: NOW)


def tables(db) -> set:
    return {row[0] for row in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}


def _try(fn):
    try:
        fn()
        return "ok"
    except ExperimentRefused as exc:
        return exc.code


def _try_insert(db, command_id):
    return _try(lambda: ExperimentStore(db, ACCOUNT, lambda: NOW).insert_armed(
        armed_record(start_command_id=command_id), principal="cli", reason="go"))


def _drive_to(store, state) -> ExperimentRecord:
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    path = {"ARMED": [], "PAUSED": ["PAUSED"], "KILLED": ["KILLED"], "STOPPED": ["STOPPED"]}[state]
    for to in path:
        rec = store.transition(rec.experiment_id, expected=frozenset({rec.state}), to=to,
                               principal="cli", command_id=None, reason="setup")
    return rec


def test_migration_70_creates_the_three_tables(db):
    assert {"experiments", "experiment_active", "experiment_transitions"} <= tables(db)
    assert db.execute("SELECT version FROM schema_migrations WHERE version = 70", fetch="one")
    assert apply_experiment_migration(SchemaMigrator(db)) is False


def test_experiment_id_is_deterministic_and_colon_free():
    a = experiment_id_for("DU1", "start-1")
    assert a == experiment_id_for("DU1", "start-1") and a.startswith("exp-") and ":" not in a
    assert a != experiment_id_for("DU1", "start-2")
    assert a != experiment_id_for("DU2", "start-1")


def test_insert_armed_then_reads(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.active() == rec == store.latest() == store.get(rec.experiment_id)
    assert (rec.state, rec.revision, rec.kill_seq, rec.peak_net_liquidation) == ("ARMED", 1, 0, 100_000.0)
    assert store.transitions(rec.experiment_id)[0]["to_state"] == "ARMED"


def test_plan5_a1_fields_exist_with_their_types(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    for name, kind in (("experiment_id", str), ("account_id", str), ("started_at", dt.datetime),
                       ("start_net_liquidation", float), ("base_currency", str),
                       ("start_usd_per_base", float), ("state", str)):
        assert type(getattr(rec, name)) is kind
    assert rec.killed_at is None


def test_second_active_experiment_is_refused(store):
    store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ExperimentRefused) as exc:
        store.insert_armed(armed_record(start_command_id="start-2"), principal="cli", reason="again")
    assert exc.value.code == "EXPERIMENT_ACTIVE"


def test_same_start_command_returns_its_own_row(store):
    first = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.insert_armed(armed_record(), principal="cli", reason="go") == first
    assert len(store.transitions(first.experiment_id)) == 1


def test_concurrent_starts_create_one_experiment(db):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: _try_insert(db, f"start-{i}"), range(8)))
    assert sum(1 for r in results if r == "ok") == 1
    assert set(results) == {"ok", "EXPERIMENT_ACTIVE"}
    assert db.execute("SELECT count(*) FROM experiment_active", fetch="one")[0] == 1


@pytest.mark.parametrize("frm,to", [("ARMED", "PAUSED"), ("PAUSED", "ARMED"), ("ARMED", "KILLED"),
                                    ("PAUSED", "KILLED"), ("KILLED", "STOPPED"), ("ARMED", "STOPPED"),
                                    ("PAUSED", "STOPPED")])
def test_allowed_transitions(store, frm, to):
    rec = _drive_to(store, frm)
    after = store.transition(rec.experiment_id, expected=frozenset({frm}), to=to, principal="cli",
                             command_id="c", reason="x")
    assert (after.state, after.revision) == (to, rec.revision + 1)
    assert store.transitions(rec.experiment_id)[-1]["from_state"] == frm


@pytest.mark.parametrize("frm,to", [("KILLED", "PAUSED"), ("KILLED", "ARMED"), ("STOPPED", "ARMED"),
                                    ("STOPPED", "KILLED"), ("ARMED", "ARMED")])
def test_illegal_transitions_are_refused(store, frm, to):
    rec = _drive_to(store, frm)
    with pytest.raises(ExperimentRefused) as exc:
        store.transition(rec.experiment_id, expected=frozenset({frm}), to=to, principal="cli",
                         command_id="c", reason="x")
    assert exc.value.code == "ILLEGAL_TRANSITION"
    assert store.get(rec.experiment_id).state == frm


def test_transition_is_compare_and_set(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                     principal="cli", command_id="p1", reason="x")
    with pytest.raises(ExperimentRefused, match="EXPERIMENT_STATE_CHANGED"):
        store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="KILLED",
                         principal="kill_monitor", command_id=None, reason="hit")


def test_concurrent_transitions_one_wins(db, store):                     # Review Focus 3
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                     principal="cli", command_id="p", reason="p")

    def resume():
        return _try(lambda: ExperimentStore(db, ACCOUNT, lambda: NOW).transition(
            rec.experiment_id, expected=frozenset({"PAUSED"}), to="ARMED", principal="cli",
            command_id="r", reason="r"))

    def kill_again():
        return _try(lambda: ExperimentStore(db, ACCOUNT, lambda: NOW).transition(
            rec.experiment_id, expected=frozenset({"PAUSED"}), to="KILLED", principal="kill_monitor",
            command_id=None, reason="hit"))
    with ThreadPoolExecutor(2) as pool:
        outcomes = [f.result() for f in (pool.submit(resume), pool.submit(kill_again))]
    assert sorted(outcomes) == ["EXPERIMENT_STATE_CHANGED", "ok"]


def test_stop_releases_the_active_row_and_is_final(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    stopped = store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="STOPPED",
                               principal="cli", command_id="s", reason="done")
    assert store.active() is None and store.latest() == stopped and stopped.stopped_at == NOW
    second = store.insert_armed(armed_record(start_command_id="start-2"), principal="cli", reason="new run")
    assert store.latest() == second and store.active() == second


def test_state_survives_a_restart(db, store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="KILLED",
                     principal="kill_monitor", command_id=None, reason="hit",
                     changes={"killed_at": NOW, "kill_seq": 1})
    assert ExperimentStore(db, ACCOUNT, lambda: NOW).active().state == "KILLED"


def test_other_account_sees_nothing(db, store):
    store.insert_armed(armed_record(), principal="cli", reason="go")
    other = ExperimentStore(db, "DU2", lambda: NOW)
    assert other.active() is None and other.latest() is None


def test_peak_only_rises(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.raise_peak(rec.experiment_id, 101_000.0) == 101_000.0
    assert store.raise_peak(rec.experiment_id, 99_000.0) == 101_000.0
    assert store.get(rec.experiment_id).peak_net_liquidation == 101_000.0


@pytest.mark.parametrize("bad", [math.nan, 0.0, -1.0, True, "1e5"])
def test_peak_rejects_bad_values(store, bad):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ValueError):
        store.raise_peak(rec.experiment_id, bad)


def test_kill_progress_cannot_touch_state_or_start_fields(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="ARMED", changes={"state": "STOPPED"})
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="ARMED",
                                   changes={"start_net_liquidation": 1.0})
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="ARMED",
                                   changes={"kill_anchor_net_liquidation": 1.0})


def test_kill_progress_needs_the_expected_state(store):
    rec = _drive_to(store, "KILLED")
    updated = store.update_kill_progress(rec.experiment_id, expected_state="KILLED",
                                         changes={"kill_flat_state": "PENDING"})
    assert updated.kill_flat_state == "PENDING" and updated.revision == rec.revision
    with pytest.raises(ExperimentRefused, match="EXPERIMENT_STATE_CHANGED"):
        store.update_kill_progress(rec.experiment_id, expected_state="ARMED", changes={"kill_round": 1})


def test_kill_progress_values_are_validated(store):
    rec = _drive_to(store, "KILLED")
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="KILLED",
                                   changes={"kill_flat_state": "MAYBE"})
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="KILLED", changes={"kill_round": True})


@pytest.mark.parametrize("key", ["state", "start_net_liquidation", "started_at", "experiment_id", "revision"])
def test_transition_changes_use_the_same_allow_list(store, key):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ValueError):
        store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                         principal="cli", command_id="p", reason="x", changes={key: 1})
    assert store.get(rec.experiment_id).state == "ARMED"


@pytest.mark.parametrize("bad", [dict(start_net_liquidation=True), dict(start_net_liquidation=math.nan),
    dict(start_net_liquidation=0.0), dict(start_usd_per_base=-1.0), dict(kill_drawdown_pct=True),
    dict(kill_drawdown_pct=100.0), dict(kill_basis="high"), dict(base_currency="usd"),
    dict(start_generation_id=True), dict(styles=("swing_short",)), dict(styles=["intraday_long"]),
    dict(started_at=dt.datetime(2026, 7, 17)), dict(experiment_id="exp-1"), dict(revision=0)])
def test_record_constructor_is_strict(bad):
    with pytest.raises(ValueError):
        armed_record(**bad)


def test_record_coerces_an_integer_net_liquidation_to_float():
    assert type(armed_record(start_net_liquidation=100_000).start_net_liquidation) is float


def test_transitions_are_append_only_and_carry_the_detail(store):
    rec = _drive_to(store, "PAUSED")
    store.transition(rec.experiment_id, expected=frozenset({"PAUSED"}), to="KILLED", principal="kill_monitor",
                     command_id=None, reason="hit", changes={"kill_seq": 1, "killed_at": NOW})
    rows = store.transitions(rec.experiment_id)
    assert [r["revision"] for r in rows] == [1, 2, 3]
    assert rows[-1]["detail"]["kill_seq"] == 1 and rows[-1]["principal"] == "kill_monitor"


def test_replace_keeps_validation():
    with pytest.raises(ValueError):
        replace(armed_record(), state="DONE")
