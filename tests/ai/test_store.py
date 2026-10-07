import asyncio
import threading
import time
from datetime import datetime, timezone
import pytest
from trader.ai.schema import FOUNDATION_MIGRATIONS, Migration
from trader.ai.store import AiStore, to_utc


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialized_across_store_instances(tmp_path, clock):
    first = AiStore(tmp_path / "w.duckdb", clock=clock)
    second = AiStore(tmp_path / "w.duckdb", clock=clock)
    first.migrate([Migration(1, "t", ("CREATE TABLE counter (n INTEGER)", "INSERT INTO counter VALUES (0)"))])

    def increment(conn):
        value = conn.execute("SELECT n FROM counter").fetchone()[0]
        time.sleep(0.002)
        conn.execute("UPDATE counter SET n = ?", [value + 1])

    await asyncio.gather(*[(first if i % 2 else second).atransaction(increment) for i in range(30)])
    assert (await first.aquery("SELECT n FROM counter", fetch="one"))[0] == 30


@pytest.mark.parametrize("zone", ["America/New_York", "Europe/Berlin", "UTC"])
def test_stored_instants_compare_the_same_under_any_process_time_zone(store, monkeypatch, zone):
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        instant = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        store.db.execute(
            "INSERT INTO ai_cost_events (event_id, attempt_key, role, backend, model, kind, cost_micros, occurred_at)"
            " VALUES ('e', 'a', 'r', 'b', 'm', 'k', 1, ?)", [instant])
        stored = store.db.execute("SELECT occurred_at FROM ai_cost_events", fetch="one")[0]
        assert to_utc(stored) == instant
    finally:
        monkeypatch.undo()
        time.tzset()


def test_plan_4_migrations_stay_inside_1_to_9():
    versions = [m.version for m in FOUNDATION_MIGRATIONS]
    assert versions == [1, 2, 3, 4, 5]
    assert all(1 <= v <= 9 for v in versions)


def test_migrate_is_idempotent_and_survives_reopen(tmp_path, clock):
    path = tmp_path / "m.duckdb"
    first = AiStore(path, clock=clock)
    assert first.migrate() == [1, 2, 3, 4, 5]
    assert first.migrate() == []
    reopened = AiStore(path, clock=clock)
    assert reopened.migrate() == []
    tables = {row[0] for row in reopened.db.execute("SHOW TABLES", fetch="all")}
    assert {"ai_model_attempts", "ai_budget_state", "ai_budget_cap_events", "ai_budget_reservations",
            "ai_cost_events", "ai_replay_evidence"} <= tables


def test_other_plans_add_their_own_range_and_bad_versions_are_refused(store):
    assert store.migrate([Migration(10, "plan5_t", ("CREATE TABLE plan5_t (n INTEGER)",))]) == [10]
    assert store.migrate([Migration(20, "plan6_t", ("CREATE TABLE plan6_t (n INTEGER)",))]) == [20]
    for bad in ([Migration(3, "b", ()), Migration(2, "b", ())],
                [Migration(4, "dup", ()), Migration(4, "dup", ())],
                [Migration(0, "zero", ())]):
        with pytest.raises(ValueError):
            store.migrate(bad)


@pytest.mark.asyncio
async def test_blocking_work_does_not_block_the_event_loop(store):
    loop_kept_running = threading.Event()

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.01)
        loop_kept_running.set()

    def slow(conn):
        # Only returns once the event loop has run the ticker. A blocked loop times out here.
        if not loop_kept_running.wait(10):
            raise AssertionError("the event loop was blocked by the transaction")

    await asyncio.gather(store.atransaction(slow), ticker())
    assert loop_kept_running.is_set()


def test_a_failed_transaction_rolls_back(store):
    store.transaction(lambda conn: conn.execute("CREATE TABLE t_roll (n INTEGER)"))

    def broken(conn):
        conn.execute("INSERT INTO t_roll VALUES (1)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.transaction(broken)
    assert store.db.execute("SELECT count(*) FROM t_roll", fetch="one")[0] == 0
