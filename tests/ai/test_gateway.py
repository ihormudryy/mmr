import asyncio
import threading
import time
from datetime import datetime, timezone
import httpx
import pytest
import pytest_asyncio
from tests.ai.fakes import (
    JEV_WORST_CASE_MICROS, ORCHESTRATOR_WORST_CASE_MICROS,
)
from tests.ai.fakes import FakeProvider, load_test_config
from tests.ai.world import World, request
from trader.ai.budget import next_window_start
from trader.ai.config import AiConfigError, usd_to_micros_floor
from trader.ai.gateway import CallFailed, CallRefused, ModelGateway, build_gateway
from trader.ai.model_client import BedrockAdapter, Usage
from trader.ai.store import AiStore


SECRET = "sk-test-key-0001"
UTC = timezone.utc


@pytest_asyncio.fixture
async def world(tmp_path, clock):
    built = World(tmp_path, clock)
    await built.start()
    return built


@pytest.mark.asyncio
async def test_a_call_before_any_cap_is_refused_and_sends_nothing(tmp_path, clock):
    built = World(tmp_path, clock)
    await built.gateway.start()  # no set_cap: the trader was never read
    with pytest.raises(CallRefused) as caught:
        await built.gateway.call("orchestrator", request(), built.gateway.new_deadline())
    assert caught.value.code == "BUDGET_CAP_UNKNOWN"
    assert built.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)]
    assert built.rows("SELECT count(*) FROM ai_model_attempts") == [(0,)]
    assert built.orchestrator.requests == []
    await built.gateway.budget.set_cap(usd_to_micros_floor(2000))  # the slot was not leaked
    assert (await built.gateway.call("orchestrator", request(), built.gateway.new_deadline())).response.text


@pytest.mark.asyncio
async def test_missing_price_refuses_and_leaves_no_trace(tmp_path, clock):
    built = World(tmp_path, clock, orchestrator_model="vendor/unpriced")
    await built.start()
    with pytest.raises(CallRefused) as caught:
        await built.gateway.call("orchestrator", request(), built.gateway.new_deadline())
    assert caught.value.code == "PRICE_UNAVAILABLE"
    assert built.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)]
    assert built.rows("SELECT count(*) FROM ai_model_attempts") == [(0,)]
    assert built.orchestrator.requests == []


@pytest.mark.asyncio
async def test_timeout_is_unknown_keeps_the_reservation_and_a_late_report_reconciles_once(tmp_path, clock):
    world = World(tmp_path, clock, call_timeout="0.05")
    await world.start()
    world.orchestrator.hold = asyncio.Event()  # never released: the call times out
    with pytest.raises(CallFailed) as caught:
        await world.gateway.call("orchestrator", request(), world.gateway.new_deadline())
    assert caught.value.outcome == "UNKNOWN" and caught.value.outcome_unknown and caught.value.code == "CALL_TIMEOUT"
    snapshot = await world.gateway.budget.snapshot()
    assert (snapshot.committed_micros, snapshot.unknown_reservations) == (ORCHESTRATOR_WORST_CASE_MICROS, 1)
    assert world.rows("SELECT kind, cost_micros FROM ai_cost_events") == [("ESTIMATED_UNKNOWN", ORCHESTRATOR_WORST_CASE_MICROS)]
    key = caught.value.attempt_key
    assert await world.gateway.report_late_usage(key, Usage(1000, 200)) is True
    assert await world.gateway.report_late_usage(key, Usage(1000, 200)) is False
    assert (await world.gateway.budget.snapshot()).committed_micros == 6000
    assert [r[0] for r in world.rows("SELECT kind FROM ai_cost_events ORDER BY event_seq")] == ["ESTIMATED_UNKNOWN", "CORRECTION"]


@pytest.mark.asyncio
async def test_malformed_usage_keeps_the_full_reservation(world):
    world.orchestrator.respond = lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": "many"}})
    with pytest.raises(CallFailed) as caught:
        await world.gateway.call("orchestrator", request(), world.gateway.new_deadline())
    assert caught.value.outcome == "COST_UNKNOWN" and caught.value.outcome_unknown
    assert world.rows("SELECT status FROM ai_model_attempts") == [("COST_UNKNOWN",)]
    assert (await world.gateway.budget.snapshot()).committed_micros == ORCHESTRATOR_WORST_CASE_MICROS


@pytest.mark.asyncio
async def test_cancelling_a_call_records_unknown_and_keeps_the_reservation(world):
    world.orchestrator.hold = asyncio.Event()
    task = asyncio.create_task(world.gateway.call("orchestrator", request(), world.gateway.new_deadline()))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.rows("SELECT status, error_code FROM ai_model_attempts") == [("UNKNOWN", "CALLER_CANCELLED")]
    assert (await world.gateway.budget.snapshot()).committed_micros == ORCHESTRATOR_WORST_CASE_MICROS
    follow_up = await world.gateway.call("jev", request("d/j/1"), world.gateway.new_deadline())  # the slot was freed
    assert follow_up.response.text


@pytest.mark.asyncio
async def test_restart_turns_a_half_finished_call_into_a_counted_unknown(tmp_path, clock):
    first = World(tmp_path, clock)
    await first.start()
    # simulate a crash after "reserve + journal begin" committed and before the adapter answered
    await first.gateway._reserve_and_begin("orchestrator", first.config.role("orchestrator"), request(),
                                           ORCHESTRATOR_WORST_CASE_MICROS)
    second = World(tmp_path, clock)  # new process, same ai.duckdb
    await second.start()
    assert second.rows("SELECT status, error_code FROM ai_model_attempts") == [("UNKNOWN", "PROCESS_RESTARTED")]
    snapshot = await second.gateway.budget.snapshot()
    assert (snapshot.unknown_reservations, snapshot.committed_micros) == (1, ORCHESTRATOR_WORST_CASE_MICROS)
    assert second.rows("SELECT kind FROM ai_cost_events") == [("ESTIMATED_UNKNOWN",)]


@pytest.mark.asyncio
async def test_success_prices_settles_and_journals(world):
    result = await world.gateway.call("orchestrator", request(), world.gateway.new_deadline())
    assert result.cost_micros == 6000  # 1000 x $3/M + 200 x $15/M
    assert result.reserved_micros == ORCHESTRATOR_WORST_CASE_MICROS
    assert (result.overrun, result.replayed) == (False, False)
    assert result.attempt_key == "d1/orchestrator/1#1" and result.response.text == '{"ok": true}'
    assert world.rows("SELECT status, input_tokens, output_tokens FROM ai_model_attempts") == [("SUCCEEDED", 1000, 200)]
    assert world.rows("SELECT state, actual_micros FROM ai_budget_reservations") == [("SETTLED", 6000)]
    assert world.rows("SELECT kind, cost_micros FROM ai_cost_events") == [("CONFIRMED", 6000)]
    assert (await world.gateway.budget.snapshot()).committed_micros == 6000


@pytest.mark.asyncio
async def test_attempt_key_reaches_the_adapter_request_log_but_not_the_wire(tmp_path, clock):
    world = World(tmp_path, clock)
    inner = world.gateway._clients["orchestrator"]
    seen = []

    class Spy:
        backend, model_id = inner.backend, inner.model_id

        async def complete(self, request, *, timeout_seconds):
            seen.append((request.request_key, request.attempt_key))
            return await inner.complete(request, timeout_seconds=timeout_seconds)

        async def aclose(self):
            return None

    spied = ModelGateway(config=world.config, store=world.store, clock=clock,
                         clients={"orchestrator": Spy(), "jev": world.gateway._clients["jev"]})
    await spied.start()
    await spied.budget.set_cap(usd_to_micros_floor(2000))
    await spied.call("orchestrator", request(), spied.new_deadline())
    assert seen == [("d1/orchestrator/1", "d1/orchestrator/1#1")]
    wire = world.orchestrator.requests[0].read()
    assert b"d1/orchestrator" not in wire and b"attempt" not in wire


@pytest.mark.asyncio
async def test_request_limits_are_enforced_before_any_reservation(world):
    too_long = request(text="a" * (60000 * 3 + 30))
    cases = [("orchestrator", request(max_output_tokens=4001), "OUTPUT_LIMIT_ABOVE_ROLE"),
             ("orchestrator", too_long, "INPUT_TOO_LARGE"),
             ("nobody", request(), "ROLE_UNKNOWN")]
    for role, bad, code in cases:
        with pytest.raises(CallRefused) as caught:
            await world.gateway.call(role, bad, world.gateway.new_deadline())
        assert caught.value.code == code
    assert world.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)]
    assert world.rows("SELECT count(*) FROM ai_model_attempts") == [(0,)]
    assert world.orchestrator.requests == [] and world.jev.requests == []


@pytest.mark.asyncio
async def test_daily_cap_blocks_with_the_reset_time_and_sends_nothing(tmp_path, clock):
    world = World(tmp_path, clock, cap_usd=0.1)  # 100_000 micro-USD, below the 240_000 worst case
    await world.start()
    with pytest.raises(CallRefused) as caught:
        await world.gateway.call("orchestrator", request(), world.gateway.new_deadline())
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert caught.value.retry_at == next_window_start(clock.now())
    assert world.orchestrator.requests == []
    assert world.rows("SELECT count(*) FROM ai_model_attempts") == [(0,)]
    follow_up = await world.gateway.call("jev", request("d/j/1"), world.gateway.new_deadline())  # 80_000 fits
    assert follow_up.response.text


@pytest.mark.asyncio
async def test_not_sent_and_rejected_release_the_reservation(world):
    def refuse_to_connect(r):
        raise httpx.ConnectError("no route")

    world.orchestrator.respond = refuse_to_connect
    with pytest.raises(CallFailed) as caught:
        await world.gateway.call("orchestrator", request("d/o/1"), world.gateway.new_deadline())
    assert (caught.value.outcome, caught.value.outcome_unknown) == ("NOT_SENT", False)
    world.orchestrator.respond = lambda r: httpx.Response(400, text="bad request")
    with pytest.raises(CallFailed) as caught:
        await world.gateway.call("orchestrator", request("d/o/2"), world.gateway.new_deadline())
    assert (caught.value.outcome, caught.value.outcome_unknown) == ("REJECTED", False)
    assert (await world.gateway.budget.snapshot()).committed_micros == 0
    assert sorted(world.rows("SELECT state, release_reason FROM ai_budget_reservations")) == [
        ("RELEASED", "NOT_SENT"), ("RELEASED", "REJECTED")]
    assert world.rows("SELECT kind, cost_micros FROM ai_cost_events") == [("NONE", 0), ("NONE", 0)]
    assert sorted(r[0] for r in world.rows("SELECT status FROM ai_model_attempts")) == ["NOT_SENT", "REJECTED"]


@pytest.mark.asyncio
async def test_at_most_two_calls_are_in_flight(world):
    world.orchestrator.hold = asyncio.Event()
    tasks = [asyncio.create_task(world.gateway.call("orchestrator", request(f"d/o/{i}"), world.gateway.new_deadline()))
             for i in range(5)]
    await asyncio.sleep(0.3)
    assert world.orchestrator.in_flight == 2
    world.orchestrator.hold.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == 5
    assert world.orchestrator.max_in_flight == 2
    assert (await world.gateway.budget.snapshot()).open_reservations == 0


@pytest.mark.asyncio
async def test_one_deadline_spans_orchestrator_and_jev(tmp_path, clock):
    world = World(tmp_path, clock, deadline=10, call_timeout="5")
    await world.start()
    world.orchestrator.advance_seconds = 6
    world.jev.advance_seconds = 6
    deadline = world.gateway.new_deadline("decision-1")
    await world.gateway.call("orchestrator", request("d/o/1"), deadline)
    assert deadline.remaining() == pytest.approx(4)
    await world.gateway.call("jev", request("d/j/1"), deadline)
    assert deadline.expired
    with pytest.raises(CallRefused) as caught:
        await world.gateway.call("orchestrator", request("d/o/2"), deadline)
    assert caught.value.code == "DEADLINE_EXPIRED"
    assert world.rows("SELECT count(*) FROM ai_model_attempts") == [(2,)]
    assert len(world.orchestrator.requests) == 1


@pytest.mark.asyncio
async def test_hourly_limit_delays_inside_the_deadline_and_refuses_beyond_it(tmp_path, clock):
    world = World(tmp_path, clock, calls_per_hour=2, deadline=600)
    await world.start()
    started = clock.now()
    await world.gateway.call("jev", request("d/j/1"), world.gateway.new_deadline())
    clock.advance(3100)
    await world.gateway.call("jev", request("d/j/2"), world.gateway.new_deadline())
    before = clock.now()
    third = await world.gateway.call("jev", request("d/j/3"), world.gateway.new_deadline())  # waits for the hour
    assert third.response.text
    assert (clock.now() - before).total_seconds() == pytest.approx(500, abs=1)
    assert (clock.now() - started).total_seconds() > 3600
    with pytest.raises(CallRefused) as caught:
        await world.gateway.call("jev", request("d/j/4"), world.gateway.new_deadline())
    assert caught.value.code == "HOURLY_LIMIT_EXCEEDS_DEADLINE"
    assert caught.value.retry_at > clock.now()
    assert world.rows("SELECT count(*) FROM ai_model_attempts") == [(3,)]
    clock.advance(3600)
    again = await world.gateway.call("jev", request("d/j/4"), world.gateway.new_deadline())  # the slot was not leaked
    assert again.response.text


@pytest.mark.asyncio
async def test_provider_billing_more_than_the_reservation_is_counted_as_an_overrun(world):
    world.jev.usage = (90_000, 8_000)  # above the role limits: 90000 x $1/M + 8000 x $5/M
    result = await world.gateway.call("jev", request("d/j/1"), world.gateway.new_deadline())
    assert result.cost_micros == 130_000 and result.reserved_micros == JEV_WORST_CASE_MICROS
    assert result.overrun is True
    assert (await world.gateway.budget.snapshot()).committed_micros == 130_000
    assert world.rows("SELECT kind, cost_micros FROM ai_cost_events") == [("CONFIRMED", 130_000)]


@pytest.mark.asyncio
async def test_config_and_client_must_agree(tmp_path, clock):
    world = World(tmp_path, clock)
    good = world.gateway._clients
    wrong_model = world.orchestrator.adapter("vendor/other")
    for clients in ({"orchestrator": wrong_model, "jev": good["jev"]},
                    {"orchestrator": good["orchestrator"]},
                    {"orchestrator": good["jev"], "jev": good["jev"]}):
        with pytest.raises(AiConfigError) as caught:
            ModelGateway(config=world.config, store=world.store, clock=clock, clients=clients)
        assert caught.value.code == "CLIENT_ROLE_MISMATCH"
    ready = build_gateway(world.config, store=world.store, clock=clock, environ={}, clients=good)
    assert isinstance(ready, ModelGateway)
    with pytest.raises(AiConfigError) as caught:
        build_gateway(world.config, store=world.store, clock=clock, environ={}, clients={"jev": good["jev"]})
    assert caught.value.code == "CREDENTIALS_MISSING"
    built = build_gateway(world.config, store=world.store, clock=clock, environ={"OPENROUTER_API_KEY": "k-12345"})
    assert isinstance(built, ModelGateway)


@pytest.mark.asyncio
async def test_credentials_never_reach_the_database(world):
    await world.gateway.call("orchestrator", request("d/o/1"), world.gateway.new_deadline())
    world.orchestrator.respond = lambda r: httpx.Response(401, text=f"bad key {SECRET}")
    with pytest.raises(CallFailed):
        await world.gateway.call("orchestrator", request("d/o/2"), world.gateway.new_deadline())
    tables = [row[0] for row in world.rows("SHOW TABLES")]
    assert "ai_model_attempts" in tables
    for table in tables:
        dump = str(world.rows(f"SELECT * FROM {table}"))
        assert SECRET not in dump and "Bearer" not in dump


@pytest.mark.asyncio
async def test_a_hung_bedrock_thread_keeps_its_gateway_slot_until_it_returns(tmp_path, clock):
    config = load_test_config(tmp_path, orchestrator_backend="bedrock", call_timeout="0.05")
    release = threading.Event()
    lock = threading.Lock()
    seen = {"running": 0, "max_running": 0, "started": 0}

    def converse(**arguments):
        with lock:
            seen["running"] += 1
            seen["started"] += 1
            seen["max_running"] = max(seen["max_running"], seen["running"])
        try:
            release.wait(15)
        finally:
            with lock:
                seen["running"] -= 1
        return {"output": {"message": {"role": "assistant", "content": [{"text": "done"}]}},
                "usage": {"inputTokens": 11, "outputTokens": 3}, "stopReason": "end_turn"}

    gateway = ModelGateway(
        config=config, store=AiStore(tmp_path / "ai.duckdb", clock=clock), clock=clock,
        clients={"orchestrator": BedrockAdapter(model_id="vendor/orch-1", converse=converse),
                 "jev": FakeProvider(clock=clock, model="vendor/jev-1").adapter("vendor/jev-1")})
    await gateway.start()
    await gateway.budget.set_cap(usd_to_micros_floor(2000))
    tasks = [asyncio.create_task(gateway.call("orchestrator", request(f"d/o/{i}"), gateway.new_deadline()))
             for i in range(5)]
    try:
        await asyncio.sleep(0.8)  # the first two calls time out; their threads are still hanging
        assert (seen["started"], seen["running"]) == (2, 2)
        assert [t.done() for t in tasks] == [True, True, False, False, False]
        assert all(isinstance(t.exception(), CallFailed) for t in tasks[:2])
    finally:
        release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert [type(r).__name__ for r in results[:2]] == ["CallFailed", "CallFailed"]
    assert all(r.response.text == "done" for r in results[2:])
    assert seen["max_running"] == 2 and seen["started"] == 5
    follow_up = await gateway.call("orchestrator", request("d/o/9"), gateway.new_deadline())
    assert follow_up.response.text == "done"  # every slot came back


@pytest.mark.asyncio
async def test_cancelling_before_the_call_is_sent_leaves_nothing_open(world, monkeypatch):
    real_reserve = world.gateway.budget.reserve_in_tx

    def slow_reserve(*args, **kwargs):
        time.sleep(0.3)  # the reservation transaction is running in its worker thread
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(world.gateway.budget, "reserve_in_tx", slow_reserve)
    task = asyncio.create_task(world.gateway.call("orchestrator", request(), world.gateway.new_deadline()))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.rows("SELECT status, error_code FROM ai_model_attempts") == [("NOT_SENT", "CALLER_CANCELLED_BEFORE_SEND")]
    assert world.rows("SELECT state, release_reason FROM ai_budget_reservations") == [("RELEASED", "NOT_SENT")]
    snapshot = await world.gateway.budget.snapshot()
    assert (snapshot.committed_micros, snapshot.open_reservations, snapshot.unknown_reservations) == (0, 0, 0)
    assert world.rows("SELECT kind, cost_micros FROM ai_cost_events") == [("NONE", 0)]
    assert world.orchestrator.requests == []
    monkeypatch.undo()
    follow_up = await world.gateway.call("jev", request("d/j/1"), world.gateway.new_deadline())  # the slot was freed
    assert follow_up.response.text
