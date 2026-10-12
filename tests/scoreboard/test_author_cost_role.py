"""Trader journal migration 132: ai_costs.role accepts author; existing rows and their seals survive the rebuild."""
import pytest

from tests.scoreboard.common import NOW
from tests.scoreboard.ingest_world import FakeDecisions, cost, enter_fact, make_ingest
from trader.scoreboard.schema import _COSTS_AUTHOR_ROLE, MIGRATIONS, apply_scoreboard_migrations
from trader.scoreboard.store import ScoreboardStore


ROWS = "SELECT * FROM ai_costs ORDER BY record_id"


def journal_before_sp3(db, migrator):
    for version, name, statements in MIGRATIONS:               # the journal as the SP2 soak left it
        if version < 132:
            migrator.apply(version, name, statements)
    store = ScoreboardStore(db, now=lambda: NOW)
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact()))
    assert cost(ingest)["status"] == "INSERTED"
    assert cost(ingest, record_id="cost-jev-2", attempt_id="dec-2-jev-1-1", role="jev")["status"] == "INSERTED"
    return store, ingest


def test_migration_132_keeps_every_cost_row_and_seal_and_accepts_author(db, migrator):
    store, ingest = journal_before_sp3(db, migrator)
    before = db.execute(ROWS, fetch="all")
    assert len(before) == 2
    assert apply_scoreboard_migrations(migrator) is True      # 132 runs now
    assert db.execute(ROWS, fetch="all") == before            # identical values, every column
    assert store.verify_seals() == []
    assert cost(ingest, record_id="cost-author-1", attempt_id="aut-20261008-author-1-1",
                role="author")["status"] == "INSERTED"
    assert store.verify_seals() == []


def test_a_rebuild_that_fails_halfway_rolls_back_and_keeps_the_rows(db, migrator):
    store, _ingest = journal_before_sp3(db, migrator)
    before = db.execute(ROWS, fetch="all")
    broken = (*_COSTS_AUTHOR_ROLE[:3], "SELECT * FROM no_such_table")      # fails after the DROP
    with pytest.raises(Exception):
        migrator.apply(132, "sp3_ai_costs_author_role", broken)
    assert db.execute(ROWS, fetch="all") == before            # one transaction: the DROP rolled back too
    assert 132 not in set(migrator.applied_versions()) and store.verify_seals() == []
    assert apply_scoreboard_migrations(migrator) is True      # the real migration still runs afterwards


def test_an_unknown_role_is_still_refused_by_the_table(db, migrator):
    apply_scoreboard_migrations(migrator)
    with pytest.raises(Exception, match="(?i)check|constraint"):
        db.execute("INSERT INTO ai_costs (record_id, experiment_id, role, provider, model, attempt_id, cost_status, "
                   "called_at, served_kind, served_id, correction_seq, body_digest, recorded_at) "
                   "VALUES ('x', 'e', 'bogus', 'p', 'm', 'a', 'unknown', now(), 'research', 's', 0, 'd', now())")
