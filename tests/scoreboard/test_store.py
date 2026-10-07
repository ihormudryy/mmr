import datetime as dt

import pytest

from tests.scoreboard.conftest import EXP_ID, equity_row
from trader.scoreboard.ports import session_date_et
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.seal import row_digest
from trader.scoreboard.store import ScoreboardConflict

D6, D7, D8 = dt.date(2026, 10, 6), dt.date(2026, 10, 7), dt.date(2026, 10, 8)


def _seals(store):
    return store.fetch("scoreboard_seals", {})


def _three_days(store):
    for day in (D6, D7, D8):
        store.insert_sealed("equity_daily", equity_row(session_date=day))


def test_migrations_are_idempotent_and_in_range(migrator):
    assert apply_scoreboard_migrations(migrator) is True
    assert apply_scoreboard_migrations(migrator) is False
    assert {v for v in migrator.applied_versions() if v >= 60} == {60, 61, 62, 63, 64}


def test_sealed_insert_roundtrips_nulls_as_null(store):
    store.insert_sealed("equity_daily", equity_row(end_nlv_usd=None))
    assert store.fetch("equity_daily", {"experiment_id": EXP_ID})[0]["end_nlv_usd"] is None


def test_duplicate_key_raises_conflict_and_leaves_one_seal(store):
    store.insert_sealed("equity_daily", equity_row())
    with pytest.raises(ScoreboardConflict):
        store.insert_sealed("equity_daily", equity_row(end_nlv_usd=1.0))
    assert len(_seals(store)) == 1
    assert store.fetch("equity_daily", {})[0]["end_nlv_usd"] == 100_250.0


def test_unknown_table_or_column_is_refused(store):
    with pytest.raises(ValueError):
        store.insert_sealed("round_trips", {})
    with pytest.raises(ValueError):
        store.insert_sealed("equity_daily", equity_row(nope=1))


@pytest.mark.parametrize("bad", [{"end_nlv_usd": float("nan")}, {"end_nlv_usd": "1"},
                                 {"ended_at": dt.datetime(2026, 10, 6, 21, 0)}, {"trade_count": 1.5}])
def test_bad_values_are_refused_never_coerced_to_zero(store, bad):
    with pytest.raises(ValueError):
        store.insert_sealed("equity_daily", equity_row(**bad))
    assert store.fetch("equity_daily", {}) == []


def test_digest_survives_a_database_roundtrip(store):
    row = equity_row(start_nlv_base=100_000)        # an int for a DOUBLE column
    store.insert_sealed("equity_daily", row)
    stored = store.fetch("equity_daily", {"experiment_id": EXP_ID})[0]
    assert row_digest(stored) == row_digest(store.prepare("equity_daily", row))


def test_intact_chain_verifies_clean(store):
    _three_days(store)
    assert store.verify_seals() == []


@pytest.mark.parametrize("sql,check", [
    ("UPDATE equity_daily SET end_nlv_usd = 1 WHERE session_date = DATE '2026-10-07'", "ROW_EDITED"),
    ("DELETE FROM equity_daily WHERE session_date = DATE '2026-10-07'", "ROW_MISSING"),
    ("DELETE FROM scoreboard_seals WHERE seal_id = 2", "CHAIN_BROKEN"),
    ("UPDATE scoreboard_seals SET row_digest = 'x' WHERE seal_id = 2", "CHAIN_BROKEN"),
])
def test_tampering_is_detected(store, db, sql, check):
    _three_days(store)
    db.execute(sql)
    assert check in {m["check"] for m in store.verify_seals()}


def test_row_without_a_seal_is_detected(store, db):
    db.execute("INSERT INTO ai_costs VALUES ('c1', NULL, 'p', 'm', 1, 1, 0.1, now(), 'job', 'j1')")
    assert "ROW_UNSEALED" in {m["check"] for m in store.verify_seals()}


def test_a_failed_insert_leaves_no_gap_in_the_seal_numbers(store):
    store.insert_sealed("equity_daily", equity_row(session_date=D6))
    with pytest.raises(ScoreboardConflict):
        store.insert_sealed("equity_daily", equity_row(session_date=D6))
    store.insert_sealed("equity_daily", equity_row(session_date=D7))
    assert [s["seal_id"] for s in _seals(store)] == [1, 2] and store.verify_seals() == []


def test_incident_is_idempotent_per_kind_and_key(store):
    assert store.record_incident("FX_EVIDENCE_MISSING", "e1:2026-10-06", "no rate") is True
    assert store.record_incident("FX_EVIDENCE_MISSING", "e1:2026-10-06", "again") is False
    assert len(store.incidents()) == 1


def test_session_date_et_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        session_date_et(dt.datetime(2026, 10, 6, 12, 0))
