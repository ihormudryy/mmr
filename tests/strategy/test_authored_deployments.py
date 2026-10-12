# tests/strategy/test_authored_deployments.py
"""SP3 spec 7.6, 7.9 and 8 (strategy side): loading an authored deployment, its gates, its retirement.
Plan 4 rulings 18-20."""
import asyncio
import datetime as dt
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import zmq

from tests.authored.fake_sandbox import IMAGE_DIGEST, FakeSandbox, caller_source
from tests.rpc_identity_fixtures import free_port, make_identities
from tests.test_strategy_artifact_soft_load import _make_runtime, _write_strategy
from trader.authored.sandbox_client import SandboxProtocolError
from trader.data.backtest_store import compute_strategy_hash
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import StrategySignalRecord
from trader.messaging.ai_deployment_wire import ActiveAiDeployment
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.strategy_key import authored_strategy_path
from trader.strategy.ai_deployment_source import (
    AiDeploymentSource,
    ai_instance_name,
    source_unchanged,
)
from trader.strategy.authored_runtime import (
    AUTHORED_UNPROVEN,
    ISOLATION_ALARM,
    PROOF_OUTAGE_HOLD,
    PROOF_TIMEOUT_SECONDS,
    RETIRE_RESEND_CAP,
    STORE_UNAVAILABLE,
    AuthoredDeployments,
    AuthoredLiveStrategy,
    crash_backoff,
    proof_passes,
)
from trader.trading.strategy import StrategyState

VERSION, CATALOGUE_VERSION = "sha256:" + "d" * 64, "sha256:" + "f" * 64
BASE = "sha256:" + "b" * 64
CONID = 265598
RETIRED = {"status": "UPDATED", "code": None, "detail": None, "retryable": False, "record": None}
WITHDRAWAL_FAILED = {"status": "REFUSED", "code": "AUTHORED_WITHDRAWAL_FAILED", "detail": "resend",
                     "retryable": True, "record": None}


class FakeTraderRpc:
    """A typed RPC client double: replies per method, in order (the last repeats); an exception is raised."""

    def __init__(self, **replies):
        self.calls, self._replies = [], {method: list(values) for method, values in replies.items()}
        self.timeouts = []

    def answer(self, method, *replies):
        self._replies[method] = list(replies)

    def call(self, method, body, response_model, timeout=None):
        self.calls.append((method, dict(body)))
        self.timeouts.append((method, timeout))
        queue = self._replies[method]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def proof(image=IMAGE_DIGEST, passed=True):
    return {"found": True, "proof": {"proof_id": "sha256:" + "9" * 64, "image_digest": image, "passed": passed,
                                     "checks": [], "ran_at": "2026-10-12T01:00:00+00:00",
                                     "recorded_at": "2026-10-12T01:00:01+00:00"}}


def bars(count):
    index = pd.date_range("2024-03-04 14:30", periods=count, freq="1min", tz="UTC", name="date")
    closes = [100.0 + i for i in range(count)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
                        index=index)


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    rt = _make_runtime(tmp_path, tmp_duckdb_path, automation_enabled=False)
    rt._load_enabled = lambda name: None
    rt._last_dispatched_bar = {}
    rt._warmup_shortfalls = {}
    rt._hist_bar_days = {}
    rt.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(tmp_duckdb_path))
    rt.event_store = SimpleNamespace(append=lambda event: None)
    rt.zmq_messagebus_client = SimpleNamespace(write=lambda *args: None)
    rt._load_ai_history = lambda instance: None                     # no IB in a node test
    sandbox = FakeSandbox(research=False)                           # the live pool (Task 9 ruling)
    query = FakeTraderRpc(get_isolation_proof=[proof()])
    command = FakeTraderRpc(retire_authored_strategy=[RETIRED])
    clock = [dt.datetime(2024, 3, 4, 16, 0, tzinfo=dt.timezone.utc)]
    rt._authored_deployments = AuthoredDeployments(client=sandbox, trader_query=query, trader_command=command,
                                                   now=lambda: clock[0])
    return SimpleNamespace(rt=rt, sandbox=sandbox, query=query, command=command, clock=clock)


def authored_active(sandbox, name="counts_bars", class_name="CountsBars", *, params=None, digest=None,
                    conids=(CONID,)):
    file_digest = sandbox.add(caller_source(name))
    return ActiveAiDeployment(version_digest=VERSION, base_digest=BASE,
                              strategy_path=authored_strategy_path(file_digest),
                              strategy_digest=digest or file_digest, class_name=class_name, params=params or {},
                              conids=list(conids), bar_size="1 min", expiry_session="2026-11-06")


def catalogue_active(world):
    path = _write_strategy(world.rt.strategies_directory)
    return ActiveAiDeployment(version_digest=CATALOGUE_VERSION, base_digest=BASE,
                              strategy_path="strategies/" + Path(path).name,
                              strategy_digest="sha256:" + compute_strategy_hash(path),
                              class_name="VwapReclaimCat", params={}, conids=[CONID], bar_size="1 min",
                              expiry_session="2026-11-06")


def reconcile(world, deployments):
    AiDeploymentSource(runtime=world.rt, read_active=lambda: deployments, paper=True).reconcile()


def instance_of(world, version=VERSION):
    return world.rt.get_strategy(ai_instance_name(version))


def retirements(world):
    return [body for method, body in world.command.calls if method == "retire_authored_strategy"]


def test_an_authored_deployment_loads_without_any_file(world):
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    assert isinstance(instance, AuthoredLiveStrategy) and instance.state == StrategyState.RUNNING
    assert instance.ai_deployment_version == VERSION and instance.paper_only
    assert not any(Path(world.rt.strategies_directory).iterdir())
    assert world.query.calls == [("get_isolation_proof", {"image_digest": IMAGE_DIGEST})]


def test_its_signals_take_the_existing_signal_path(world):
    deployment = authored_active(world.sandbox)
    reconcile(world, [deployment])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    frame = bars(3)
    world.rt._dispatch_once(instance, instance.on_prices(frame), CONID, frame)
    entries = world.rt.signal_record.read(0, 10).signals
    assert [(e.entry.action, e.entry.source_digest, e.entry.deployment_version) for e in entries] == [
        ("BUY", deployment.strategy_digest, VERSION)]


def test_a_digest_that_is_not_the_files_is_refused_and_retired(world, caplog):
    file_digest = authored_active(world.sandbox).strategy_digest
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored_active(world.sandbox, digest="sha256:" + "0" * 64)])
    assert world.rt.ai_instances() == {} and "AUTHORED_DIGEST_MISMATCH" in caplog.text
    world.rt._authored_deployments.send_retirements()
    assert [(body["digest"], body["code"]) for body in retirements(world)] == [
        (file_digest, "SANDBOX_DIGEST_MISMATCH")]


@pytest.mark.parametrize("reply", [proof(passed=False), proof(image="sha256:" + "f" * 64),
                                   {"found": False, "proof": None}])
def test_no_passing_proof_for_the_running_image_loads_nothing_and_retires_nothing(world, reply, caplog):
    world.query.answer("get_isolation_proof", reply)
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {} and ISOLATION_ALARM in caplog.text
    world.rt._authored_deployments.send_retirements()
    assert retirements(world) == []


def test_a_sandbox_rebuilt_after_the_load_gives_no_signal_until_its_build_is_proven(world):   # review r1 #6
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    world.sandbox.image_digest = "e4f5a6b-20261012T090000Z"              # build B replaced the proven build
    world.query.answer("get_isolation_proof", {"found": False, "proof": None})   # B has no proof yet
    frame = bars(3)
    assert instance.on_prices(frame) is None and world.sandbox.sessions == []
    assert world.rt.signal_record.read(0, 10).signals == ()
    world.query.answer("get_isolation_proof", proof(image="e4f5a6b-20261012T090000Z"))
    signal = instance.on_prices(frame)                                   # read fresh: no reconcile needed
    world.rt._dispatch_once(instance, signal, CONID, frame)
    assert len(world.rt.signal_record.read(0, 10).signals) == 1


def test_a_failed_proof_for_the_same_build_before_first_open_gives_no_signal(world):      # review r2 #4
    reconcile(world, [authored_active(world.sandbox)])                   # build A proven, no session opened yet
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    world.query.answer("get_isolation_proof", proof(passed=False))       # A's proof withdrawn; no reconcile
    assert instance.on_prices(bars(3)) is None                          # the gate re-reads, so no first OPEN
    assert world.sandbox.sessions == [] and world.rt.signal_record.read(0, 10).signals == ()


def test_a_later_failing_proof_stops_the_running_instance_at_the_next_bar(world):         # review r2 #4
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    assert instance.on_prices(bars(3)) is not None                      # opened on the proven build
    world.query.answer("get_isolation_proof", proof(passed=False))       # the same build's proof is withdrawn
    assert instance.on_prices(bars(4)) is None and world.sandbox.open_sessions == []   # next bar re-reads, stops


def test_a_trader_that_refuses_the_proof_read_loads_nothing(world, caplog):
    world.query.answer("get_isolation_proof", TypedRpcRemoteError("PERMISSION_DENIED", "cli only"))
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {} and "PERMISSION_DENIED" in caplog.text


def test_an_unreachable_live_sandbox_waits_for_the_next_reconcile(world):
    world.sandbox.unavailable = True
    reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {}
    world.sandbox.unavailable = False
    reconcile(world, [authored_active(world.sandbox)])
    assert isinstance(instance_of(world), AuthoredLiveStrategy)


def test_a_fault_retires_once_and_the_file_never_reloads(world):
    deployment = authored_active(world.sandbox, "faults_above_limit", "FaultsAboveLimit", params={"LIMIT": 104.5})
    reconcile(world, [deployment])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    assert instance.on_prices(bars(10)) is None                  # the warm-up reaches close 105: a fault
    assert retirements(world) == []                              # nothing is sent on the tick thread
    assert not source_unchanged(instance)
    reconcile(world, [deployment])
    world.rt._authored_deployments.send_retirements()
    reconcile(world, [deployment])
    world.rt._authored_deployments.send_retirements()
    assert world.rt.ai_instances() == {}
    assert [(body["digest"], body["code"]) for body in retirements(world)] == [
        (deployment.strategy_digest, "SANDBOX_ERROR")]


def test_a_lost_retirement_is_sent_again_next_reconcile(world):
    world.command.answer("retire_authored_strategy", ConnectionError("trader down"), RETIRED)
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    for _ in range(3):
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 2


def test_a_final_refusal_stops_the_resend(world, caplog):
    world.command.answer("retire_authored_strategy", {"status": "REFUSED", "code": "AUTHORED_STATE_CONFLICT",
                                                      "detail": "not deployed", "retryable": False, "record": None})
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    with caplog.at_level(logging.ERROR):
        world.rt._authored_deployments.send_retirements()
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 1 and "AUTHORED_STATE_CONFLICT" in caplog.text


def test_reconcile_sends_pending_retirements(world):
    world.rt._config_mtime = None
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    world.rt._reconcile_sync()
    assert len(retirements(world)) == 1


def test_is_proven_reads_the_trader_every_call_and_never_caches(world):                   # review r2 #4
    deployments = world.rt._authored_deployments
    world.query.answer("get_isolation_proof", proof(), proof(passed=False))   # pass once, then fail (repeats)
    assert deployments.is_proven(IMAGE_DIGEST) is True
    assert deployments.is_proven(IMAGE_DIGEST) is False                  # a second call re-reads, not cached
    reads = [body for method, body in world.query.calls if method == "get_isolation_proof"]
    assert reads[-2:] == [{"image_digest": IMAGE_DIGEST}, {"image_digest": IMAGE_DIGEST}]


def test_unloading_closes_its_sessions(world):
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    instance.on_prices(bars(3))
    world.rt.unload_strategy(instance.name)
    assert world.sandbox.open_sessions == []


def test_a_catalogue_and_an_authored_deployment_load_side_by_side(world):
    catalogue = catalogue_active(world)
    reconcile(world, [catalogue, authored_active(world.sandbox)])
    assert set(world.rt.ai_instances()) == {ai_instance_name(CATALOGUE_VERSION), ai_instance_name(VERSION)}
    assert source_unchanged(instance_of(world, CATALOGUE_VERSION))


def test_proof_passes_needs_found_passed_and_the_same_image():
    assert proof_passes(proof(), IMAGE_DIGEST)
    assert not proof_passes({"found": True, "proof": {**proof()["proof"], "passed": "yes"}}, IMAGE_DIGEST)
    assert not proof_passes({"found": True, "proof": None}, IMAGE_DIGEST)
    assert not proof_passes({}, IMAGE_DIGEST)
    assert not proof_passes(None, IMAGE_DIGEST)                          # not a mapping: fail closed


# Task 10 rulings (controller, 2026-10-12)


def test_a_proof_read_that_times_out_is_false_and_alarms_once(world, caplog):            # ruling 1
    deployments = world.rt._authored_deployments
    world.query.answer("get_isolation_proof", TimeoutError("no reply in 2000ms"))
    with caplog.at_level(logging.ERROR):
        assert deployments.is_proven(IMAGE_DIGEST) is False
        assert deployments.is_proven(IMAGE_DIGEST) is False
    assert [r.getMessage() for r in caplog.records].count(caplog.records[0].getMessage()) == 1
    assert len([r for r in caplog.records if ISOLATION_ALARM in r.getMessage()]) == 1
    assert world.query.timeouts[-1] == ("get_isolation_proof", PROOF_TIMEOUT_SECONDS) and PROOF_TIMEOUT_SECONDS == 2.0
    world.query.answer("get_isolation_proof", proof())
    world.clock[0] += PROOF_OUTAGE_HOLD
    assert deployments.is_proven(IMAGE_DIGEST) is True                   # the episode ends on a pass


def test_the_proof_client_bounds_its_send_and_its_reply_by_the_proof_timeout(world):     # ruling 1
    world.rt._rpc_identity = make_identities()["strategy"]
    world.rt.trader_typed_address, world.rt.typed_query_port = "tcp://127.0.0.1", free_port()
    client = world.rt._new_proof_query_client()
    client.connect()
    try:
        assert client.timeout == PROOF_TIMEOUT_SECONDS
        assert client.socket.getsockopt(zmq.SNDTIMEO) == int(PROOF_TIMEOUT_SECONDS * 1000)
    finally:
        client.socket.close(linger=0)


def test_a_proof_read_that_breaks_unexpectedly_is_false_and_does_not_raise(world):
    world.query.answer("get_isolation_proof", RuntimeError("bug in the client"))
    assert world.rt._authored_deployments.is_proven(IMAGE_DIGEST) is False


def proof_reads(world):
    return len([method for method, _ in world.query.calls if method == "get_isolation_proof"])


@pytest.mark.parametrize("failure", [ConnectionError("down"), TimeoutError("slow"),
                                     TypedRpcRemoteError("INTERNAL_ERROR", "boom")])
def test_a_failed_proof_read_shuts_the_gate_without_asking_for_the_hold(world, failure):  # fix round 1
    assert PROOF_OUTAGE_HOLD == dt.timedelta(seconds=20)
    deployments = world.rt._authored_deployments
    world.query.answer("get_isolation_proof", failure, proof())
    assert deployments.is_proven(IMAGE_DIGEST) is False and proof_reads(world) == 1
    for _ in range(5):
        world.clock[0] += dt.timedelta(seconds=3)
        assert deployments.is_proven(IMAGE_DIGEST) is False
    assert proof_reads(world) == 1                                       # 15 s: no call reached the trader
    world.clock[0] += dt.timedelta(seconds=5)                            # 20 s after the failure
    assert deployments.is_proven(IMAGE_DIGEST) is True and proof_reads(world) == 2


@pytest.mark.parametrize("answer", [proof(), {"found": False, "proof": None}, proof(passed=False),
                                    proof(image="sha256:" + "f" * 64)])
def test_an_answer_is_never_cached(world, answer):                       # pass or definite answer: re-read
    deployments = world.rt._authored_deployments
    world.query.answer("get_isolation_proof", answer)
    first, second = deployments.is_proven(IMAGE_DIGEST), deployments.is_proven(IMAGE_DIGEST)
    assert first == second == (answer == proof()) and proof_reads(world) == 2


def test_a_retryable_refusal_is_resent_at_most_the_cap_then_one_error(world, caplog):    # ruling 2
    assert RETIRE_RESEND_CAP == 20
    digest = "sha256:" + "a" * 64
    world.command.answer("retire_authored_strategy", WITHDRAWAL_FAILED)
    world.rt._authored_deployments.request_retirement(digest, "SANDBOX_ERROR")
    with caplog.at_level(logging.ERROR):
        for _ in range(RETIRE_RESEND_CAP + 5):
            world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == RETIRE_RESEND_CAP
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and digest in errors[0] and "AUTHORED_WITHDRAWAL_FAILED" in errors[0]


def test_an_unreachable_trader_does_not_count_toward_the_cap(world):                      # ruling 2
    world.command.answer("retire_authored_strategy", ConnectionError("down"), TimeoutError("slow"),
                         OSError("reset"), WITHDRAWAL_FAILED)
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    for _ in range(RETIRE_RESEND_CAP + 10):
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 3 + RETIRE_RESEND_CAP


def test_a_store_outage_on_retire_does_not_count_toward_the_cap(world):                  # fix round 1
    digest = "sha256:" + "a" * 64
    outage = TypedRpcRemoteError(STORE_UNAVAILABLE, "journal locked")
    world.command.answer("retire_authored_strategy", *([outage] * 25), RETIRED)
    world.rt._authored_deployments.request_retirement(digest, "SANDBOX_ERROR")
    for _ in range(30):
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 26
    assert world.rt._authored_deployments._stopped(digest)
    world.rt._authored_deployments.request_retirement(digest, "SANDBOX_ERROR")   # retired: never queued again
    world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == 26


def test_the_store_outage_code_is_the_traders():
    from trader.messaging.authored_surface import STORE_UNAVAILABLE as TRADER_CODE
    assert STORE_UNAVAILABLE == TRADER_CODE


def test_a_remote_error_on_retire_counts_toward_the_cap(world):
    world.command.answer("retire_authored_strategy", TypedRpcRemoteError("PERMISSION_DENIED", "strategy only"))
    world.rt._authored_deployments.request_retirement("sha256:" + "a" * 64, "SANDBOX_ERROR")
    for _ in range(RETIRE_RESEND_CAP + 5):
        world.rt._authored_deployments.send_retirements()
    assert len(retirements(world)) == RETIRE_RESEND_CAP


def test_a_digest_given_up_after_the_cap_never_reloads(world):
    deployment = authored_active(world.sandbox, "faults_above_limit", "FaultsAboveLimit", params={"LIMIT": 104.5})
    world.command.answer("retire_authored_strategy", WITHDRAWAL_FAILED)
    reconcile(world, [deployment])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    instance.on_prices(bars(10))                                         # a fault
    for _ in range(RETIRE_RESEND_CAP + 1):
        world.rt._authored_deployments.send_retirements()
    reconcile(world, [deployment])
    reconcile(world, [deployment])
    assert world.rt.ai_instances() == {}


@pytest.mark.parametrize("breaks", ["repeated_conids", "ping_protocol_error"])
def test_an_authored_row_that_cannot_be_built_does_not_stop_a_catalogue_row(world, breaks, caplog):
    if breaks == "repeated_conids":                                     # the proxy refuses a repeated conid
        authored = authored_active(world.sandbox, conids=(CONID, CONID))
    else:
        authored = authored_active(world.sandbox)

        def protocol_error():
            raise SandboxProtocolError("bad PONG")
        world.sandbox.ping = protocol_error
    with caplog.at_level(logging.ERROR):
        reconcile(world, [authored, catalogue_active(world)])          # the authored row comes first
    assert set(world.rt.ai_instances()) == {ai_instance_name(CATALOGUE_VERSION)}
    assert ai_instance_name(VERSION) in caplog.text


def test_an_instance_dropped_after_a_failed_backfill_is_final(world):
    def no_history(instance):
        raise RuntimeError("IB down")
    world.rt._load_ai_history = no_history
    built = []
    build = world.rt._authored_deployments.build
    world.rt._authored_deployments.build = lambda *a, **k: built.append(build(*a, **k)) or built[-1]
    reconcile(world, [authored_active(world.sandbox)])
    assert world.rt.ai_instances() == {} and len(built) == 1
    built[0]._dispatch_conid = CONID
    assert built[0].on_prices(bars(3)) is None and world.sandbox.sessions == []


# Ruling 3: an authored instance in ERROR is re-enabled with a growing wait, not every reconcile.


def test_crash_backoff_doubles_and_is_capped_at_thirty_minutes():
    assert [crash_backoff(n).total_seconds() for n in (1, 2, 3)] == [60, 120, 240]
    assert crash_backoff(6).total_seconds() == 1800 and crash_backoff(50).total_seconds() == 1800


def test_an_authored_instance_that_keeps_crashing_is_re_enabled_with_a_growing_wait(world):
    clock = [dt.datetime(2024, 3, 4, 16, 0, tzinfo=dt.timezone.utc)]
    world.rt._authored_deployments = AuthoredDeployments(client=world.sandbox, trader_query=world.query,
                                                         trader_command=world.command, now=lambda: clock[0])
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    world.rt._maybe_check_exits = lambda *args: None
    frames = iter(bars(n) for n in range(3, 40))
    world.rt._strategy_frame = lambda conid, bar_size: next(frames)
    real_open = world.sandbox.open_session

    def broken_open(*args, **kwargs):
        raise RuntimeError("unexpected")

    def tick():
        world.rt._on_tick_for_strategy(instance, CONID)

    def after(seconds):
        clock[0] += dt.timedelta(seconds=seconds)
        world.rt._maybe_recover_from_instrument_error(instance)
        return instance.state

    world.sandbox.open_session = broken_open
    tick()
    assert instance.state == StrategyState.ERROR
    assert after(30) == StrategyState.ERROR                             # 60 s after the first crash
    assert after(31) == StrategyState.RUNNING
    tick()
    assert after(61) == StrategyState.ERROR                             # 120 s after the second
    assert after(60) == StrategyState.RUNNING
    world.sandbox.open_session = real_open
    tick()                                                               # a clean bar resets the wait
    assert instance.state == StrategyState.RUNNING
    world.sandbox.open_sessions[0].bar = broken_open                     # the open session now breaks
    tick()
    assert after(59) == StrategyState.ERROR and after(2) == StrategyState.RUNNING


def test_an_authored_instance_an_operator_disabled_is_not_re_enabled_after_an_error(world):
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    world.rt._load_enabled = lambda name: False
    instance.state = StrategyState.ERROR
    world.rt._maybe_recover_from_instrument_error(instance)
    assert instance.state == StrategyState.ERROR


# Final review minor: a signal is dispatched only while the build it was decided on still has a passing proof.


def test_a_signal_decided_on_a_proven_build_is_refused_once_the_proof_is_withdrawn(world):
    gaps = []
    world.rt.event_store = SimpleNamespace(append=gaps.append)
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    frame = bars(3)
    signal = instance.on_prices(frame)
    assert signal is not None
    world.query.answer("get_isolation_proof", proof(passed=False))      # withdrawn after the decision
    world.rt._signal_hold.dispatch((CONID, instance.name), instance, signal, CONID, frame)
    assert world.rt.signal_record.read(0, 10).signals == ()
    assert [event.metadata["reason"] for event in gaps] == [AUTHORED_UNPROVEN]
    assert instance.state == StrategyState.RUNNING                       # not the strategy's fault


def test_a_signal_with_no_open_session_is_refused(world):
    gaps = []
    world.rt.event_store = SimpleNamespace(append=gaps.append)
    reconcile(world, [authored_active(world.sandbox)])
    instance = instance_of(world)
    instance._dispatch_conid = CONID
    frame = bars(3)
    signal = instance.on_prices(frame)
    world.sandbox.open_sessions[0].close()
    instance._end_session()                                              # an outage on a later bar ended it
    world.rt._signal_hold.dispatch((CONID, instance.name), instance, signal, CONID, frame)
    assert world.rt.signal_record.read(0, 10).signals == ()
    assert [event.metadata["reason"] for event in gaps] == [AUTHORED_UNPROVEN]


# Final review I2: an authored bar runs on its instance's own thread, so the service loop never waits on the sandbox.

OTHER = 76792991


@pytest.fixture
def service_loop(world):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-service-loop", daemon=True)
    thread.start()
    world.rt._maybe_check_exits = lambda *args: None
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def on_loop(loop, fn, *args):
    """Runs ``fn`` on the service loop; the returned future answers once it has run there."""
    async def run():
        return fn(*args)
    return asyncio.run_coroutine_threadsafe(run(), loop)


def held_open(sandbox):
    """OPEN waits until released: the first bar of a session stands for a long warm-up or a slow proof read."""
    release, real_open = threading.Event(), sandbox.open_session

    def slow_open(*args, **kwargs):
        release.wait(timeout=10)
        return real_open(*args, **kwargs)
    sandbox.open_session = slow_open
    return release


def dispatched(world, count):
    """The signals that reached the signal hold, in order, and an Event set once ``count`` of them did."""
    seen, done, dispatch = [], threading.Event(), world.rt._signal_hold.dispatch

    def record(key, strategy, signal, conid, frame):
        seen.append((conid, signal.probability))
        dispatch(key, strategy, signal, conid, frame)
        if len(seen) >= count:
            done.set()
    world.rt._signal_hold.dispatch = record
    return seen, done


def test_a_slow_authored_bar_does_not_hold_the_service_loop(world, service_loop):
    reconcile(world, [catalogue_active(world), authored_active(world.sandbox)])
    authored, catalogue = instance_of(world), instance_of(world, CATALOGUE_VERSION)
    world.rt._strategy_frame = lambda conid, bar_size: bars(3)
    catalogue_bars, on_prices = [], catalogue.on_prices
    catalogue.on_prices = lambda frame: catalogue_bars.append(len(frame)) or on_prices(frame)
    seen, signalled = dispatched(world, 1)
    release = held_open(world.sandbox)
    try:
        on_loop(service_loop, world.rt._on_tick_for_strategy, authored, CONID).result(timeout=2)
        on_loop(service_loop, world.rt._on_tick_for_strategy, catalogue, CONID).result(timeout=2)
        assert catalogue_bars == [3]                                       # the catalogue bar ran meanwhile
        assert on_loop(service_loop, world.rt.disable_strategy, catalogue.name).result(timeout=2) == \
            StrategyState.DISABLED                                         # and so did a control call
        assert seen == []
    finally:
        release.set()
    assert signalled.wait(timeout=5) and seen == [(CONID, 3 / 10000)]     # the authored signal follows
    entries = world.rt.signal_record.read(0, 10).signals
    assert [(e.entry.action, e.entry.conid) for e in entries] == [("BUY", CONID)]


def test_the_bars_of_an_authored_instance_keep_their_order_and_none_runs_twice(world, service_loop):
    reconcile(world, [authored_active(world.sandbox, conids=(CONID, OTHER))])
    authored = instance_of(world)
    ticks = [(CONID, 3), (OTHER, 4), (CONID, 5), (OTHER, 6), (CONID, 7)]
    frames = iter(bars(count) for _, count in ticks)
    world.rt._strategy_frame = lambda conid, bar_size: next(frames)
    seen, signalled = dispatched(world, len(ticks))
    release = held_open(world.sandbox)
    try:
        for conid, _ in ticks:                                             # all queued behind the held OPEN
            on_loop(service_loop, world.rt._on_tick_for_strategy, authored, conid).result(timeout=2)
        assert seen == []
    finally:
        release.set()
    assert signalled.wait(timeout=5)
    assert seen == [(conid, count / 10000) for conid, count in ticks]
    session = world.sandbox.sessions[0]
    assert len(world.sandbox.sessions) == 1
    assert len(session.stamps_of(CONID)) == 7 and len(session.stamps_of(OTHER)) == 6   # each bar sent once


def test_unloading_drops_the_queued_bars_and_ends_the_worker(world, service_loop):
    reconcile(world, [authored_active(world.sandbox)])
    authored = instance_of(world)
    frames = iter(bars(count) for count in (3, 4, 5))
    world.rt._strategy_frame = lambda conid, bar_size: next(frames)
    release = held_open(world.sandbox)
    unloaded = threading.Thread(target=world.rt.unload_strategy, args=(authored.name,))
    try:
        for _ in range(3):
            on_loop(service_loop, world.rt._on_tick_for_strategy, authored, CONID).result(timeout=2)
        worker, stopped, stop = authored._worker._thread, threading.Event(), authored._worker.stop
        authored._worker.stop = lambda: (stop(), stopped.set())
        unloaded.start()                                                   # then waits for the bar holding the lock
        assert stopped.wait(timeout=2)
    finally:
        release.set()
    unloaded.join(timeout=5)
    worker.join(timeout=5)
    assert not unloaded.is_alive() and not worker.is_alive()
    assert len(world.sandbox.sessions) == 1 and len(world.sandbox.sessions[0].stamps) == 3   # only the first bar
    assert world.sandbox.open_sessions == []
    assert not authored.run_off_loop(lambda: None)                         # nothing runs after close


def test_a_bar_after_the_worker_stopped_is_dropped_never_run_on_the_loop(world, service_loop):  # final re-review
    reconcile(world, [authored_active(world.sandbox)])
    authored = instance_of(world)
    world.rt._strategy_frame = lambda conid, bar_size: bars(3)
    authored._worker.stop()                                     # an unload is under way on the reconcile thread
    called = []
    authored.on_prices = lambda prices: called.append(prices)
    on_loop(service_loop, world.rt._on_tick_for_strategy, authored, CONID).result(timeout=2)
    assert called == [] and world.sandbox.sessions == []        # nothing ran inline, nothing opened


def test_a_crash_on_the_worker_stops_the_bars_queued_behind_it(world, service_loop):
    reconcile(world, [authored_active(world.sandbox)])
    authored = instance_of(world)
    frames = iter(bars(count) for count in (3, 4))
    world.rt._strategy_frame = lambda conid, bar_size: next(frames)
    release, opens, idle = threading.Event(), [], threading.Event()

    def broken_open(*args, **kwargs):
        release.wait(timeout=10)
        opens.append(args)
        raise RuntimeError("unexpected")
    world.sandbox.open_session = broken_open
    try:
        for _ in range(2):
            on_loop(service_loop, world.rt._on_tick_for_strategy, authored, CONID).result(timeout=2)
        assert authored.run_off_loop(idle.set)
    finally:
        release.set()
    assert idle.wait(timeout=5)
    assert len(opens) == 1 and authored.state == StrategyState.ERROR    # the second bar did not reopen
    assert world.rt.signal_record.read(0, 10).signals == ()


def test_a_fault_callback_on_the_worker_may_close_the_sessions_without_a_deadlock(world, service_loop):
    reconcile(world, [authored_active(world.sandbox, "faults_above_limit", "FaultsAboveLimit",
                                      params={"LIMIT": 104.5})])
    authored = instance_of(world)
    world.rt._strategy_frame = lambda conid, bar_size: bars(10)           # close 109 > 104.5: a fault
    faults, reported = [], threading.Event()

    def close_from_the_worker(digest, code):
        authored.close_sessions()
        faults.append((threading.current_thread() is authored._worker._thread, code))
        reported.set()
    authored._on_fault = close_from_the_worker
    on_loop(service_loop, world.rt._on_tick_for_strategy, authored, CONID).result(timeout=2)
    assert reported.wait(timeout=5) and faults == [(True, "SANDBOX_ERROR")]
    authored._worker._thread.join(timeout=5)
    assert not authored._worker._thread.is_alive() and world.sandbox.open_sessions == []
