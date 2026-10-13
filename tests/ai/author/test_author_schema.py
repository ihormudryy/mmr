import duckdb
import pytest

from trader.ai.author_schema import AUTHOR_MIGRATIONS
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore

INSERT = ("INSERT INTO author_attempts (attempt_id, session_date, slot_start, state, started_at) "
          "VALUES (?, ?, now(), ?, now())")


def test_version_40_is_appended_last_and_41_to_44_stay_free():
    assert [m.version for m in AUTHOR_MIGRATIONS] == [40]
    assert ALL_MIGRATIONS[-1:] == AUTHOR_MIGRATIONS
    assert not {41, 42, 43, 44} & {m.version for m in ALL_MIGRATIONS}


def test_one_attempt_per_session_and_known_states_only(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    store.db.execute(INSERT, ["aut-20261008", "2026-10-08", "RUNNING"])
    with pytest.raises(duckdb.ConstraintException):
        store.db.execute(INSERT, ["aut-other", "2026-10-08", "RUNNING"])
    with pytest.raises(duckdb.ConstraintException):
        store.db.execute(INSERT, ["aut-20261009", "2026-10-09", "DONE"])
    assert store.migrate(ALL_MIGRATIONS) == []


def test_an_existing_database_at_version_34_gains_only_the_new_table(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    older = tuple(m for m in ALL_MIGRATIONS if m.version <= 34)
    store.migrate(older)
    store.db.execute("INSERT INTO ai_research_cycles (cycle_id, session_date, slot_start, state, started_at) "
                     "VALUES ('rcy-20261008', '2026-10-08', now(), 'DONE', now())")
    assert store.migrate(ALL_MIGRATIONS) == [40]
    assert store.db.execute("SELECT cycle_id FROM ai_research_cycles", fetch="all") == [("rcy-20261008",)]
