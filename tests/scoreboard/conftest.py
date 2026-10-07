import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.store import ScoreboardStore

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
EXP_ID = "exp-0123456789abcdef0123"   # the real ExperimentRecord shape: exp-<20 hex>
ACCOUNT = "DU1234567"


def equity_row(**changes):
    row = {
        "experiment_id": EXP_ID, "session_date": dt.date(2026, 10, 6), "account_id": ACCOUNT,
        "session_end_state": "FLAT", "base_currency": "USD", "start_nlv_base": 100_000.0,
        "end_nlv_base": 100_250.0, "fx_usd_per_base": 1.0, "fx_source": "base_is_usd", "fx_as_of": NOW,
        "start_nlv_usd": 100_000.0, "end_nlv_usd": 100_250.0, "start_source": "experiment_start",
        "missing_sessions_before": 0, "realized_pnl_usd": 10.0, "commissions_usd": 2.0,
        "commission_json": '{"e1": 1.0, "e2": 1.0}', "peak_gross_exposure_usd": 1000.0,
        "trade_count": 1, "fill_count": 2, "open_positions": 0, "fills_digest": "d" * 64,
        "ended_at": NOW, "written_at": NOW,
    }
    row.update(changes)
    return row


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
