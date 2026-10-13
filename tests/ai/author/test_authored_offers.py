"""SP3 Plan 5 Task 8: stored authored strategies become research candidates; unproven isolation waits."""
import json
import logging

import pytest
import pytest_asyncio

from tests.ai.author.helpers import authored_reply, record
from tests.ai.research.cases import resolved, submitted, view
from tests.ai.research.rig import EXPERIMENT, Leader, Rig, ScriptedClient, Watch
from tests.ai.research.test_research_pump import EMPTY, night, script
from trader.ai.author_wire import parse_authored_strategies
from trader.ai.authored_offers import AuthoredOffers, authored_submit_body, default_point
from trader.ai.research_cycle import ResearchCycle
from trader.ai.research_roles import RESEARCH_MARKER
from trader.ai.rpc_clients import AUTHOR_COMMANDS, AUTHOR_QUERIES, RpcNotSent
from trader.ai.schedule import SessionSlots

STORED_KEY = "authored:sha256:" + "a" * 64 + ":Old"


@pytest_asyncio.fixture
async def rig(tmp_path):
    built = Rig(tmp_path)
    await built.start()
    built.author = ScriptedClient(AUTHOR_COMMANDS | AUTHOR_QUERIES)
    return built


def cycle_with_offers(rig):
    return ResearchCycle(config=rig.config, store=rig.store, clock=rig.clock, slots=SessionSlots(),
                         leadership=Leader(1), watch=Watch(EXPERIMENT), lab=rig.lab, registry=rig.registry,
                         gateway=rig.gated, judge=rig.judge, strategies_root=rig.tmp_path,
                         authored=AuthoredOffers(client=rig.author, clock=rig.clock))


def candidates(rig):
    return rig.rows("SELECT cycle_id, strategy_key, body_json, state FROM ai_research_candidates ORDER BY created_at")


def test_the_body_is_one_default_point_on_the_universe():
    (rec,) = parse_authored_strategies(authored_reply(strategies=[
        record("a", state="STORED", reason=None, tunables={"WINDOW": {"low": 2, "high": 10, "default": 5}})])).records
    point = default_point(rec)                                          # the record's own default (D5)
    assert point == {"WINDOW": 5.0} and isinstance(point["WINDOW"], float)
    assert authored_submit_body(rec, (1003, 1001, 1002), point) == {
        "kind": "INITIAL", "strategy_key": STORED_KEY, "cohort": [{"WINDOW": 5.0}], "conids": [1001, 1002, 1003],
        "bar_size": "15 mins"}


@pytest.mark.asyncio
async def test_stored_records_become_one_candidate_each(rig):                         # review focus 3
    rig.author.script("get_authored_strategies", authored_reply(strategies=[
        record("a", state="STORED", reason=None), record("b", state="EVALUATING", reason=None)]))
    rig.lab.script("submit_evaluation", submitted())
    rig.lab.script("get_evaluation", view("RUNNING"))
    cycle = cycle_with_offers(rig)
    for _ in range(3):
        await cycle.pump()
        rig.clock.advance(400)                                         # past the 5-minute read interval
    (cycle_id, key, body_json, state), = candidates(rig)
    assert (cycle_id, key, state) == ("rcy-20261008", STORED_KEY, "SUBMITTED")
    assert json.loads(body_json) == {"kind": "INITIAL", "strategy_key": STORED_KEY, "cohort": [{"WINDOW": 5.0}],
                                     "conids": list(range(1001, 1009)), "bar_size": "15 mins"}
    assert len(rig.lab.sent("submit_evaluation")) == 1 and len(rig.author.sent("get_authored_strategies")) == 3


@pytest.mark.asyncio
async def test_switch_off_offers_nothing(rig):
    rig.author.script("get_authored_strategies", authored_reply(enabled=False, strategies=[
        record("a", state="STORED", reason=None)]))
    await cycle_with_offers(rig).pump()
    assert candidates(rig) == [] and rig.lab.sent("submit_evaluation") == []


@pytest.mark.asyncio
async def test_an_away_trader_or_a_bad_reply_offers_nothing(rig):
    rig.author.script("get_authored_strategies", RpcNotSent("TRADER_UNREACHABLE"), {"enabled": True})
    cycle = cycle_with_offers(rig)
    await cycle.pump()
    rig.clock.advance(400)
    await cycle.pump()
    assert candidates(rig) == []


@pytest.mark.asyncio
async def test_a_record_left_stored_is_offered_again_the_next_night(rig):
    rig.author.script("get_authored_strategies", authored_reply(strategies=[
        record("a", state="STORED", reason=None)]))
    rig.lab.script("submit_evaluation", submitted())
    rig.lab.script("get_evaluation", view("RUNNING"))
    cycle = cycle_with_offers(rig)
    await cycle.pump()
    rig.clock.advance(24 * 3600)                                       # Friday 17:00: the next research slot
    await cycle.pump()
    assert [row[0] for row in candidates(rig)] == ["rcy-20261008", "rcy-20261009"]


@pytest.mark.asyncio
async def test_a_record_is_offered_without_any_local_author_row(rig):                 # D5: ai.duckdb may be lost
    assert rig.rows("SELECT count(*) FROM author_attempts") == [(0,)]
    rig.author.script("get_authored_strategies", authored_reply(strategies=[
        record("a", state="STORED", reason=None)]))
    rig.lab.script("submit_evaluation", submitted())
    rig.lab.script("get_evaluation", view("RUNNING"))
    await cycle_with_offers(rig).pump()
    assert [row[1] for row in candidates(rig)] == [STORED_KEY]


@pytest.mark.asyncio
async def test_unproven_isolation_waits_for_the_next_evening(rig):                   # review focus 4
    script(rig)
    rig.registry.queues["register_ai_deployment"] = [
        {**resolved(), "state": "REJECTED", "outcome": None, "error_code": "AUTHORED_ISOLATION_UNPROVEN"},
        resolved()]
    cycle = await night(rig)
    assert rig.rows("SELECT state, error_code FROM ai_research_registrations") == [
        ("WAITING_CAP", "AUTHORED_ISOLATION_UNPROVEN")]
    rig.clock.advance(24 * 3600)
    rig.orchestrator.script(RESEARCH_MARKER, EMPTY)
    await night(rig, cycle, pumps=1)
    assert rig.rows("SELECT state, error_code FROM ai_research_registrations") == [("REGISTERED", None)]


def offer_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "trader.ai.authored_offers"]


@pytest.mark.asyncio
async def test_each_reason_for_no_offer_is_logged_once(rig, caplog):
    caplog.set_level(logging.INFO, logger="trader.ai.authored_offers")
    offers = AuthoredOffers(client=rig.author, clock=rig.clock)
    away, good = RpcNotSent("TRADER_UNREACHABLE"), authored_reply(strategies=[record("a", state="STORED", reason=None)])
    rig.author.script("get_authored_strategies", away, away, {"enabled": True}, {"enabled": True},
                      authored_reply(enabled=False), authored_reply(enabled=False), good, away, good)
    found = []
    for _ in range(9):
        found.append(await offers.due_records())
        rig.clock.advance(400)
    assert [len(records) for records in found] == [0, 0, 0, 0, 0, 0, 1, 0, 1]
    logs = offer_logs(caplog)
    assert len(logs) == 4                     # away, bad reply, switch off, away again after a good read
    assert "TRADER_UNREACHABLE" in logs[0] and "get_authored_strategies" in logs[1] and "off" in logs[2]
    assert "TRADER_UNREACHABLE" in logs[3]


@pytest.mark.asyncio
async def test_the_trader_is_read_at_most_every_five_minutes(rig):
    rig.author.script("get_authored_strategies", authored_reply(strategies=[record("a", state="STORED", reason=None)]))
    offers = AuthoredOffers(client=rig.author, clock=rig.clock)
    assert len(await offers.due_records()) == 1
    rig.clock.advance(299)
    assert await offers.due_records() == ()
    rig.clock.advance(1)
    assert len(await offers.due_records()) == 1
    assert len(rig.author.sent("get_authored_strategies")) == 2
