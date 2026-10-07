"""SP2 Plan 5 Task 1: ai.duckdb migrations 10-17 and the cursor helpers."""
import datetime as dt

import pytest

from tests.ai.fakes import FakeClock
from trader.ai.runtime_schema import ALL_MIGRATIONS, RUNTIME_MIGRATIONS, read_cursor, set_cursor_in_tx
from trader.ai.store import AiStore

TABLES = {"ai_held_epochs", "ai_submissions", "ai_outbox", "ai_cursors", "ai_opportunities",
          "ai_coverage_gaps", "ai_call_contexts", "ai_cycles"}


def test_plan_5_owns_10_to_17():
    assert [m.version for m in RUNTIME_MIGRATIONS] == list(range(10, 18))


def test_migrations_apply_once_and_survive_reopen(tmp_path):
    clock = FakeClock(dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    assert [v for v in store.migrate(ALL_MIGRATIONS) if 10 <= v < 20] == list(range(10, 18))
    assert AiStore(tmp_path / "ai.duckdb", clock=clock).migrate(ALL_MIGRATIONS) == []
    names = {row[0] for row in store.db.execute(
        "SELECT table_name FROM information_schema.tables", fetch="all")}
    assert TABLES <= names


@pytest.mark.asyncio
async def test_cursor_starts_at_zero_and_moves_only_in_a_transaction(tmp_path):
    clock = FakeClock(dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    assert await read_cursor(store, "signals") == 0
    await store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 7, clock.now()))
    assert await read_cursor(store, "signals") == 7
    with pytest.raises(ValueError):
        await store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", True, clock.now()))
