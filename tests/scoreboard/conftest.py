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
