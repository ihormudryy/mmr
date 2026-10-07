import datetime as dt

import pytest

from tests.scoreboard.common import EXP_ID, NOW
from trader.scoreboard.store import IngestRefused, ScoreboardConflict


def cost_row(**changes):
    row = {"record_id": "cost-0000001", "experiment_id": EXP_ID, "role": "jev", "provider": "openrouter",
           "model": "m1", "attempt_id": "att-0000001", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5,
           "cost_status": "confirmed", "called_at": NOW, "served_kind": "cycle", "served_id": "cycle-1",
           "decision_id": None, "corrects_record_id": None, "correction_seq": 0, "body_digest": "a" * 64,
           "recorded_at": NOW}
    row.update(changes)
    return row


def test_migrations_95_and_96_create_the_tables_and_drop_simulated_books(db, store):
    tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"simulated_decisions", "simulated_outcomes", "ai_costs"} <= tables
    assert "simulated_books" not in tables
    assert {95, 96} <= {r[0] for r in db.execute("SELECT version FROM schema_migrations", fetch="all")}


def test_ingest_inserts_once_and_seals_the_row(store):
    assert store.ingest_sealed_many([("ai_costs", cost_row())]) == "INSERTED"
    assert store.fetch("ai_costs", {})[0]["cost_usd"] == 0.5 and store.verify_seals() == []


def test_ingest_duplicate_ignores_server_time_columns(store):
    store.ingest_sealed_many([("ai_costs", cost_row())])
    later = NOW + dt.timedelta(hours=3)
    assert store.ingest_sealed_many([("ai_costs", cost_row(recorded_at=later))]) == "DUPLICATE"
    assert len(store.fetch("ai_costs", {})) == 1 and store.seal_count() == 1


def test_ingest_with_a_different_digest_is_a_conflict(store):
    store.ingest_sealed_many([("ai_costs", cost_row())])
    with pytest.raises(ScoreboardConflict):
        store.ingest_sealed_many([("ai_costs", cost_row(body_digest="b" * 64, cost_usd=9.0))])
    assert store.fetch("ai_costs", {})[0]["cost_usd"] == 0.5


def test_extend_runs_inside_the_transaction_and_its_refusal_writes_nothing(store):
    def refuse(conn):
        raise IngestRefused("NOPE", "no", retryable=True)
    with pytest.raises(IngestRefused) as exc:
        store.ingest_sealed_many([("ai_costs", cost_row())], extend=refuse)
    assert exc.value.retryable is True and store.fetch("ai_costs", {}) == [] and store.seal_count() == 0


def test_extend_columns_are_merged_into_the_primary_row(store):
    store.ingest_sealed_many([("ai_costs", cost_row(correction_seq=0))], extend=lambda conn: {"correction_seq": 4})
    assert store.fetch("ai_costs", {})[0]["correction_seq"] == 4


def test_a_status_without_a_cost_is_refused_by_the_table(store):
    with pytest.raises(Exception, match="(?i)constraint"):
        store.ingest_sealed_many([("ai_costs", cost_row(cost_status="unknown"))])
    with pytest.raises(Exception, match="(?i)constraint"):
        store.ingest_sealed_many([("ai_costs", cost_row(record_id="cost-0000002", cost_usd=None))])


def test_primary_and_extra_rows_are_written_together_or_not_at_all(store):
    outcome = {"record_id": "sim-0000001", "experiment_id": EXP_ID, "baseline_id": "no_trade.v1",
               "cohort": "self_found", "session_date": dt.date(2026, 10, 6), "status": "COMPLETE", "reason": None,
               "exit_kind": "NONE", "exit_at": None, "exit_price": None, "pnl_usd": 0.0, "trades": 0,
               "bar_source": "none", "bars_digest": None, "computed_at": NOW}
    def conflict(conn):
        raise ScoreboardConflict("x")
    with pytest.raises(ScoreboardConflict):
        store.ingest_sealed_many([("ai_costs", cost_row()), ("simulated_outcomes", outcome)], extend=conflict)
    assert store.fetch("simulated_outcomes", {}) == [] and store.seal_count() == 0
