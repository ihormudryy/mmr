import pytest

from tests.scoreboard.common import NOW
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.store import ScoreboardStore


@pytest.fixture
def db(tmp_path):
    return DuckDBConnection(str(tmp_path / "journal.duckdb"))


@pytest.fixture
def migrator(db):
    return SchemaMigrator(db)


@pytest.fixture
def store(db, migrator):
    apply_scoreboard_migrations(migrator)
    return ScoreboardStore(db, now=lambda: NOW)


from tests.scoreboard.ledger_world import ledger, world  # noqa: E402,F401 - shared fixtures


@pytest.fixture
def scoreboard(world):  # noqa: F811 - the shared fixture
    from tests.scoreboard.ledger_world import CAL
    from trader.scoreboard.benchmark import BenchmarkBook
    from trader.scoreboard.service import ScoreboardService
    book = BenchmarkBook(world.store, lambda start, end: {}, CAL, now=lambda: world.clock[0])
    return ScoreboardService(store=world.store, db=world.db, experiments=world.experiments, ledger=world.ledger(),
                             book=book, links=world.links, calendar=CAL, now=lambda: world.clock[0])
