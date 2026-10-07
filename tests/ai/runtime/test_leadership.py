"""SP2 Plan 5 Task 3: one trader-granted epoch per process (spec 5.1, Plan 1 Rulings 2-3)."""
import pytest
import pytest_asyncio

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import EpochTrader, et
from trader.ai.leadership import Leadership, NotLeader, new_holder_id
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore
from trader.automation.controller_epoch import HOLDER_ID


@pytest.fixture
def clock():
    return FakeClock(et(11, 0))


@pytest.fixture
def store(tmp_path, clock):
    created = AiStore(tmp_path / "ai.duckdb", clock=clock)
    created.migrate(ALL_MIGRATIONS)
    return created


@pytest_asyncio.fixture
async def trader(tmp_path, clock):
    return EpochTrader(tmp_path / "trader", clock)


def leader(trader, store, clock):
    return Leadership(supervisor=trader, store=store, clock=clock, holder_id=new_holder_id())


def held_rows(store):
    return store.db.execute("SELECT epoch, holder_id, lost_reason FROM ai_held_epochs ORDER BY epoch", fetch="all")


@pytest.mark.asyncio
async def test_the_first_grant_is_persisted_before_it_is_used(trader, store, clock):
    a = leader(trader, store, clock)
    assert a.current_epoch() is None
    assert await a.acquire() == 1 and a.current_epoch() == 1
    assert held_rows(store) == [(1, a.holder_id, None)]


@pytest.mark.asyncio
async def test_renewal_keeps_the_epoch_and_moves_the_local_deadline(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    clock.advance(20)
    assert await a.grant_once() == 1
    clock.advance(50)
    assert a.current_epoch() == 1                      # renewed at +20: deadline is +75
    assert trader.calls[-1]["current_epoch"] == 1


@pytest.mark.asyncio
async def test_a_restart_waits_for_the_old_lease(trader, store, clock):          # Plan 1 Ruling 3
    a = leader(trader, store, clock)
    await a.acquire()
    b = leader(trader, store, clock)                   # the restarted process: a new holder
    started = clock.now()
    assert await b.acquire() == 2
    waited = (clock.now() - started).total_seconds()
    assert 60 <= waited <= 66
    with pytest.raises(NotLeader) as exc:
        await a.grant_once()
    assert exc.value.code == "CONTROLLER_EPOCH_HELD" and a.current_epoch() is None
    assert [(e, r) for e, _, r in held_rows(store)] == [(1, "CONTROLLER_EPOCH_HELD"), (2, None)]


@pytest.mark.asyncio
async def test_a_stale_reply_drops_the_epoch_at_once(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    await a.on_stale("CONTROLLER_EPOCH_STALE")
    assert a.current_epoch() is None
    assert held_rows(store)[0][2] == "CONTROLLER_EPOCH_STALE"


@pytest.mark.asyncio
async def test_without_renewal_the_epoch_ends_at_the_local_deadline(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    trader.down = True
    clock.advance(54.9)
    assert a.current_epoch() == 1
    clock.advance(0.1)                                 # 60 s lease minus the 5 s safety margin
    assert a.current_epoch() is None


@pytest.mark.asyncio
async def test_regaining_after_expiry_takes_the_next_epoch(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    clock.advance(61)
    assert await a.grant_once() == 2                   # Plan 1: an expired holder renewing gets a new epoch
    assert [(e, r) for e, _, r in held_rows(store)] == [(1, "SUPERSEDED"), (2, None)]


@pytest.mark.asyncio
async def test_an_epoch_the_trader_never_granted_is_dropped_and_asked_fresh(tmp_path, store, clock):
    a = Leadership(supervisor=EpochTrader(tmp_path / "t1", clock), store=store, clock=clock,
                   holder_id=new_holder_id())
    await a.acquire()
    a._supervisor = EpochTrader(tmp_path / "t2", clock)        # the trader's journal was reset
    with pytest.raises(NotLeader) as exc:
        await a.grant_once()
    assert exc.value.code == "CONTROLLER_EPOCH_UNKNOWN" and a.last_epoch is None
    assert await a.grant_once() == 1


@pytest.mark.asyncio
async def test_acquire_stops_when_asked(trader, store, clock):
    import asyncio
    a = leader(trader, store, clock)
    await a.acquire()
    stop = asyncio.Event()
    stop.set()
    assert await leader(trader, store, clock).acquire(stop) is None


def test_holder_ids_are_fresh_per_process():
    first, second = new_holder_id(), new_holder_id()
    assert first != second and HOLDER_ID.fullmatch(first) and first.startswith("ai-")
