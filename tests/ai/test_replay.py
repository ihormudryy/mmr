import httpx
import pytest
import pytest_asyncio
from tests.ai.world import World, request
from trader.ai.gateway import CallFailed
from trader.ai.replay import (
    COMPLETE, INCOMPLETE, ExternalAdapterCounter, ExternalCallInReplay, RecordingClock, ReplayDiverged,
    ReplayEvidence, ReplayIncomplete, ReplayRecorder, ReplaySession,
)


DECISION = "dec-1"


async def live_decision(world):
    """One live decision: a tool read, two clock reads, an orchestrator call and a Jev call."""
    recorder = ReplayRecorder(world.store)
    clock = RecordingClock(world.clock)
    started = clock.now()
    await recorder.record_tool_result(DECISION, "quote", {"conid": 4391}, {"last": 12.5, "delayed": True})
    await recorder.record_tool_result(DECISION, "quote", {"conid": 4391}, {"last": 12.6, "delayed": True})
    first = await world.gateway.call("orchestrator", request(f"{DECISION}/orchestrator/1", "ideas?"),
                                     world.gateway.new_deadline())
    second = await world.gateway.call("jev", request(f"{DECISION}/jev/1", "judge"), world.gateway.new_deadline())
    clock.now()
    await recorder.record_clock_values(DECISION, clock.values)
    await recorder.record_manifest(DECISION, code_version="abc123", config_digest=world.config.digest())
    return started, first, second


@pytest_asyncio.fixture
async def decided(tmp_path, clock):
    world = World(tmp_path, clock)
    await world.gateway.start()
    return world, await live_decision(world)


@pytest.mark.asyncio
async def test_replay_reproduces_the_decision_with_zero_external_calls(decided, no_network):
    world, (started, first, second) = decided
    provider_calls = len(world.orchestrator.requests) + len(world.jev.requests)
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION))
    session.counter.instrument("openrouter", world.gateway._clients["orchestrator"])  # a live adapter that replay must never call

    async def decide(replay: ReplaySession):
        assert replay.clock.now() == started
        quote_1 = replay.tool_result("quote", {"conid": 4391})
        quote_2 = replay.tool_result("quote", {"conid": 4391})
        a = await replay.gateway.call("orchestrator", request(f"{DECISION}/orchestrator/1", "ideas?"),
                                      replay.gateway.new_deadline())
        b = await replay.gateway.call("jev", request(f"{DECISION}/jev/1", "judge"), replay.gateway.new_deadline())
        return quote_1["last"], quote_2["last"], a, b

    result = await session.arun(decide)
    assert result.status == COMPLETE
    last_1, last_2, a, b = result.value
    assert (last_1, last_2) == (12.5, 12.6)
    assert a.response == first.response and b.response == second.response and a.replayed
    assert session.evidence.manifest == {"code_version": "abc123", "config_digest": world.config.digest()}
    assert session.counter.total == 0
    session.assert_no_external_calls()
    assert len(world.orchestrator.requests) + len(world.jev.requests) == provider_calls


@pytest.mark.asyncio
async def test_the_counter_really_counts_a_live_adapter(decided):
    world, _ = decided
    counter = ExternalAdapterCounter()
    live = counter.instrument("openrouter", world.gateway._clients["jev"])
    await live.complete(request("probe/jev/1"), timeout_seconds=5)
    assert (counter.total, counter.count("openrouter")) == (1, 1)
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION), counter)
    with pytest.raises(AssertionError):
        session.assert_no_external_calls()


@pytest.mark.asyncio
async def test_missing_evidence_is_an_incomplete_result_and_never_a_fetch(decided, no_network):
    world, _ = decided
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION))
    fetch = session.counter.tripwire("alpaca")

    async def wants_more(replay: ReplaySession):
        replay.tool_result("quote", {"conid": 4391})
        replay.tool_result("quote", {"conid": 4391})
        return replay.tool_result("quote", {"conid": 4391})  # a third read was never recorded

    result = await session.arun(wants_more)
    assert (result.status, result.missing) == (INCOMPLETE, ("tool_result:quote#3",))
    assert session.counter.total == 0  # the code never reached for the live tool
    with pytest.raises(ExternalCallInReplay):
        fetch()
    assert session.counter.count("alpaca") == 1


@pytest.mark.asyncio
async def test_missing_attempt_and_missing_clock_are_incomplete(decided, no_network):
    world, _ = decided
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION))

    async def unrecorded_call(replay: ReplaySession):
        return await replay.gateway.call("orchestrator", request(f"{DECISION}/orchestrator/9"),
                                         replay.gateway.new_deadline())

    result = await session.arun(unrecorded_call)
    assert (result.status, result.missing) == (INCOMPLETE, (f"attempt:{DECISION}/orchestrator/9#1",))

    def too_many_clock_reads(replay: ReplaySession):
        return [replay.clock.now() for _ in range(3)]  # two were recorded

    run = ReplaySession(ReplayEvidence.load(world.store, DECISION)).run(too_many_clock_reads)
    assert (run.status, run.missing) == (INCOMPLETE, ("clock_value#3",))

    nothing = ReplaySession(ReplayEvidence.load(world.store, "never-recorded"))
    assert nothing.run(lambda replay: replay.tool_result("quote", {"conid": 1})).status == INCOMPLETE
    assert nothing.evidence.manifest is None and nothing.evidence.attempts == {}
    assert nothing.run(lambda replay: replay.clock.now()).missing == ("clock_value#1",)


@pytest.mark.asyncio
async def test_a_changed_prompt_is_a_divergence_not_a_silent_answer(decided, no_network):
    world, _ = decided
    session = ReplaySession(ReplayEvidence.load(world.store, DECISION))

    async def changed_prompt(replay: ReplaySession):
        return await replay.gateway.call("orchestrator", request(f"{DECISION}/orchestrator/1", "other ideas?"),
                                         replay.gateway.new_deadline())

    result = await session.arun(changed_prompt)
    assert result.status == INCOMPLETE
    assert result.missing == (f"request_changed:{DECISION}/orchestrator/1#1",)
    with pytest.raises(ReplayDiverged):
        await ReplaySession(ReplayEvidence.load(world.store, DECISION)).gateway.call(
            "orchestrator", request(f"{DECISION}/orchestrator/1", "other ideas?"), session.gateway.new_deadline())
    with pytest.raises(ReplayDiverged) as wrong_role:
        await ReplaySession(ReplayEvidence.load(world.store, DECISION)).gateway.call(
            "jev", request(f"{DECISION}/orchestrator/1", "ideas?"), session.gateway.new_deadline())
    assert str(wrong_role.value).startswith("replay evidence is missing: role_changed")
    assert issubclass(ReplayDiverged, ReplayIncomplete)


@pytest.mark.asyncio
async def test_recorded_failures_replay_as_failures_and_unknown_stays_unknown(tmp_path, clock):
    world = World(tmp_path, clock)
    await world.gateway.start()
    world.jev.respond = lambda r: httpx.Response(500, text="upstream down")
    with pytest.raises(CallFailed):
        await world.gateway.call("jev", request("dec-2/jev/1", "judge"), world.gateway.new_deadline())
    world.jev.respond = lambda r: httpx.Response(400, text="bad")
    with pytest.raises(CallFailed):
        await world.gateway.call("jev", request("dec-2/jev/1", "judge"), world.gateway.new_deadline())  # second attempt
    world.jev.respond = None
    live = await world.gateway.call("jev", request("dec-2/jev/1", "judge"), world.gateway.new_deadline())  # third

    async def replayed(replay: ReplaySession):
        outcomes = []
        for _ in range(3):
            try:
                outcomes.append(await replay.gateway.call("jev", request("dec-2/jev/1", "judge"),
                                                          replay.gateway.new_deadline()))
            except CallFailed as failure:
                outcomes.append(failure)
        return outcomes

    result = await ReplaySession(ReplayEvidence.load(world.store, "dec-2")).arun(replayed)
    first, second, third = result.value
    assert isinstance(first, CallFailed) and first.outcome == "UNKNOWN" and first.outcome_unknown
    assert first.code == "HTTP_500" and first.attempt_key == "dec-2/jev/1#1"
    assert isinstance(second, CallFailed) and second.outcome == "REJECTED" and not second.outcome_unknown
    assert third.response == live.response and third.replayed and third.attempt_key == "dec-2/jev/1#3"


@pytest.mark.asyncio
async def test_a_tool_result_that_is_not_plain_json_is_refused_when_recorded(store):
    recorder = ReplayRecorder(store)
    with pytest.raises(ValueError):
        await recorder.record_tool_result("dec-3", "quote", {"conid": 1}, {"last": float("nan")})
    with pytest.raises(TypeError):
        await recorder.record_tool_result("dec-3", "quote", {"conid": 1}, {"when": object()})
    with pytest.raises(TypeError):
        await recorder.record_tool_result("dec-3", "quote", {"conid": 1}, {1, 2})
    assert store.db.execute("SELECT count(*) FROM ai_replay_evidence", fetch="one")[0] == 0
    await recorder.record_tool_result("dec-3", "quote", {"conid": 1}, {"last": 1.5})
    await recorder.record_tool_result("dec-3", "quote", {"conid": 2}, {"last": 2.5})
    session = ReplaySession(ReplayEvidence.load(store, "dec-3"))
    assert session.tool_result("quote", {"conid": 2}) == {"last": 2.5}
    assert session.tool_result("quote", {"conid": 1}) == {"last": 1.5}
