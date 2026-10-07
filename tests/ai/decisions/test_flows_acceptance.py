"""SP2 Plan 6 Task 8: spec 12 "Flows" with the real engine, real adapters over scripted providers, and SP1's
real coordinator, risk gates, ownership and safe close over signed RPC. Only the broker and providers are fakes."""
import dataclasses
import json

import pytest
import pytest_asyncio

from tests.ai.decisions.decision_world import DecisionNode, TraderMarket, decisions_block, register_discretionary
from tests.ai.runtime.trader_world import TraderWorld
from tests.sp1_fixtures import MSFT, LoopThread, et
from trader.ai.ids import derive_decision_id
from trader.ai.roles import CLOSE_MARKER, ENTRY_MARKER, JEV_MARKER
from trader.ai.schedule import ENTRY
from trader.automation.risk_limits import PAPER_LIMITS


def ruling(verdict="TAKE", quantity=None):
    return json.dumps({"verdict": verdict, "quantity": quantity, "reason": "ok"})


def picks(*refs):
    return json.dumps({"picks": [{"candidate": ref, "thesis": "momentum"} for ref in refs]})


def closes(*items):
    return json.dumps({"closes": [{"position": ref, "action": action, "quantity": quantity, "reason": "manage"}
                                  for ref, action, quantity in items]})


@pytest.fixture
def loop_thread():
    thread = LoopThread()
    yield thread
    thread.stop()


async def build(tmp_path, loop_thread, monkeypatch, *, extra="", market=None, **world_options):
    # The trader reads trading_filters.yaml from here, never from the developer's ~/.config/mmr.
    monkeypatch.setattr("trader.trading.trading_filter._default_path", lambda: tmp_path / "trading_filters.yaml")
    market = market or TraderMarket()
    world = TraderWorld(tmp_path, loop_thread, monkeypatch, prepare=market.prepare, **world_options)
    market.now = world.served.now
    try:
        digest = register_discretionary(world)
        node = DecisionNode(world, tmp_path, decisions_block(world, digest, extra))
        await node.start()
    except BaseException:
        world.close()
        raise
    return world, node, market


@pytest_asyncio.fixture
async def stack(tmp_path, loop_thread, monkeypatch):
    world, node, market = await build(tmp_path, loop_thread, monkeypatch)
    yield world, node, market
    assert market.scanned == []                         # discovery never used the IB scanner
    world.close()


@pytest_asyncio.fixture
async def slow_recheck(tmp_path, loop_thread, monkeypatch):
    """A role stays down for an hour: the 11:15 cycles still see Jev down."""
    world, node, market = await build(tmp_path, loop_thread, monkeypatch, extra="  role_recheck_seconds: 3600\n")
    yield world, node, market
    world.close()


async def enter_and_settle(world, node):
    """One strategy BUY taken by Jev, filled and protected; the scoreboard projects its trip."""
    node.jev.script(JEV_MARKER, ruling())
    source = world.strategy_signal()
    await node.signals()
    world.settle()
    world.served.stack.scoreboard.service.refresh()
    return source


async def settle_close(world, node, decision_id, max_steps=30):
    """The broker fills the close one promoted second at a time; the controller reconciles its receipt."""
    for _ in range(max_steps):
        world.advance(1)
        await node.controller.reconcile_once()
        row = await node.node.submitter.get(decision_id)
        if row.state == "FINAL" and row.close_root_id is not None:
            return row
    raise AssertionError(f"close {decision_id} never settled: {await node.node.submitter.get(decision_id)}")


async def next_slot(world, node, minutes=15):
    """Move trader time to the next slot start (inside its grace) and run the due slots."""
    world.advance(minutes * 60)
    await node.slots()


@pytest.mark.asyncio
async def test_an_entry_signal_is_judged_once_even_when_redelivered(stack):
    world, node, _ = stack
    node.jev.script(JEV_MARKER, ruling())
    source = world.strategy_signal()
    await node.signals()
    world.strategy_signal()                                                     # same bar: the same source_event_id
    await node.signals()
    decision_id = derive_decision_id(source, f"enter:{world.conid}")
    assert len(node.jev.requests) == 1 and node.opportunity(source)[0] == "DECIDED"
    assert (await node.node.submitter.get(decision_id)).state in ("ACCEPTED", "FINAL")
    world.settle()
    assert len(world.entries()) == 1 and world.protected()


@pytest.mark.asyncio
async def test_follow_signal_baseline_has_the_real_enter_size_on_a_binding_limit(stack):  # Plan 2 Ruling 19
    world, node, _ = stack
    world.hold_other_position(value_share_of_equity=0.045)    # another conid: the 6 % gross limit binds first
    # A REDUCE leaves gross room for the skipped MSFT signal; the trader sizes a baseline only once the
    # session has effective limits, i.e. after its first admitted ENTER (Plan 2: NO_EFFECTIVE_LIMITS before).
    node.jev.script(JEV_MARKER, ruling("REDUCE", 2), ruling("SKIP"))
    taken = world.strategy_signal()
    await node.signals()
    world.settle()
    await node.report()                                        # the outbox delivers the linked follow baseline
    (entry,) = world.entries()
    row = world.served.trader.journal_db.execute(
        "SELECT quantity, quantity_source FROM simulated_decisions WHERE opportunity_id = ?", [taken], fetch="one")
    assert row == (int(entry[3]), "linked_entry") and entry[3] == 2.0   # exactly the size SP1 gave the real ENTER
    skipped = world.strategy_signal(conid=MSFT)                  # Jev skips: the trader sizes it at ingestion
    await node.signals()
    await node.report()
    row = world.served.trader.journal_db.execute(
        "SELECT quantity, quantity_source FROM simulated_decisions WHERE opportunity_id = ?", [skipped], fetch="one")
    assert row == (2, "trader_sizing")              # (6000 - 4500 - 2 x 229.9) / 500: gross binds, not the notional's 4


@pytest.mark.asyncio
async def test_an_exit_signal_bypasses_jev_and_uses_the_safe_close(stack):
    world, node, _ = stack
    await enter_and_settle(world, node)
    sell = world.strategy_signal(action="SELL")
    await node.signals()
    close_id = derive_decision_id(sell, f"close:{world.conid}")
    assert len(node.jev.requests) == 1 and node.opportunity(sell) == ("DECIDED", "EXIT_SIGNAL")
    await settle_close(world, node, close_id)
    assert (await node.node.submitter.get(close_id)).close_root_id is not None
    assert not world.served.sim.held.get(world.conid) and not world.protected()


@pytest.mark.asyncio
async def test_a_multi_action_cycle_submits_one_decision_per_taken_pick(stack):
    world, node, _ = stack
    node.orchestrator.script(ENTRY_MARKER, picks("C1", "C2"))
    node.jev.script(JEV_MARKER, ruling(), ruling("SKIP"))
    await node.slots()
    cycle_id = node.controller_slot(ENTRY).cycle_id
    assert node.cycle(cycle_id)[0] == "DONE"
    taken = derive_decision_id(cycle_id, f"enter:{MSFT}")
    assert (await node.node.submitter.get(taken)).state in ("ACCEPTED", "FINAL")
    assert await node.node.submitter.get(derive_decision_id(cycle_id, f"enter:{world.conid}")) is None


@pytest.mark.asyncio
async def test_a_reduce_that_would_come_out_larger_is_refused(stack):                     # review focus 1
    world, node, _ = stack
    node.jev.script(JEV_MARKER, ruling("REDUCE", 1_000_000))
    source = world.strategy_signal()
    await node.signals()
    assert node.opportunity(source) == ("DECIDED", "JEV_REDUCE_NOT_SMALLER")
    assert world.receipt(derive_decision_id(source, f"enter:{world.conid}")) is None


@pytest.mark.asyncio
async def test_policy_republished_during_jev_is_refused_by_the_trader(stack):
    world, node, _ = stack

    def republish(_request):
        tighter = dataclasses.replace(PAPER_LIMITS, position_fraction=0.04).to_json()
        world.served.call("cli", "publish_ai_risk_policy", {"command_id": "cli-pol-000000000002",
                                                            "limits": tighter, "reason": "tighten"})
        return ruling()
    node.jev.script(JEV_MARKER, republish)
    source = world.strategy_signal()
    await node.signals()
    decision_id = derive_decision_id(source, f"enter:{world.conid}")
    assert world.receipt(decision_id).error_code == "POLICY_REVISION_STALE"


def submissions(node):
    return node.node.store.db.execute("SELECT decision_id, action, state, error_code FROM ai_submissions",
                                      fetch="all")


def working_legs(world, conid):
    """The working protective orders and their prices, from the broker."""
    legs = {}
    for entity, row in world.served.sim.orders.items():
        trade = world.served.sim.ib_trades.get(entity)
        if row.conid == conid and row.leg in ("stop", "take_profit") and row.status in ("Submitted", "PreSubmitted"):
            price = trade.order.auxPrice if row.leg == "stop" else trade.order.lmtPrice
            legs[row.leg] = (float(row.total_quantity) - float(row.filled_quantity), round(float(price), 2))
    return legs


@pytest.mark.asyncio
async def test_jev_failing_blocks_every_enter_while_closes_work(slow_recheck):            # review focus 3
    world, node, _ = slow_recheck
    await enter_and_settle(world, node)
    node.jev.script(JEV_MARKER, 404)
    second = world.strategy_signal(conid=MSFT)
    await node.signals()
    assert node.opportunity(second) == ("DECIDED", "MODEL_FAILED_REJECTED") and len(node.jev.requests) == 2
    world.advance(1)
    third = world.strategy_signal(conid=MSFT)
    await node.signals()
    assert node.opportunity(third) == ("DECIDED", "JEV_UNHEALTHY") and len(node.jev.requests) == 2
    node.orchestrator.script(CLOSE_MARKER, closes(("P1", "PARTIAL_CLOSE", 1)))
    await next_slot(world, node)                                   # 11:15: the position and the entry cycle
    cycle_id = node.controller_slot(ENTRY).cycle_id
    state, reason = node.cycle(cycle_id)
    assert state == "DONE" and "JEV_UNHEALTHY" in reason
    assert not any(ENTRY_MARKER in r.content.decode() for r in node.orchestrator.requests)
    queued = {json.loads(body)["baseline_id"] for _, _, body in node.outbox()
              if json.loads(body)["opportunity_id"] == cycle_id}
    assert queued == {"fixed_rule.v1", "no_trade.v1"}
    position_cycle = node.controller_slot("position").cycle_id
    partial = await node.node.submitter.get(derive_decision_id(position_cycle, f"partial_close:{world.conid}"))
    assert partial is not None and partial.state in ("ACCEPTED", "FINAL")
    await settle_close(world, node, partial.decision_id)
    sell = world.strategy_signal(action="SELL")
    await node.signals()
    close = await node.node.submitter.get(derive_decision_id(sell, f"close:{world.conid}"))
    assert node.opportunity(sell) == ("DECIDED", "EXIT_SIGNAL") and close.state in ("ACCEPTED", "FINAL")
    await settle_close(world, node, close.decision_id)
    assert not world.served.sim.held.get(world.conid) and len(world.entries()) == 1


@pytest.mark.asyncio
async def test_the_after_cutoff_close_is_driven_by_the_position_cycle(stack):
    world, node, _ = stack
    await enter_and_settle(world, node)
    world.advance((et(15, 30) - world.served.now()).total_seconds())
    node.orchestrator.script(CLOSE_MARKER, closes(("P1", "CLOSE", None)))
    await node.slots()                                             # after the entry cutoff: the position slot only
    assert node.cycle(node.controller_slot(ENTRY).cycle_id)[0] == "MISSED"
    position_cycle = node.controller_slot("position").cycle_id
    assert node.cycle(position_cycle) == ("DONE", "CLOSES")
    await settle_close(world, node, derive_decision_id(position_cycle, f"close:{world.conid}"))
    assert not world.served.sim.held.get(world.conid) and not world.protected()
    assert working_legs(world, world.conid) == {}


@pytest.mark.asyncio
async def test_a_partial_close_keeps_protection_at_the_original_prices(stack):
    world, node, _ = stack
    await enter_and_settle(world, node)
    held = world.served.sim.held[world.conid]
    node.orchestrator.script(CLOSE_MARKER, closes(("P1", "PARTIAL_CLOSE", 1)))
    node.orchestrator.script(ENTRY_MARKER, picks())
    await next_slot(world, node)
    position_cycle = node.controller_slot("position").cycle_id
    await settle_close(world, node, derive_decision_id(position_cycle, f"partial_close:{world.conid}"))
    assert world.served.sim.held[world.conid] == held - 1
    assert working_legs(world, world.conid) == {"stop": (held - 1, 225.4), "take_profit": (held - 1, 239.2)}


@pytest.mark.asyncio
async def test_malformed_output_is_a_recorded_refusal(stack):
    world, node, _ = stack
    node.orchestrator.script(ENTRY_MARKER, "buy MSFT")
    await node.slots()
    cycle_id = node.controller_slot(ENTRY).cycle_id
    assert node.cycle(cycle_id) == ("DONE", "OUTPUT_NO_JSON")
    assert (cycle_id, "entries", "REFUSED", "OUTPUT_NO_JSON") in node.rulings()
    assert submissions(node) == [] and node.jev.requests == []


@pytest.mark.asyncio
async def test_the_quote_moving_below_the_stop_during_jev_is_refused_by_the_trader(stack):
    world, node, _ = stack

    def crash(_request):
        world.served.sim.quote(world.conid, 200.0, 200.1)
        return ruling()
    node.jev.script(JEV_MARKER, crash)
    source = world.strategy_signal()
    await node.signals()
    assert world.receipt(derive_decision_id(source, f"enter:{world.conid}")).error_code == "STOP_INVALID"
    assert world.entries() == []


@pytest.mark.asyncio
async def test_the_position_closing_during_the_model_call_is_refused_by_the_trader(stack):
    world, node, _ = stack
    await enter_and_settle(world, node)

    def stopped_out(_request):
        sim = world.served.sim
        sim.quote(world.conid, 220.0, 220.1)                       # through the stop: the broker fills it
        stop = next(entity for entity, row in sim.orders.items()
                    if row.conid == world.conid and row.leg == "stop" and row.status in ("Submitted", "PreSubmitted"))
        sim._fill(stop, sim.orders[stop].total_quantity - sim.orders[stop].filled_quantity)
        world.advance(1)                                           # promoted: the trader sees the position flat
        assert not sim.held.get(world.conid)
        return closes(("P1", "CLOSE", None))
    node.orchestrator.script(CLOSE_MARKER, stopped_out)
    node.orchestrator.script(ENTRY_MARKER, picks())
    sells_before = [p for p in world.served.sim.placed if p[2] == "SELL"]
    await next_slot(world, node)
    position_cycle = node.controller_slot("position").cycle_id
    close_id = derive_decision_id(position_cycle, f"close:{world.conid}")
    assert world.receipt(close_id).error_code == "NOT_A_REDUCTION"
    assert [p for p in world.served.sim.placed if p[2] == "SELL"] == sells_before     # no second sell order


def close_rows(node):
    return node.node.store.db.execute("SELECT decision_id, state FROM ai_submissions WHERE action = 'CLOSE'",
                                      fetch="all")


@pytest.mark.asyncio
async def test_a_sell_before_the_entry_fills_waits_and_then_closes_once(stack):          # PR #86 thread 4211394337
    world, node, _ = stack
    node.jev.script(JEV_MARKER, ruling())
    buy = world.strategy_signal()
    await node.signals()
    assert (await node.node.submitter.get(derive_decision_id(buy, f"enter:{world.conid}"))).state in (
        "ACCEPTED", "FINAL")
    world.served.sim.quote(world.conid, 232.0, 232.1)              # the entry's limit is no longer marketable
    world.advance(1)
    assert not world.served.sim.held.get(world.conid)              # accepted, working, unfilled
    sell = world.strategy_signal(action="SELL")
    await node.signals()
    assert node.opportunity(sell) == ("IN_PROGRESS", "EXIT_WAITING_FOR_ENTRY") and close_rows(node) == []
    world.advance(400)                                              # older than the signal age limit: still kept
    await node.signals()
    assert node.opportunity(sell) == ("IN_PROGRESS", "EXIT_WAITING_FOR_ENTRY") and close_rows(node) == []
    world.served.sim.quote(world.conid, 229.9, 230.0)              # the entry fills and is protected
    world.settle()
    await node.signals()                                            # the trip is held now: the exit closes it
    close_id = derive_decision_id(sell, f"close:{world.conid}")
    assert node.opportunity(sell) == ("DECIDED", "EXIT_SIGNAL") and [r[0] for r in close_rows(node)] == [close_id]
    await settle_close(world, node, close_id)
    await node.signals()
    assert not world.served.sim.held.get(world.conid) and len(close_rows(node)) == 1
    assert len([p for p in world.served.sim.placed if p[2] == "SELL" and p[1] == "MKT"]) <= 1
