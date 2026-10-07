"""SP2 Plan 5 Task 11: spec 12 "Crash and leadership" 1, 3, 4, "Signed epoch" and "Durability" against SP1's
real coordinator over signed typed RPC. Crash test 2 (a real process restart) is Task 12."""
import asyncio
import json

import pytest

from tests.ai.runtime.fakes import write_cost_event
from tests.ai.runtime.scripted_engine import ScriptedEngine
from tests.ai.runtime.trader_world import TraderClock, TraderWorld, wait_for_heartbeat, write_service_config
from tests.rpc_identity_fixtures import make_identities, write_keyset
from tests.sp1_fixtures import LoopThread
from trader.ai.budget import Budget
from trader.ai.engine import SimulatedBaseline
from trader.ai.leadership import NotLeader
from trader.ai.model_client import Usage
from trader.ai.outbox import register_context_in_tx
from trader.ai.store import AiStore
from trader.ai_service import ServiceSettings, serve
from trader.automation.ai_paper_actions import PUBLISH_ACTION
from trader.messaging.typed_rpc import TypedRpcRemoteError


@pytest.fixture
def loop_thread():
    thread = LoopThread()
    yield thread
    thread.stop()


@pytest.fixture
def world(tmp_path, loop_thread, monkeypatch):
    created = TraderWorld(tmp_path, loop_thread, monkeypatch)
    yield created
    created.close()


@pytest.mark.asyncio
async def test_crash_with_the_journal_committed_and_the_send_not_started(world):              # spec 12 crash 1
    a = world.node()
    assert await a.leadership.acquire() == 1
    decision_id = await a.plan(world.enter())
    before = await a.submitter.get(decision_id)
    # the process dies here: nothing left the process
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    assert await b.submitter.recover() == 0
    await b.watch.refresh()
    await b.submitter.send_due()
    after = await b.submitter.get(decision_id)
    assert (after.body_sha256, after.created_epoch, after.last_epoch) == (before.body_sha256, 1, 2)
    assert after.state in ("ACCEPTED", "FINAL") and after.receipt_state == world.receipt(decision_id).state
    assert world.decision_row(decision_id).controller_epoch == 2
    replay = await b.clients.supervisor.call("submit_ai_paper_decision", json.loads(after.body_json))
    assert replay["command_id"] == f"aip-{decision_id}"           # same id and body: a replay, never a conflict
    world.settle()
    assert len(world.entries()) == 1 and world.protected()


@pytest.mark.asyncio
async def test_takeover_around_a_lost_submit_reply(world):                                      # spec 12 crash 3
    a = world.node(flaky=True)
    a.sockets["supervisor_command"].script("submit_ai_paper_decision", "lose_reply")
    assert await a.leadership.acquire() == 1
    await a.watch.refresh()
    decision_id = await a.plan(world.enter())
    await a.submitter.send_due()
    assert (await a.submitter.get(decision_id)).state == "UNKNOWN"
    assert world.receipt(decision_id) is not None                  # the trader did accept it
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    await b.submitter.reconcile_once()                             # by the original id, under epoch 2
    row = await b.submitter.get(decision_id)
    assert row.state in ("ACCEPTED", "FINAL") and row.receipt_state == world.receipt(decision_id).state
    assert (row.attempts, row.last_epoch) == (1, 1)                 # recovered, never resent
    world.settle()                                                 # accepted trader work went on through the takeover
    assert len(world.entries()) == 1 and world.protected()
    assert a.leadership.current_epoch() is None
    with pytest.raises(NotLeader):
        await a.leadership.grant_once()


@pytest.mark.asyncio
async def test_stale_controller_is_refused_by_its_epoch(world):                                # spec 12 crash 4
    a = world.node(clock=TraderClock(world.served, frozen_monotonic=True))     # paused: its lease looks alive
    assert await a.leadership.acquire() == 1
    await a.watch.refresh()
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    decision_id = await a.plan(world.enter())
    await a.submitter.send_due()
    row = await a.submitter.get(decision_id)
    assert (row.state, row.error_code) == ("PENDING", "CONTROLLER_EPOCH_STALE")
    assert a.leadership.current_epoch() is None and world.receipt(decision_id) is None   # no ledger row
    await b.watch.refresh()
    await b.submitter.send_due()                                   # the successor sends the same id and body
    row = await b.submitter.get(decision_id)
    assert row.state in ("ACCEPTED", "FINAL") and row.last_epoch == 2
    assert world.decision_row(decision_id).controller_epoch == 2
    world.settle()
    assert len(world.entries()) == 1 and world.protected()


@pytest.mark.asyncio
async def test_a_missing_or_altered_epoch_is_refused(world):                                  # spec 12 signed epoch
    a = world.node()
    await a.leadership.acquire()
    decision_id = await a.plan(world.enter())
    body = json.loads((await a.submitter.get(decision_id)).body_json)
    sockets = world.served.sockets
    with pytest.raises(TypedRpcRemoteError) as exc:
        sockets.client("ai_supervisor", "trader", "command").call("submit_ai_paper_decision", body, dict)
    assert exc.value.code == "CONTROLLER_EPOCH_MISSING"
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    stale = sockets.signed("ai_supervisor", role="command", method="submit_ai_paper_decision", body=body,
                           controller_epoch=1)
    assert sockets.raw_code(stale.model_copy(update={"controller_epoch": 2})) == "AUTHENTICATION_ERROR"
    assert sockets.raw_code(stale) == "CONTROLLER_EPOCH_STALE"
    assert world.receipt(decision_id) is None


@pytest.mark.asyncio
async def test_outbox_delivers_after_a_trader_outage_without_duplicates(world):           # spec 12 durability
    node = world.node(flaky=True)
    world.served.advance(1)
    now, experiment_id, context = node.clock.now(), world.served.experiment_id, "sig-" + "5" * 32
    await node.store.atransaction(lambda conn: register_context_in_tx(
        conn, context_key=context, experiment_id=experiment_id, served_kind="signal", served_id=context, now=now))
    no_trade = SimulatedBaseline("no_trade.v1", "self_found", "cyc-entry-20260717-1100", now)
    await node.store.atransaction(lambda conn: node.outbox.enqueue_simulated_in_tx(
        conn, experiment_id=experiment_id, baseline=no_trade, wait_for_decision_id=None, now=now))
    write_cost_event(node.store, request_key=f"{context}/jev/1", kind="CONFIRMED", cost_micros=15_000,
                     usage=Usage(1000, 200), now=now)
    await node.outbox.pump_costs()
    command = node.sockets["supervisor_command"]
    command.script("record_simulated_decision", "lose_reply")
    command.script("record_ai_cost", "down", "lose_reply")
    for _ in range(4):
        await node.outbox.deliver_due()
        world.served.advance(20)                                   # past every backoff
    assert await node.outbox.counts() == {"waiting": 0, "pending": 0, "dropped": 0, "delivered": 2,
                                            "dead": 0}
    report = world.served.call("cli", "get_scoreboard", {})
    assert report["benchmarks"]["ai_cost"]["calls"] == 1
    books = [b for b in report["benchmarks"]["books"] if b["baseline_id"] == "no_trade.v1"]
    assert len(books) == 1 and books[0]["records"] == 1


@pytest.fixture
def keyed_world(tmp_path, loop_thread, monkeypatch):
    keys = tmp_path / "keys"
    created = TraderWorld(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=write_keyset(keys)))
    created.keys_dir = keys
    yield created
    created.close()


@pytest.mark.asyncio
async def test_the_ai_service_never_publishes_policy_on_start_or_restart(keyed_world, tmp_path):   # spec 6.7
    world, heartbeat = keyed_world, tmp_path / "hb.json"
    settings = ServiceSettings(config_path=str(write_service_config(tmp_path, world, heartbeat)),
                               keys_dir=str(world.keys_dir), trader_address="tcp://127.0.0.1")
    for epoch in (1, 2):                                           # a start, then a restart as a new holder
        stop = asyncio.Event()
        task = asyncio.create_task(serve(settings, engine_factory=lambda deps: ScriptedEngine(), stop=stop,
                                         clock=TraderClock(world.served, real_sleep=True),
                                         environ={"OPENROUTER_API_KEY": "test-only-not-a-key"}))
        await wait_for_heartbeat(heartbeat, lambda status, e=epoch: status["epoch"] == e, task)
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        world.advance(61)
    sources = world.served.trader.journal_db.execute(
        "SELECT source FROM command_ledger WHERE action = ?", [PUBLISH_ACTION], fetch="all")
    assert sources == [("cli",)]                                   # only the operator's initial policy


@pytest.fixture
def capped_world(tmp_path, loop_thread, monkeypatch):
    keys = tmp_path / "keys"
    created = TraderWorld(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=write_keyset(keys)),
                          model_budget_usd_per_day=1500.0)   # the trader read its trader.yaml ai_paper section
    created.keys_dir = keys
    yield created
    created.close()


@pytest.mark.asyncio
async def test_the_owner_cap_is_read_from_trader_yaml_over_signed_rpc(capped_world, tmp_path):     # Ruling 19
    world, heartbeat = capped_world, tmp_path / "hb.json"
    settings = ServiceSettings(config_path=str(write_service_config(tmp_path, world, heartbeat)),
                               keys_dir=str(world.keys_dir), trader_address="tcp://127.0.0.1")
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, engine_factory=lambda deps: ScriptedEngine(), stop=stop,
                                     clock=TraderClock(world.served, real_sleep=True),
                                     environ={"OPENROUTER_API_KEY": "test-only-not-a-key"}))
    await wait_for_heartbeat(heartbeat, lambda status: status.get("budget_cap_ready") is True, task)
    stop.set()
    await asyncio.wait_for(task, timeout=15)
    snapshot = await Budget(AiStore(world.ai_db, clock=TraderClock(world.served)),
                            TraderClock(world.served), calls_per_hour=120).snapshot()
    assert snapshot.effective_cap_micros == 1_500_000_000
