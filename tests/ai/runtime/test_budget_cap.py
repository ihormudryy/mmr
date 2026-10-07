"""SP2 Plan 5 Task 9: the owner's cap comes from the trader (trader.yaml), never from ai.yaml (Ruling 19)."""
import pytest

from tests.ai.fakes import FakeClock, config_text, write_config
from tests.ai.runtime.fakes import et
from tests.ai.world import World, request
from trader.ai.budget import Budget
from trader.ai.budget_cap import BudgetCapSync, CapGatedGateway
from trader.ai.config import AiConfigError, load_ai_config
from trader.ai.gateway import CallRefused
from trader.ai.rpc_clients import RpcNotSent
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore


def reply(usd):
    return {"model_budget_usd_per_day": usd, "source": "trader.yaml"}


class CapTrader:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def set(self, answer):
        self.answer = answer

    async def call(self, method, body, *, epoch=None):
        assert method == "get_ai_model_budget"
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def migrated_store(tmp_path, clock):
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    return store


USD = 1_000_000


@pytest.mark.asyncio
async def test_the_cap_comes_from_the_trader_and_a_raise_waits_for_new_york_midnight(tmp_path):
    clock, trader = FakeClock(et(11, 0)), CapTrader(reply(1.0))
    store = migrated_store(tmp_path, clock)
    sync = BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock)
    assert await sync.sync() and sync.ready()
    trader.set(reply(5.0))                                         # the operator edited trader.yaml and restarted it
    assert await sync.sync()
    snapshot = await Budget(store, clock, calls_per_hour=120).snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_cap_micros) == (1 * USD, 5 * USD)
    clock.advance(13 * 3600 + 60)                                 # 00:01 New York, the next day
    assert not sync.ready()                                        # a new New York window needs a new read
    assert await sync.sync() and (await Budget(store, clock, calls_per_hour=120).snapshot()).effective_cap_micros == 5 * USD


@pytest.mark.asyncio
async def test_an_ai_restart_or_ai_config_change_cannot_raise_the_cap_early(tmp_path):
    clock, trader = FakeClock(et(11, 0)), CapTrader(reply(1.0))
    store = migrated_store(tmp_path, clock)
    await BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock).sync()
    trader.set(reply(5.0))
    await BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock).sync()
    # a new ai process on the same ai.duckdb, with an ai.yaml that tries to name a cap
    with pytest.raises(AiConfigError):
        load_ai_config(str(write_config(tmp_path, config_text(extra_top_level="model_budget_usd_per_day: 9999"))))
    restarted = BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock)
    assert await restarted.sync()
    assert (await Budget(store, clock, calls_per_hour=120).snapshot()).effective_cap_micros == 1 * USD


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [RpcNotSent("NO_ROUTE"), {"model_budget_usd_per_day": True, "source": "trader.yaml"},
                                 {"model_budget_usd_per_day": -1, "source": "trader.yaml"},
                                 {"model_budget_usd_per_day": float("nan"), "source": "trader.yaml"},
                                 {"model_budget_usd_per_day": 5, "source": "ai.yaml"}, {"x": 1}])
async def test_a_failed_cap_read_stops_model_calls_until_a_read_succeeds(tmp_path, bad):
    clock, trader = FakeClock(et(11, 0)), CapTrader(reply(2000.0))
    world = World(tmp_path, clock)                                 # Plan 4's gateway world
    await world.gateway.start()
    sync = BudgetCapSync(supervisor=trader, budget=world.gateway.budget, clock=clock)
    gated = CapGatedGateway(world.gateway, sync)
    assert await sync.sync()
    trader.set(bad)
    assert await sync.sync() is False and not sync.ready()
    with pytest.raises(CallRefused) as caught:
        await gated.call("jev", request("d/jev/1"), gated.new_deadline())
    assert caught.value.code == "BUDGET_CAP_UNKNOWN"
    assert world.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)] and world.jev.requests == []
    trader.set(reply(2000.0))
    assert await sync.sync() and (await gated.call("jev", request("d/jev/1"), gated.new_deadline())).response.text
