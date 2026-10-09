import duckdb
import pytest

from trader.ai.research_schema import RESEARCH_MIGRATIONS
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore

TABLES = {"ai_research_cycles", "ai_research_candidates", "ai_backtest_judgments",
          "ai_research_registrations", "ai_research_cooldowns"}


def test_versions_are_30_to_34_and_appended_last():
    assert [m.version for m in RESEARCH_MIGRATIONS] == [30, 31, 32, 33, 34]
    assert ALL_MIGRATIONS[-5:] == RESEARCH_MIGRATIONS


def test_tables_exist_and_a_second_migrate_is_a_no_op(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    names = {row[0] for row in store.db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert TABLES <= names and store.migrate(ALL_MIGRATIONS) == []


def test_one_judgment_per_case_and_per_candidate(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    insert = ("INSERT INTO ai_backtest_judgments (judgment_id, candidate_id, case_digest, kind, menu_json, state, "
              "created_at, updated_at) VALUES (?, ?, ?, 'INITIAL', '[]', 'JUDGING', now(), now())")
    store.db.execute(insert, ["jdg-1", "rc-1", "sha256:" + "a" * 64])
    with pytest.raises(duckdb.ConstraintException):
        store.db.execute(insert, ["jdg-2", "rc-2", "sha256:" + "a" * 64])
    with pytest.raises(duckdb.ConstraintException):
        store.db.execute(insert, ["jdg-3", "rc-1", "sha256:" + "b" * 64])


def test_a_version_digest_is_registered_once(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    insert = ("INSERT INTO ai_research_registrations (judgment_id, kind, strategy_key, state, version_digest, "
              "next_try_at, created_at, updated_at) "
              "VALUES (?, 'INITIAL', 'k', 'REGISTERED', ?, now(), now(), now())")
    store.db.execute(insert, ["jdg-1", "sha256:" + "c" * 64])
    with pytest.raises(duckdb.ConstraintException):
        store.db.execute(insert, ["jdg-2", "sha256:" + "c" * 64])


def test_check_constraints_refuse_unknown_states(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    with pytest.raises(duckdb.ConstraintException):
        store.db.execute("INSERT INTO ai_research_cooldowns VALUES ('k', '2026-07-02', 'OTHER', now())")
