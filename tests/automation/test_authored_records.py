"""SP3 Plan 3 Task 4: tables 130-131 and the record states (spec 7.3; ruling 10)."""
from __future__ import annotations

import duckdb
import pytest

from tests.automation.authored_fixtures import TUNABLES, authored_journal, digest_of, request_id, source
from tests.automation.backtest_judge_fixtures import NOW
from trader.automation.authored_refusals import AuthoredRefused, AuthoredStateConflict
from trader.automation.authored_schema import apply_authored_migrations
from trader.automation.authored_strategies import (
    COUNTED_STATES, DEPLOYED, EVALUATING, EXPIRED, FAILED, REJECTED, RETIRED, SHADOW, STORED, TRANSITIONS,
    AuthoredState, counted_in_tx, insert_in_tx, move_in_tx, new_record, note_in_tx, record_in_tx, records_in_tx,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

DIGEST = digest_of(source())


def fresh(digest: str = DIGEST):
    return new_record(digest=digest, class_name="RangeBreak", bar_size="5 mins", tunables=TUNABLES,
                      note="range break", author_model="openrouter/author-1", now=NOW)


def test_migrations_130_and_131_apply_once(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "plain.duckdb"))
    migrator = SchemaMigrator(db)
    assert apply_authored_migrations(migrator) == [130, 131]
    assert apply_authored_migrations(migrator) == []
    tables = {row[0] for row in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"authored_strategies", "authored_isolation_proofs"} <= tables


def test_the_states_are_the_spec_table():
    assert set(AuthoredState) == {STORED, EVALUATING, SHADOW, DEPLOYED, REJECTED, RETIRED, EXPIRED, FAILED}
    assert COUNTED_STATES == {STORED, EVALUATING, SHADOW, DEPLOYED}
    assert set(TRANSITIONS) == set(COUNTED_STATES)                    # terminal states have no way out
    assert TRANSITIONS[STORED] == {EVALUATING}
    assert TRANSITIONS[EVALUATING] == {REJECTED, SHADOW, DEPLOYED, STORED, FAILED}
    assert TRANSITIONS[SHADOW] == {RETIRED, EXPIRED, DEPLOYED}         # ruling 10
    assert TRANSITIONS[DEPLOYED] == {RETIRED, EXPIRED}


def test_a_record_round_trips_and_moves_along_the_table(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)) == record
    moved = db.transaction(lambda conn: move_in_tx(
        conn, record, EVALUATING, event="CLAIMED", reason=None, now=NOW, request_id=request_id(1),
        evaluation_request_id=request_id(1)))
    assert (moved.state, moved.evaluation_request_id) == (EVALUATING, request_id(1))
    assert [entry["to"] for entry in moved.history] == ["STORED", "EVALUATING"]
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)) == moved
    assert db.transaction(counted_in_tx) == 1
    assert moved.key == f"authored:{DIGEST}:RangeBreak"


def test_an_illegal_move_is_refused_and_writes_nothing(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    with pytest.raises(AuthoredRefused) as refused:
        db.transaction(lambda conn: move_in_tx(conn, record, DEPLOYED, event="X", reason=None, now=NOW))
    assert refused.value.code == "AUTHORED_STATE_CONFLICT"
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)).state == STORED


def test_a_terminal_record_never_moves_and_is_not_counted(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    evaluating = db.transaction(lambda conn: move_in_tx(conn, record, EVALUATING, event="CLAIMED", reason=None,
                                                        now=NOW))
    rejected = db.transaction(lambda conn: move_in_tx(conn, evaluating, REJECTED, event="JUDGED",
                                                      reason="JEV_REJECT", now=NOW))
    with pytest.raises(AuthoredRefused):
        db.transaction(lambda conn: move_in_tx(conn, rejected, STORED, event="X", reason=None, now=NOW))
    assert db.transaction(counted_in_tx) == 0
    assert [r.digest for r in db.transaction(records_in_tx)] == [DIGEST]


def test_the_table_refuses_an_unknown_state(tmp_path):
    db = authored_journal(tmp_path)
    with pytest.raises(duckdb.ConstraintException):
        db.execute("INSERT INTO authored_strategies (digest, class_name, bar_size, tunables_json, note, author_model, "
                   "state, attempts, created_at, state_changed_at, history_json) "
                   "VALUES ('d', 'C', '5 mins', '{}', '', 'm', 'LIVE', 0, now(), now(), '[]')")


def test_a_move_from_a_stale_record_is_refused_and_keeps_the_newer_state(tmp_path):
    db = authored_journal(tmp_path)
    stale = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, stale))
    newer = db.transaction(lambda conn: move_in_tx(conn, stale, EVALUATING, event="CLAIMED", reason=None, now=NOW,
                                                   request_id=request_id(1), evaluation_request_id=request_id(1)))
    with pytest.raises(AuthoredStateConflict) as refused:
        db.transaction(lambda conn: move_in_tx(conn, stale, EVALUATING, event="CLAIMED", reason=None, now=NOW,
                                               request_id=request_id(2), evaluation_request_id=request_id(2)))
    assert refused.value.code == "AUTHORED_STATE_CONFLICT"
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)) == newer


def test_a_note_from_a_stale_record_is_refused_and_keeps_the_newer_history(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    evaluating = db.transaction(lambda conn: move_in_tx(conn, record, EVALUATING, event="CLAIMED", reason=None,
                                                        now=NOW, request_id=request_id(1)))
    noted = db.transaction(lambda conn: note_in_tx(conn, evaluating, event="STARTED", now=NOW,
                                                   request_id=request_id(1), image="a1b2c3d-20261011T220000Z"))
    with pytest.raises(AuthoredStateConflict):
        db.transaction(lambda conn: note_in_tx(conn, evaluating, event="STARTED", now=NOW,
                                               request_id=request_id(1), image="e4f5a6b-20261012T090000Z"))
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)) == noted


def test_a_move_of_a_record_that_is_not_stored_is_refused(tmp_path):
    db = authored_journal(tmp_path)
    with pytest.raises(AuthoredStateConflict):
        db.transaction(lambda conn: move_in_tx(conn, fresh(), EVALUATING, event="CLAIMED", reason=None, now=NOW))
    assert db.transaction(records_in_tx) == ()
