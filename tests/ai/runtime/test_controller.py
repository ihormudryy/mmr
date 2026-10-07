"""SP2 Plan 5 Task 9: the controller schedules, dispatches and commits; the engine only judges (spec 5.2, 5.5)."""
import asyncio
import datetime as dt
import json
import logging

import pytest
import pytest_asyncio

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, FRIDAY, MSFT, FakeGateway, FakeLeadership, FakeTrader, et, open_trip
from tests.ai.runtime.scripted_engine import ScriptedEngine
from tests.ai.world import World, request
from trader.ai.budget_cap import BudgetCapSync, CapGatedGateway
from trader.ai.config import ControllerConfig
from trader.ai.controller import EXIT_WAIT_ALERT_EVERY, EXIT_WAIT_OVERRUN, AiController, ExperimentWatch
from trader.ai.engine import EngineResult, ProposedDecision, SimulatedBaseline
from trader.ai.gateway import CallRefused
from trader.ai.ids import derive_decision_id
from trader.ai.journal import AttemptJournal
from trader.ai.leadership import Leadership, new_holder_id
from trader.ai.outbox import ReportingOutbox
from trader.ai.rpc_clients import RpcNotSent, RpcRefused
from trader.ai.runtime_schema import ALL_MIGRATIONS, set_cursor_in_tx
from trader.ai.schedule import Slot, SessionSlots
from trader.ai.signal_intake import SignalIntake
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter
from trader.scoreboard.ingest_models import RecordSimulatedDecisionRequest


def enter(conid=AAPL):
    return ProposedDecision(action_key=f"enter:{conid}", action="ENTER", conid=conid, side="BUY", decider="jev",
                            evidence_digest="sha256:" + "c" * 64, deployment_digest="sha256:" + "a" * 64,
                            policy_revision=1, stop_price=225.4, target_price=234.6, quantity=3)


def close(conid=AAPL):
    return ProposedDecision(action_key=f"close:{conid}", action="CLOSE", conid=conid, side="SELL",
                            decider="orchestrator", evidence_digest="sha256:" + "d" * 64)


class Rig:
    def __init__(self, tmp_path, at):
        self.tmp_path, self.clock = tmp_path, FakeClock(at)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.trader, self.leadership, self.engine = FakeTrader(), FakeLeadership(1), ScriptedEngine()
        self.build()

    def build(self):
        self.watch = ExperimentWatch(self.trader)
        slots = SessionSlots()
        self.submitter = Submitter(store=self.store, supervisor=self.trader, leadership=self.leadership,
                                   clock=self.clock, slots=slots, experiment_state=self.watch.state)
        self.intake = SignalIntake(store=self.store, supervisor=self.trader, clock=self.clock)
        self.controller = AiController(
            config=ControllerConfig(heartbeat_path=str(self.tmp_path / "hb.json")), store=self.store,
            clock=self.clock, supervisor=self.trader, leadership=self.leadership, watch=self.watch,
            submitter=self.submitter, outbox=ReportingOutbox(store=self.store, journal=AttemptJournal(self.store),
                                                             supervisor=self.trader, clock=self.clock),
            intake=self.intake, slots=slots, engine=self.engine, gateway=FakeGateway(self.clock))

    def sent(self):
        return [json.loads(body) for body, _ in self.trader.decisions.sent]

    def cycles(self):
        return self.store.db.execute("SELECT cycle_id, state, reason FROM ai_cycles ORDER BY cycle_id", fetch="all")

    def opportunity(self, signal):
        return self.store.db.execute("SELECT state, reason FROM ai_opportunities WHERE opportunity_id = ?",
                                     [signal["source_event_id"]], fetch="one")

    async def signals_then_drain(self):
        await self.controller.tick_signals()
        await self.controller.drain()

    async def slots_then_drain(self):
        await self.controller.run_due_slots()
        await self.controller.drain()


async def rig_at(tmp_path, at, **trader):
    rig = Rig(tmp_path, at)
    for name, value in trader.items():
        setattr(rig.trader, name, value)
    await rig.controller.start()
    return rig


@pytest_asyncio.fixture
async def rig(tmp_path):
    return await rig_at(tmp_path, et(11, 0, 30))


@pytest.mark.asyncio
async def test_an_entry_signal_submits_one_enter_and_records_its_baseline(rig):
    s = rig.trader.signals.add()
    rig.engine.results["entry_signal"] = lambda ctx: EngineResult(decisions=(enter(),), baselines=(
        SimulatedBaseline("follow_signal.v1", "strategy_signal", ctx.opportunity.opportunity_id, ctx.now,
                          conid=AAPL, side="BUY", reference_price=230.0, stop_price=225.4, target_price=234.6,
                          deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}"),))
    await rig.signals_then_drain()
    decision_id = derive_decision_id(s["source_event_id"], f"enter:{AAPL}")
    assert [b["decision_id"] for b in rig.sent()] == [decision_id]
    assert rig.opportunity(s) == ("DECIDED", None)
    await rig.controller.report_once()
    (baseline,) = rig.trader.ingest.rows.values()
    assert baseline["linked_decision_id"] == decision_id and baseline["opportunity_id"] == s["source_event_id"]


@pytest.mark.asyncio
async def test_an_exit_signal_goes_to_the_exit_hook_only(rig):
    rig.trader.signals.add(action="SELL")
    rig.engine.results["exit_signal"] = EngineResult(decisions=(close(),))
    await rig.signals_then_drain()
    assert rig.engine.hooks_called() == ["exit_signal"] and [b["action"] for b in rig.sent()] == ["CLOSE"]


@pytest.mark.asyncio
async def test_a_redelivered_signal_is_judged_once(rig):
    rig.trader.signals.add()
    await rig.signals_then_drain()
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 0, rig.clock.now()))
    await rig.signals_then_drain()
    assert rig.engine.hooks_called() == ["entry_signal"]


@pytest.mark.asyncio
async def test_a_multi_action_cycle_gets_one_derived_id_per_action(rig):
    rig.engine.results["entry_cycle"] = EngineResult(decisions=(enter(AAPL), enter(MSFT)))
    await rig.slots_then_drain()
    cycle = "cyc-entry-20260717-1100"
    assert sorted(b["decision_id"] for b in rig.sent()) == sorted(
        [derive_decision_id(cycle, f"enter:{AAPL}"), derive_decision_id(cycle, f"enter:{MSFT}")])
    assert rig.cycles() == [(cycle, "DONE", None), ("cyc-position-20260717-1100", "SKIPPED", "NO_OWNED_POSITIONS")]


@pytest.mark.asyncio
async def test_an_engine_cannot_enter_from_a_position_cycle(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), trips=[open_trip()])
    rig.engine.results["position_cycle"] = EngineResult(decisions=(enter(),))
    await rig.slots_then_drain()
    assert ("cyc-position-20260717-1100", "FAILED", "ACTION_NOT_ALLOWED_HERE") in rig.cycles()
    assert rig.sent() == []                                   # the entry cycle of the same slot proposed nothing


@pytest.mark.asyncio
async def test_a_position_cycle_closes_after_the_entry_cutoff(tmp_path):
    rig = await rig_at(tmp_path, et(15, 30, 10), trips=[open_trip()])
    rig.engine.results["position_cycle"] = EngineResult(decisions=(close(),))
    await rig.slots_then_drain()
    assert rig.engine.hooks_called() == ["position_cycle"]
    assert [b["action"] for b in rig.sent()] == ["CLOSE"]
    assert rig.cycles() == [("cyc-entry-20260717-1515", "MISSED", "LATE_START"),
                            ("cyc-position-20260717-1530", "DONE", None)]
    (_, ctx), = rig.engine.calls
    assert [p.conid for p in ctx.positions] == [AAPL]


@pytest.mark.asyncio
async def test_position_cycles_run_while_paused_and_skip_without_positions(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), state="PAUSED")
    await rig.slots_then_drain()
    assert rig.cycles() == [("cyc-entry-20260717-1100", "SKIPPED", "EXPERIMENT_PAUSED"),
                            ("cyc-position-20260717-1100", "SKIPPED", "NO_OWNED_POSITIONS")]
    rig.trader.trips = [open_trip()]
    rig.clock.advance(15 * 60)
    await rig.slots_then_drain()
    assert rig.engine.hooks_called() == ["position_cycle"]


@pytest.mark.asyncio
async def test_killed_runs_no_cycles_but_an_exit_signal_still_closes(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), state="KILLED", trips=[open_trip()])
    buy, sell = rig.trader.signals.add(), rig.trader.signals.add(action="SELL")
    rig.engine.results["exit_signal"] = EngineResult(decisions=(close(),))
    await rig.slots_then_drain()
    await rig.signals_then_drain()
    assert {state for _, state, _ in rig.cycles()} == {"SKIPPED"}
    assert rig.opportunity(buy) == ("MISSED", "EXPERIMENT_KILLED") and rig.opportunity(sell) == ("DECIDED", None)
    assert [b["action"] for b in rig.sent()] == ["CLOSE"]                  # the trader joins it to the kill flatten


@pytest.mark.asyncio
async def test_stopped_admits_nothing_new(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), state="STOPPED", trips=[open_trip()])
    buy, sell = rig.trader.signals.add(), rig.trader.signals.add(action="SELL")
    await rig.slots_then_drain()
    await rig.signals_then_drain()
    assert rig.engine.calls == [] and rig.sent() == []
    assert rig.opportunity(buy) == ("MISSED", "EXPERIMENT_STOPPED") == rig.opportunity(sell)


@pytest.mark.asyncio
async def test_a_missed_slot_is_recorded_and_never_replayed(tmp_path):
    rig = await rig_at(tmp_path, et(11, 3))
    await rig.slots_then_drain()
    assert rig.cycles()[0] == ("cyc-entry-20260717-1100", "MISSED", "LATE_START")
    rig.clock.advance(12 * 60 + 5)                                          # 11:15:05
    await rig.slots_then_drain()
    assert [ctx.slot.cycle_id for hook, ctx in rig.engine.calls if hook == "entry_cycle"] == \
        ["cyc-entry-20260717-1115"]


@pytest.mark.asyncio
async def test_slow_model_work_never_blocks_receipts_or_reconciliation(rig):
    now = rig.clock.now()
    decision_id = await rig.store.atransaction(lambda conn: rig.submitter.insert_in_tx(
        conn, source_kind="entry_signal", source_id="sig-" + "2" * 32, decision=enter(),
        expires_at=now + dt.timedelta(minutes=5), epoch=1, now=now))
    rig.store.db.execute("UPDATE ai_submissions SET state = 'UNKNOWN', last_sent_at = ? WHERE decision_id = ?",
                         [now, decision_id])
    rig.trader.decisions.ledger[decision_id] = {"command_id": f"aip-{decision_id}", "correlation_id": "c",
                                                "state": "SUBMITTED", "outcome": None, "error_code": None,
                                                "retryable": False}
    rig.engine.block = asyncio.Event()
    await rig.controller.run_due_slots()                                    # the entry cycle now waits on the model
    await asyncio.wait_for(rig.controller.reconcile_once(), timeout=2)
    await asyncio.wait_for(rig.controller.tick_signals(), timeout=2)
    assert (await rig.submitter.get(decision_id)).state == "ACCEPTED"
    rig.engine.block.set()
    await rig.controller.drain()


@pytest.mark.asyncio
async def test_a_cycle_is_cut_at_its_slot_deadline(rig):
    rig.engine.block = asyncio.Event()
    rig.engine.results["entry_cycle"] = EngineResult(decisions=(enter(),))
    slot = Slot("entry", FRIDAY, et(11, 0), rig.clock.now() + dt.timedelta(seconds=0.05))
    await rig.controller.record_cycle(slot, "RUNNING", None)
    await rig.controller.run_cycle(slot)
    assert rig.cycles() == [("cyc-entry-20260717-1100", "TIMED_OUT", "SLOT_DEADLINE")] and rig.sent() == []


@pytest.mark.asyncio
async def test_an_unfinished_opportunity_is_judged_again_if_fresh_else_missed(rig):
    fresh, stale = rig.trader.signals.add(at=et(10, 58)), rig.trader.signals.add(at=et(10, 50))
    await rig.intake.poll()
    for s in (fresh, stale):
        await rig.intake.mark(s["source_event_id"], "IN_PROGRESS", None)          # the process died while judging
    rig.build()
    await rig.controller.start()
    await rig.controller.dispatch_opportunities()
    await rig.controller.drain()
    assert rig.opportunity(fresh) == ("DECIDED", None) and rig.opportunity(stale) == ("MISSED", "STALE")


@pytest.mark.asyncio
async def test_a_buy_outside_the_entry_window_is_missed(tmp_path):
    rig = await rig_at(tmp_path, et(15, 31))
    buy = rig.trader.signals.add(at=et(15, 31))
    await rig.signals_then_drain()
    assert rig.opportunity(buy) == ("MISSED", "OUTSIDE_ENTRY_WINDOW") and rig.engine.calls == []


@pytest.mark.asyncio
async def test_the_cost_context_exists_before_the_hook_runs(rig):
    seen = []

    def check(ctx):
        seen.append(rig.store.db.execute("SELECT experiment_id, served_kind, served_id FROM ai_call_contexts "
                                         "WHERE context_key = ?", [ctx.work.context_key], fetch="one"))
        return EngineResult()
    s = rig.trader.signals.add()
    rig.engine.results["entry_signal"] = check
    await rig.signals_then_drain()
    assert seen == [(rig.watch.view.experiment_id, "signal", s["source_event_id"])]


@pytest.mark.asyncio
async def test_an_unknown_experiment_is_never_treated_as_no_experiment(rig):
    rig.trader.experiment_down = True
    await rig.controller.refresh_experiment()
    buy = rig.trader.signals.add()
    await rig.signals_then_drain()
    assert rig.opportunity(buy) == ("NEW", None) and rig.engine.calls == []


@pytest.mark.asyncio
async def test_an_engine_error_fails_only_that_opportunity(rig):
    rig.engine.error = RuntimeError("bug")
    buy = rig.trader.signals.add()
    await rig.signals_then_drain()
    assert rig.opportunity(buy) == ("FAILED", "ENGINE_ERROR") and rig.sent() == []


@pytest.mark.asyncio
async def test_the_heartbeat_reports_leadership_and_open_work(rig, tmp_path):
    status = await rig.controller.heartbeat()
    written = json.loads((tmp_path / "hb.json").read_text())
    assert written == status and (status["epoch"], status["unsettled_submissions"]) == (1, 0)


class JevFirstEngine:
    """Asks Jev before any ENTER; records the refusal and still returns the follow-signal baseline."""

    def __init__(self):
        self.refusals = []

    async def on_entry_signal(self, ctx):
        work = await ctx.work.for_action(f"enter:{AAPL}")
        try:
            await work.gateway.call("jev", request(work.request_key("jev", 1)), work.deadline)
        except CallRefused as refused:
            self.refusals.append(refused.code)
        return EngineResult(baselines=(SimulatedBaseline(
            "follow_signal.v1", "strategy_signal", ctx.opportunity.opportunity_id, ctx.now, conid=AAPL,
            deployment_digest="sha256:" + "a" * 64, incomplete_reason="budget_refused"),), note="BUDGET_CAP_UNKNOWN")

    async def on_exit_signal(self, ctx):
        return EngineResult(decisions=(close(),))

    async def on_entry_cycle(self, ctx):
        return EngineResult()

    async def on_position_cycle(self, ctx):
        return EngineResult()


class FailingCapTrader:
    async def call(self, method, body, *, epoch=None):
        assert method == "get_ai_model_budget"
        raise RpcNotSent("TRADER_UNREACHABLE")


@pytest.mark.asyncio
async def test_a_closed_cap_gate_keeps_reconciliation_and_the_outbox_running(tmp_path):
    rig = Rig(tmp_path, et(11, 0, 30))
    world = World(tmp_path, rig.clock)
    await world.gateway.start()
    cap = BudgetCapSync(supervisor=FailingCapTrader(), budget=world.gateway.budget, clock=rig.clock)
    engine = JevFirstEngine()
    rig.controller = AiController(
        config=ControllerConfig(heartbeat_path=str(tmp_path / "hb.json")), store=rig.store, clock=rig.clock,
        supervisor=rig.trader, leadership=rig.leadership, watch=rig.watch, submitter=rig.submitter,
        outbox=ReportingOutbox(store=rig.store, journal=AttemptJournal(rig.store), supervisor=rig.trader,
                               clock=rig.clock),
        intake=rig.intake, slots=SessionSlots(), engine=engine, gateway=CapGatedGateway(world.gateway, cap),
        cap_sync=cap)
    await rig.controller.start()
    assert not cap.ready() and cap.last_error == "TRADER_UNREACHABLE"

    now = rig.clock.now()
    earlier = await rig.store.atransaction(lambda conn: rig.submitter.insert_in_tx(
        conn, source_kind="entry_signal", source_id="sig-" + "2" * 32, decision=enter(),
        expires_at=now + dt.timedelta(minutes=5), epoch=1, now=now))
    rig.store.db.execute("UPDATE ai_submissions SET state = 'UNKNOWN', last_sent_at = ? WHERE decision_id = ?",
                         [now, earlier])
    rig.trader.decisions.ledger[earlier] = {"command_id": f"aip-{earlier}", "correlation_id": "c",
                                            "state": "SUBMITTED", "outcome": None, "error_code": None,
                                            "retryable": False}
    buy, sell = rig.trader.signals.add(), rig.trader.signals.add(action="SELL")
    await rig.signals_then_drain()
    assert engine.refusals == ["BUDGET_CAP_UNKNOWN"] and world.jev.requests == []
    assert world.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)]
    assert [b["action"] for b in rig.sent()] == ["CLOSE"]                 # an exit signal's close still goes out
    assert rig.opportunity(buy) == ("DECIDED", "BUDGET_CAP_UNKNOWN") and rig.opportunity(sell) == ("DECIDED", None)

    await rig.controller.reconcile_once()                                 # reconciliation needs no cap
    assert (await rig.submitter.get(earlier)).state == "ACCEPTED" and (earlier, 1) in rig.trader.decisions.reads
    await rig.controller.report_once()                                    # nor does the outbox
    (baseline,) = rig.trader.ingest.rows.values()
    assert (baseline["opportunity_id"], baseline["incomplete_reason"]) == (buy["source_event_id"], "budget_refused")
    RecordSimulatedDecisionRequest.model_validate(baseline)


class GrantThenRefuse:
    """The first grant succeeds; every later one is refused as a broken key would be (not a lost lease)."""

    def __init__(self, clock):
        self.clock, self.grants = clock, 0

    async def call(self, method, body, *, epoch=None):
        assert method == "grant_ai_controller_epoch"
        self.grants += 1
        if self.grants > 1:
            raise RpcRefused("PERMISSION_DENIED", "the trader no longer accepts this key")
        expires = self.clock.now() + dt.timedelta(seconds=60)
        return {"epoch": 1, "lease_expires_at": expires.isoformat()}


@pytest.mark.asyncio
async def test_a_dead_renewal_loop_stops_the_service_loudly(rig, tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="trader.ai.controller")
    leadership = Leadership(supervisor=GrantThenRefuse(rig.clock), store=rig.store, clock=rig.clock,
                            holder_id=new_holder_id())
    assert await leadership.grant_once() == 1
    controller = AiController(
        config=ControllerConfig(heartbeat_path=str(tmp_path / "hb.json")), store=rig.store, clock=rig.clock,
        supervisor=rig.trader, leadership=leadership, watch=rig.watch, submitter=rig.submitter,
        outbox=ReportingOutbox(store=rig.store, journal=AttemptJournal(rig.store), supervisor=rig.trader,
                               clock=rig.clock),
        intake=rig.intake, slots=SessionSlots(), engine=rig.engine, gateway=FakeGateway(rig.clock))
    stop = asyncio.Event()
    await asyncio.wait_for(controller.run(stop), timeout=10)        # without the fix it never returns
    assert stop.is_set() and "PERMISSION_DENIED" in caplog.text


def submissions(rig):
    return rig.store.db.execute("SELECT count(*) FROM ai_submissions", fetch="one")[0]


@pytest.mark.asyncio
async def test_a_hook_that_returns_after_the_slot_deadline_persists_nothing(rig):   # PR #84 thread 4210304317
    def late(ctx):
        rig.clock.advance(16 * 60)                                  # the answer arrives at 11:16:30, after 11:15
        return EngineResult(decisions=(enter(),))
    rig.engine.results["entry_cycle"] = late
    await rig.slots_then_drain()
    assert ("cyc-entry-20260717-1100", "TIMED_OUT", "SLOT_DEADLINE") in rig.cycles()
    assert rig.sent() == [] and submissions(rig) == 0


@pytest.mark.asyncio
async def test_the_deadline_is_checked_again_right_before_the_persist(rig, monkeypatch):
    import trader.ai.controller as controller_module
    real = controller_module.validate_result

    def slow_check(source_kind, result):
        rig.clock.advance(16 * 60)                                  # time passes between the result and the commit
        return real(source_kind, result)
    monkeypatch.setattr(controller_module, "validate_result", slow_check)
    rig.engine.results["entry_cycle"] = EngineResult(decisions=(enter(),))
    await rig.slots_then_drain()
    assert ("cyc-entry-20260717-1100", "TIMED_OUT", "SLOT_DEADLINE") in rig.cycles()
    assert rig.sent() == [] and submissions(rig) == 0


@pytest.mark.asyncio
async def test_an_exit_that_waits_for_its_entry_is_kept_past_the_signal_age(rig):   # PR #86 thread 4211394337
    sell = rig.trader.signals.add(action="SELL")
    until = rig.clock.now() + dt.timedelta(hours=1)
    answers = [EngineResult(note="EXIT_WAITING_FOR_ENTRY", wait_until=until)] * 2 + [EngineResult(decisions=(close(),))]
    rig.engine.results["exit_signal"] = lambda ctx: answers.pop(0)
    await rig.signals_then_drain()
    assert rig.opportunity(sell) == ("IN_PROGRESS", "EXIT_WAITING_FOR_ENTRY") and rig.sent() == []
    rig.clock.advance(400)                                          # older than signal_max_age_seconds (300)
    await rig.signals_then_drain()
    assert rig.opportunity(sell) == ("IN_PROGRESS", "EXIT_WAITING_FOR_ENTRY") and rig.sent() == []
    await rig.signals_then_drain()                                  # the entry filled: the engine closes now
    assert rig.opportunity(sell)[0] == "DECIDED" and [b["action"] for b in rig.sent()] == ["CLOSE"]
    await rig.signals_then_drain()
    assert rig.engine.hooks_called() == ["exit_signal"] * 3 and len(rig.sent()) == 1


@pytest.mark.asyncio
async def test_a_stuck_exit_wait_is_a_loud_incident_and_stays_pending(rig, caplog):  # PR #86 4212341131
    sell = rig.trader.signals.add(action="SELL")
    rig.engine.results["exit_signal"] = EngineResult(note="EXIT_WAITING_FOR_ENTRY",
                                                     wait_until=rig.clock.now() + dt.timedelta(minutes=5))
    await rig.signals_then_drain()
    rig.clock.advance(5 * 60 + EXIT_WAIT_OVERRUN.total_seconds() + 1)
    caplog.set_level(logging.ERROR, logger="trader.ai.controller")
    await rig.signals_then_drain()
    await rig.signals_then_drain()
    # PR #86 4212341131: the backstop only escalates; the exit stays pending and is judged again.
    assert rig.opportunity(sell) == ("IN_PROGRESS", "EXIT_WAITING_FOR_ENTRY") and rig.sent() == []
    assert caplog.text.count("EXIT_WAIT_STUCK") == 1 and sell["source_event_id"] in caplog.text
    assert (await rig.controller.heartbeat())["exit_waits_stuck"] == 1
    rig.clock.advance(EXIT_WAIT_ALERT_EVERY.total_seconds())
    await rig.signals_then_drain()
    assert caplog.text.count("EXIT_WAIT_STUCK") == 2                 # at most once per hour
    assert rig.store.db.execute("SELECT alerts FROM ai_exit_waits", fetch="one") == (2,)
    rig.engine.results["exit_signal"] = EngineResult(decisions=(close(),))      # the entry filled after all
    await rig.signals_then_drain()
    assert rig.opportunity(sell)[0] == "DECIDED" and [b["action"] for b in rig.sent()] == ["CLOSE"]
    assert (await rig.controller.heartbeat())["exit_waits_stuck"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["entry_signal", "entry_cycle"])
async def test_only_an_exit_signal_may_wait(source):
    from trader.ai.controller import validate_result
    waiting = EngineResult(wait_until=et(12, 0))
    assert validate_result(source, waiting) == "WAIT_NOT_ALLOWED_HERE"
    assert validate_result("exit_signal", EngineResult(decisions=(close(),), wait_until=et(12, 0))) == \
        "WAIT_NOT_ALLOWED_HERE"


def cycle_states(rig):
    return {cycle: (state, reason) for cycle, state, reason in rig.cycles()}


def entry_cycles_run(rig):
    return [ctx.slot.cycle_id for hook, ctx in rig.engine.calls if hook == "entry_cycle"]


@pytest.mark.asyncio
async def test_a_multi_slot_jump_journals_every_elapsed_slot_as_missed(rig):        # PR #84 thread 4210304802
    await rig.slots_then_drain()                                    # 11:00 runs
    rig.clock.advance(46 * 60)                                      # 11:46:30: 11:15 and 11:30 were never seen
    await rig.slots_then_drain()
    states = cycle_states(rig)
    for hhmm in ("1115", "1130"):
        assert states[f"cyc-entry-20260717-{hhmm}"] == ("MISSED", "LATE_START")
        assert states[f"cyc-position-20260717-{hhmm}"] == ("MISSED", "LATE_START")
    assert entry_cycles_run(rig) == ["cyc-entry-20260717-1100", "cyc-entry-20260717-1145"]   # never replayed


@pytest.mark.asyncio
async def test_a_restart_journals_the_slots_it_slept_through(rig):
    await rig.slots_then_drain()
    rig.clock.advance(46 * 60)
    rig.build()                                                     # a new process on the same ai.duckdb
    await rig.controller.start()
    await rig.slots_then_drain()
    assert cycle_states(rig)["cyc-entry-20260717-1130"] == ("MISSED", "LATE_START")
    assert entry_cycles_run(rig) == ["cyc-entry-20260717-1100", "cyc-entry-20260717-1145"]


@pytest.mark.asyncio
async def test_a_restart_on_the_next_day_journals_yesterdays_last_slots(tmp_path):
    rig = await rig_at(tmp_path, et(15, 16, day=FRIDAY - dt.timedelta(days=1)))    # Thursday 15:16
    await rig.slots_then_drain()
    assert cycle_states(rig)["cyc-entry-20260716-1515"] == ("DONE", None)
    rig.clock.advance((dt.timedelta(hours=18, minutes=30)).total_seconds())        # Friday 09:46
    rig.build()
    await rig.controller.start()
    await rig.slots_then_drain()
    states = cycle_states(rig)
    assert states["cyc-position-20260716-1530"] == ("MISSED", "LATE_START")
    assert "cyc-entry-20260716-1530" not in states                  # 15:15 was the last entry slot
    assert entry_cycles_run(rig) == ["cyc-entry-20260716-1515", "cyc-entry-20260717-0945"]


@pytest.mark.asyncio
async def test_the_first_start_does_not_invent_slots_from_before_the_service_ran(rig):
    await rig.slots_then_drain()
    assert sorted(cycle_states(rig)) == ["cyc-entry-20260717-1100", "cyc-position-20260717-1100"]
