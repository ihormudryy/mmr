import asyncio
from datetime import datetime, timezone
import pytest
from tests.ai.fakes import FakeClock
from trader.ai.budget import (
    Budget, BudgetConflict, BudgetExhausted, BudgetNotInitialized, HourlyLimitReached, next_window_start, window_date,
)
from trader.ai.store import AiStore


UTC = timezone.utc
USD = 1_000_000


def make_budget(store, clock, calls_per_hour=120) -> Budget:
    return Budget(store, clock, calls_per_hour=calls_per_hour)


def reserve(budget, store, clock, rid, amount=100_000, role="orchestrator"):
    return store.transaction(lambda conn: budget.reserve_in_tx(
        conn, reservation_id=rid, role=role, backend="openrouter", model="m", worst_case_micros=amount, now=clock.now()))


def settle(budget, store, clock, rid, actual, late=False):
    return store.transaction(lambda conn: budget.settle_in_tx(
        conn, rid, actual_micros=actual, input_tokens=1, output_tokens=1, now=clock.now(), late=late))


def tx(store, fn):
    return store.transaction(fn)


@pytest.mark.asyncio
async def test_restart_never_applies_a_raise_early_or_moves_its_date(tmp_path, clock):
    path = tmp_path / "r.duckdb"
    first = AiStore(path, clock=clock)
    first.migrate()
    await make_budget(first, clock).set_cap(1 * USD)
    await make_budget(first, clock).set_cap(5 * USD)
    clock.advance(6 * 3600)
    restarted = AiStore(path, clock=clock)
    restarted.migrate()
    again = make_budget(restarted, clock)
    assert await again.set_cap(5 * USD) == "RAISE_KEPT"
    snapshot = await again.snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_window_date) == (1 * USD, "2026-07-02")


@pytest.mark.asyncio
async def test_restart_after_midnight_applies_the_pending_raise_before_comparing(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    await budget.set_cap(5 * USD)
    clock.advance(14 * 3600)  # 01:00 next day in New York
    assert await make_budget(store, clock).set_cap(5 * USD) == "UNCHANGED"
    assert (await budget.snapshot()).effective_cap_micros == 5 * USD


@pytest.mark.asyncio
async def test_concurrent_reservations_across_roles_cannot_exceed_the_cap(store, clock):
    budget = make_budget(store, clock, calls_per_hour=1000)
    await budget.set_cap(1 * USD)

    async def attempt(i):
        role, amount = ("orchestrator", 240_000) if i % 2 else ("jev", 80_000)
        try:
            await store.atransaction(lambda conn: budget.reserve_in_tx(
                conn, reservation_id=f"r{i}", role=role, backend="openrouter", model="m",
                worst_case_micros=amount, now=clock.now()))
            return amount
        except BudgetExhausted:
            return 0

    granted = await asyncio.gather(*[attempt(i) for i in range(60)])
    snapshot = await budget.snapshot()
    assert sum(granted) == snapshot.committed_micros <= 1 * USD
    assert snapshot.committed_micros > 900_000  # the cap was actually used


@pytest.mark.asyncio
async def test_hourly_limit_is_a_delay_not_a_block_until_reset(store, clock):
    budget = make_budget(store, clock, calls_per_hour=3)
    await budget.set_cap(100 * USD)
    for i, gap in enumerate((0, 600, 600)):
        clock.advance(gap)
        reserve(budget, store, clock, f"r{i}", 1000)
    with pytest.raises(HourlyLimitReached) as caught:
        reserve(budget, store, clock, "r3", 1000)
    assert caught.value.retry_after_seconds == pytest.approx(3600 - 1200)
    assert caught.value.retry_at < next_window_start(clock.now())
    clock.advance(caught.value.retry_after_seconds + 1)
    reserve(budget, store, clock, "r3", 1000)  # admitted after the oldest call leaves the hour


@pytest.mark.asyncio
async def test_a_call_spanning_midnight_stays_in_its_own_window(store):
    clock = FakeClock(datetime(2026, 7, 2, 3, 59, 50, tzinfo=UTC))  # 23:59:50 EDT on July 1
    store.clock = clock
    budget = make_budget(store, clock)
    await budget.set_cap(300_000)
    reserve(budget, store, clock, "r1", 240_000)
    clock.advance(20)  # 00:00:10 EDT on July 2
    assert window_date(clock.now()) == "2026-07-02"
    reserve(budget, store, clock, "r2", 240_000)  # the new window starts at zero
    settle(budget, store, clock, "r1", 50_000)
    row = store.db.execute("SELECT window_date, state, actual_micros FROM ai_budget_reservations "
                           "WHERE reservation_id = 'r1'", fetch="one")
    assert row == ("2026-07-01", "SETTLED", 50_000)
    assert (await budget.snapshot()).committed_micros == 240_000  # only r2 counts today
    assert store.db.execute("SELECT count(*) FROM ai_budget_reservations", fetch="one")[0] == 2


@pytest.mark.asyncio
async def test_a_25_hour_day_does_not_reset_twice(store):
    clock = FakeClock(datetime(2026, 11, 1, 4, 30, tzinfo=UTC))  # 00:30 EDT, Nov 1 (25-hour day)
    budget = make_budget(store, clock)
    await budget.set_cap(300_000)
    reserve(budget, store, clock, "r1", 240_000)
    clock.advance(60 * 60)  # 01:30 EDT
    clock.advance(60 * 60)  # 01:30 EST (the repeated hour)
    with pytest.raises(BudgetExhausted):
        reserve(budget, store, clock, "r2", 240_000)


@pytest.mark.asyncio
async def test_reserving_before_a_cap_exists_is_a_loud_error(store, clock):
    budget = make_budget(store, clock)
    with pytest.raises(BudgetNotInitialized):
        reserve(budget, store, clock, "r1")
    with pytest.raises(BudgetNotInitialized):
        await budget.snapshot()


@pytest.mark.asyncio
async def test_first_cap_is_applied_and_lowering_applies_at_once(store, clock):
    budget = make_budget(store, clock)
    assert await budget.set_cap(5 * USD) == "INITIALIZED"
    assert (await budget.snapshot()).effective_cap_micros == 5 * USD
    assert await budget.set_cap(2 * USD) == "LOWERED"
    snapshot = await budget.snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_cap_micros) == (2 * USD, None)
    with pytest.raises(ValueError):
        await budget.set_cap(-1)
    with pytest.raises(ValueError):
        await budget.set_cap(1.5)


@pytest.mark.asyncio
async def test_raising_waits_for_the_next_new_york_midnight(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    assert await budget.set_cap(5 * USD) == "RAISE_SCHEDULED"
    snapshot = await budget.snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_cap_micros, snapshot.pending_window_date) == (
        1 * USD, 5 * USD, "2026-07-02")
    clock.advance(13 * 3600 - 60)  # 23:59 New York
    assert (await budget.snapshot()).effective_cap_micros == 1 * USD
    clock.advance(60)  # 00:00 New York
    applied = await budget.snapshot()
    assert (applied.effective_cap_micros, applied.pending_cap_micros, applied.pending_window_date) == (
        5 * USD, None, None)


@pytest.mark.asyncio
async def test_reverting_the_config_cancels_a_pending_raise(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    await budget.set_cap(5 * USD)
    assert await budget.set_cap(1 * USD) == "UNCHANGED"
    snapshot = await budget.snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_cap_micros) == (1 * USD, None)
    clock.advance(24 * 3600)
    assert (await budget.snapshot()).effective_cap_micros == 1 * USD


@pytest.mark.asyncio
async def test_reservation_over_the_cap_is_refused_with_the_reset_time(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(100_000)
    reserve(budget, store, clock, "r1", 60_000)
    with pytest.raises(BudgetExhausted) as caught:
        reserve(budget, store, clock, "r2", 60_000)
    assert caught.value.retry_at == next_window_start(clock.now())
    assert (caught.value.committed_micros, caught.value.needed_micros, caught.value.cap_micros) == (60_000, 60_000, 100_000)
    assert (await budget.snapshot()).committed_micros == 60_000
    assert store.db.execute("SELECT count(*) FROM ai_budget_reservations", fetch="one")[0] == 1


@pytest.mark.asyncio
async def test_lowering_below_what_is_committed_refuses_new_work_and_keeps_old(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r1", 800_000)
    assert await budget.set_cap(500_000) == "LOWERED"
    assert (await budget.snapshot()).committed_micros == 800_000
    with pytest.raises(BudgetExhausted):
        reserve(budget, store, clock, "r2", 1)


@pytest.mark.asyncio
async def test_reservations_survive_a_restart(tmp_path, clock):
    path = tmp_path / "s.duckdb"
    first = AiStore(path, clock=clock)
    first.migrate()
    budget = make_budget(first, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, first, clock, "r1", 240_000)
    second = AiStore(path, clock=clock)
    second.migrate()
    snapshot = await make_budget(second, clock).snapshot()
    assert (snapshot.committed_micros, snapshot.open_reservations) == (240_000, 1)


@pytest.mark.asyncio
async def test_settle_replaces_the_reservation_with_actual_cost_once(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r1", 240_000)
    assert settle(budget, store, clock, "r1", 5_000) is True
    assert (await budget.snapshot()).committed_micros == 5_000
    assert settle(budget, store, clock, "r1", 5_000) is False
    with pytest.raises(BudgetConflict) as caught:
        settle(budget, store, clock, "r1", 6_000)
    assert caught.value.code == "SETTLEMENT_CONFLICT"
    with pytest.raises(BudgetConflict) as caught:
        settle(budget, store, clock, "missing", 1)
    assert caught.value.code == "RESERVATION_UNKNOWN"


@pytest.mark.asyncio
async def test_actual_cost_above_the_reservation_is_counted_not_hidden(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r1", 1_000)
    settle(budget, store, clock, "r1", 5_000)
    assert (await budget.snapshot()).committed_micros == 5_000


@pytest.mark.asyncio
async def test_release_returns_the_money_and_is_not_a_second_release(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r1", 240_000)
    tx(store, lambda conn: budget.release_in_tx(conn, "r1", reason="NOT_SENT", now=clock.now()))
    assert (await budget.snapshot()).committed_micros == 0
    with pytest.raises(BudgetConflict) as caught:
        tx(store, lambda conn: budget.release_in_tx(conn, "r1", reason="NOT_SENT", now=clock.now()))
    assert caught.value.code == "RESERVATION_STATE"
    with pytest.raises(BudgetConflict):
        settle(budget, store, clock, "r1", 10)
    with pytest.raises(BudgetConflict):
        tx(store, lambda conn: budget.mark_unknown_in_tx(conn, "r1", clock.now()))


@pytest.mark.asyncio
async def test_unknown_keeps_its_reservation_and_a_late_report_reconciles_once(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r1", 240_000)
    tx(store, lambda conn: budget.mark_unknown_in_tx(conn, "r1", clock.now()))
    snapshot = await budget.snapshot()
    assert (snapshot.committed_micros, snapshot.unknown_reservations, snapshot.open_reservations) == (240_000, 1, 0)
    assert settle(budget, store, clock, "r1", 6_000, late=True) is True
    assert (await budget.snapshot()).committed_micros == 6_000
    assert settle(budget, store, clock, "r1", 6_000, late=True) is False
    with pytest.raises(BudgetConflict) as caught:
        settle(budget, store, clock, "r1", 7_000, late=True)
    assert caught.value.code == "SETTLEMENT_CONFLICT"


@pytest.mark.asyncio
async def test_a_normal_settle_cannot_close_an_unknown_reservation(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r1", 240_000)
    reserve(budget, store, clock, "r2", 240_000)
    tx(store, lambda conn: budget.mark_unknown_in_tx(conn, "r1", clock.now()))
    with pytest.raises(BudgetConflict) as caught:
        settle(budget, store, clock, "r1", 10)
    assert caught.value.code == "RESERVATION_STATE"
    with pytest.raises(BudgetConflict):
        settle(budget, store, clock, "r2", 10, late=True)  # a late report needs an UNKNOWN reservation
    assert (await budget.snapshot()).committed_micros == 480_000


@pytest.mark.asyncio
async def test_recover_marks_open_reservations_unknown_and_counts_them(store, clock):
    budget = make_budget(store, clock)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "a", 100_000)
    reserve(budget, store, clock, "b", 100_000)
    settle(budget, store, clock, "b", 1_000)
    assert tx(store, lambda conn: budget.recover_open_in_tx(conn, clock.now())) == ["a"]
    snapshot = await budget.snapshot()
    assert (snapshot.open_reservations, snapshot.unknown_reservations, snapshot.committed_micros) == (0, 1, 101_000)
    assert tx(store, lambda conn: budget.recover_open_in_tx(conn, clock.now())) == []


@pytest.mark.asyncio
async def test_calls_proven_not_sent_do_not_use_the_hourly_allowance(store, clock):
    budget = make_budget(store, clock, calls_per_hour=2)
    await budget.set_cap(1 * USD)
    reserve(budget, store, clock, "r0", 1000)
    tx(store, lambda conn: budget.release_in_tx(conn, "r0", reason="NOT_SENT", now=clock.now()))
    reserve(budget, store, clock, "r1", 1000)
    reserve(budget, store, clock, "r2", 1000)  # r0 does not count
    tx(store, lambda conn: budget.release_in_tx(conn, "r2", reason="REJECTED", now=clock.now()))
    with pytest.raises(HourlyLimitReached):  # a rejected call still reached the provider
        reserve(budget, store, clock, "r3", 1000)


def test_windows_follow_new_york_local_dates_across_dst():
    assert window_date(datetime(2026, 11, 1, 3, 59, tzinfo=UTC)) == "2026-10-31"
    assert window_date(datetime(2026, 11, 1, 4, 0, tzinfo=UTC)) == "2026-11-01"
    assert window_date(datetime(2026, 11, 2, 4, 59, tzinfo=UTC)) == "2026-11-01"
    assert window_date(datetime(2026, 11, 2, 5, 0, tzinfo=UTC)) == "2026-11-02"
    fall_day = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert next_window_start(fall_day) == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert (next_window_start(fall_day) - datetime(2026, 11, 1, 4, 0, tzinfo=UTC)).total_seconds() == 25 * 3600
    spring_day = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
    assert next_window_start(spring_day) == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    assert (next_window_start(spring_day) - datetime(2026, 3, 8, 5, 0, tzinfo=UTC)).total_seconds() == 23 * 3600
    assert window_date(datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)) == "2026-07-01"
