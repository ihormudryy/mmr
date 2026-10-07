"""SP2 Plan 6 Task 6: the engine's flow control with real adapters over scripted providers (spec 5.5, 7, 9)."""
import datetime as dt
import json

import pytest
import pytest_asyncio

from tests.ai.decisions.fakes import (
    AAPL, DISCRETIONARY_DIGEST, MSFT, NOW, STRATEGY_DIGEST, FakeReads, ScriptedProvider, entry_quote_reply,
    trader_down,
)
from tests.ai.decisions.test_discovery_client import candidate, response
from tests.ai.fakes import FakeClock, load_test_config
from trader.ai.decision_engine import RoleHealth
from trader.ai.engine import (
    EntryCycleContext, ExperimentView, ModelWork, OwnedPosition, PositionCycleContext, ProposedDecision,
    SignalContext, SignalOpportunity,
)
from trader.ai.gateway import CallFailed, CallRefused, ModelGateway
from trader.ai.ids import derive_decision_id
from trader.ai.replay import ReplayRecorder
from trader.ai.roles import CLOSE_MARKER, ENTRY_MARKER, JEV_MARKER
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import ENTRY, POSITION, SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter
from trader.ai_service import EngineDeps, build_engine

BLOCK = (f"decisions:\n  discretionary_deployment_digest: \"{DISCRETIONARY_DIGEST}\"\n  strategies:\n"
         f"    orb: {{deployment_digest: \"{STRATEGY_DIGEST}\", stop_fraction: 0.02, target_fraction: 0.04}}\n")
EXPERIMENT = ExperimentView("exp-" + "b" * 20, "ARMED", NOW, None)
SIGNAL = SignalOpportunity("sig-" + "1" * 32, 7, "orb", AAPL, "BUY", 0.7, NOW, NOW)
ENTER_ID = derive_decision_id(SIGNAL.opportunity_id, f"enter:{AAPL}")


def ruling(verdict="TAKE", quantity=None):
    return json.dumps({"verdict": verdict, "quantity": quantity, "reason": "ok"})


def closes(*picks):
    return json.dumps({"closes": [dict(zip(("position", "action", "quantity", "reason"), pick)) for pick in picks]})


class Rig:
    def __init__(self, tmp_path, reads=None):
        self.clock = FakeClock(NOW)
        self.config = load_test_config(tmp_path, extra_top_level=BLOCK)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.orchestrator, self.jev = ScriptedProvider("vendor/orch-1"), ScriptedProvider("vendor/jev-1")
        self.gateway = ModelGateway(config=self.config, store=self.store, clock=self.clock,
                                    clients={"orchestrator": self.orchestrator.adapter("vendor/orch-1"),
                                             "jev": self.jev.adapter("vendor/jev-1")})
        self.reads = reads or FakeReads(discover_ai_candidates=response([candidate("MSFT", MSFT, change=3.0),
                                                                          candidate("AAPL", AAPL)]))
        self.engine = build_engine(EngineDeps(self.config, self.gateway, self.reads, self.clock,
                                              ReplayRecorder(self.store), self.store))

    def work(self, source_id, kind="signal"):
        async def register(*_args):
            return None
        return ModelWork(context_key=source_id, served_kind=kind, served_id=source_id, source_id=source_id,
                         experiment_id=EXPERIMENT.experiment_id, gateway=self.gateway,
                         deadline=self.gateway.new_deadline(source_id), register=register)

    def signal(self, opportunity=SIGNAL):
        return SignalContext(NOW, EXPERIMENT, opportunity, self.work(opportunity.opportunity_id))

    def entry_cycle(self):
        slot = SessionSlots().latest(ENTRY, NOW)
        return EntryCycleContext(NOW, EXPERIMENT, slot, self.work(slot.cycle_id, "cycle"))

    def position_cycle(self, positions, now=NOW):
        slot = SessionSlots().latest(POSITION, now)
        return PositionCycleContext(now, EXPERIMENT, slot, positions, self.work(slot.cycle_id, "cycle"))

    def calls(self, method):
        return [body for name, body in self.reads.calls if name == method]

    def rulings(self):
        return self.store.db.execute("SELECT unit_key, step, action_key, outcome, code FROM ai_rulings "
                                     "ORDER BY rowid", fetch="all")


async def started(tmp_path, reads=None, cap=True):
    rig = Rig(tmp_path, reads)
    await rig.gateway.start()
    if cap:
        await rig.gateway.budget.set_cap(2000 * 1_000_000)    # Plan 5 sets the owner cap from the trader
    return rig


@pytest_asyncio.fixture
async def rig(tmp_path):
    return await started(tmp_path)


def insert_enter(rig, opportunity=SIGNAL, now=NOW):
    """The ENTER the controller persisted for this signal (Plan 5's Submitter), so closes find its bracket."""
    enter = ProposedDecision(action_key=f"enter:{opportunity.conid}", action="ENTER", conid=opportunity.conid,
                             side="BUY", decider="jev", evidence_digest="sha256:" + "f" * 64,
                             deployment_digest=STRATEGY_DIGEST, policy_revision=1, stop_price=225.4,
                             target_price=239.2)
    submitter = Submitter(store=rig.store, supervisor=None, leadership=None, clock=rig.clock, slots=None,
                          experiment_state=lambda: None)
    return rig.store.transaction(lambda conn: submitter.insert_in_tx(
        conn, source_kind="entry_signal", source_id=opportunity.opportunity_id, decision=enter,
        expires_at=now + dt.timedelta(minutes=5), epoch=1, now=now))


@pytest.mark.asyncio
async def test_a_taken_signal_enters_with_code_owned_fields_and_a_linked_follow_baseline(rig):
    rig.jev.script(JEV_MARKER, ruling())
    result = await rig.engine.on_entry_signal(rig.signal())
    (enter,) = result.decisions
    assert (enter.action_key, enter.decider, enter.quantity, enter.deployment_digest) == (
        f"enter:{AAPL}", "jev", None, STRATEGY_DIGEST)
    assert (enter.stop_price, enter.target_price, enter.policy_revision) == (225.4, 239.2, 1)
    (follow,) = result.baselines
    assert (follow.baseline_id, follow.linked_action_key, follow.quantity) == ("follow_signal.v1", f"enter:{AAPL}", None)
    assert follow.deployment_digest == STRATEGY_DIGEST and follow.reference_price == 230.0


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,code", [(ruling("SKIP"), "JEV_SKIP"), ("not json", "OUTPUT_NO_JSON"),
                                        (ruling("REDUCE", 21), "JEV_REDUCE_NOT_SMALLER"),
                                        (500, "MODEL_FAILED_UNKNOWN")])
async def test_no_enter_without_a_valid_take_but_the_follow_baseline_is_still_written(rig, reply, code):
    rig.jev.script(JEV_MARKER, reply)
    result = await rig.engine.on_entry_signal(rig.signal())
    assert result.decisions == () and result.note == code
    assert [b.baseline_id for b in result.baselines] == ["follow_signal.v1"]
    assert result.baselines[0].linked_action_key is None


@pytest.mark.asyncio
async def test_a_reduce_sends_exactly_the_smaller_quantity(rig):
    rig.jev.script(JEV_MARKER, ruling("REDUCE", 4))
    (enter,) = (await rig.engine.on_entry_signal(rig.signal())).decisions
    assert enter.quantity == 4


@pytest.mark.asyncio
async def test_jev_down_blocks_every_enter_but_not_baselines(rig):                     # review focus 3
    rig.jev.script(JEV_MARKER, 404)
    first = await rig.engine.on_entry_signal(rig.signal())
    second = await rig.engine.on_entry_signal(rig.signal(SignalOpportunity("sig-" + "2" * 32, 8, "orb", AAPL, "BUY",
                                                                          0.7, NOW, NOW)))
    assert (first.decisions, first.note, second.decisions, second.note) == ((), "MODEL_FAILED_REJECTED", (),
                                                                            "JEV_UNHEALTHY")
    assert len(rig.jev.requests) == 1 and [b.baseline_id for b in second.baselines] == ["follow_signal.v1"]
    assert second.baselines[0].incomplete_reason is None
    cycle = await rig.engine.on_entry_cycle(rig.entry_cycle())
    assert cycle.decisions == () and "JEV_UNHEALTHY" in cycle.note and rig.orchestrator.requests == []
    assert sorted(b.baseline_id for b in cycle.baselines) == ["fixed_rule.v1", "no_trade.v1"]
    held = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, None, 230.0, 10.0)
    rig.orchestrator.script(CLOSE_MARKER, closes(("P1", "CLOSE", None, "fade")))
    (close,) = (await rig.engine.on_position_cycle(rig.position_cycle((held,)))).decisions
    assert (close.action, close.decider) == ("CLOSE", "orchestrator")


@pytest.mark.asyncio
async def test_an_entry_cycle_judges_each_pick_and_records_its_baselines_first(rig):
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": [{"candidate": "C1", "thesis": "breakout"},
                                                                {"candidate": "C2", "thesis": "news"}]}))
    rig.jev.script(JEV_MARKER, ruling("TAKE"), ruling("SKIP"))
    result = await rig.engine.on_entry_cycle(rig.entry_cycle())
    assert [d.action_key for d in result.decisions] == [f"enter:{MSFT}"]
    assert all(d.deployment_digest == DISCRETIONARY_DIGEST for d in result.decisions)
    fixed = next(b for b in result.baselines if b.baseline_id == "fixed_rule.v1")
    assert fixed.conid == MSFT and fixed.opportunity_id == rig.entry_cycle().slot.cycle_id
    assert "untrusted" in rig.jev.requests[0].content.decode()          # the orchestrator thesis is fenced for Jev
    assert result.note == "MSFT:JEV_TAKE,AAPL:JEV_SKIP"


@pytest.mark.asyncio
async def test_an_exit_signal_closes_a_held_conid_without_any_model(tmp_path):
    trips = {"experiment_id": EXPERIMENT.experiment_id, "trips": [
        {"round_trip_id": "rt-1", "conid": AAPL, "symbol": "AAPL", "opened_at": NOW.isoformat(),
         "opened_quantity": 10.0, "closed_quantity": 0.0, "decision_id": None, "state": "OPEN",
         "entry_avg_price": 230.0}]}
    rig = await started(tmp_path, FakeReads(get_experiment_trips=trips))
    sell = SignalOpportunity("sig-" + "3" * 32, 9, "orb", AAPL, "SELL", 0.7, NOW, NOW)
    result = await rig.engine.on_exit_signal(rig.signal(sell))
    (close,) = result.decisions
    assert (close.action, close.action_key, close.decider, close.side) == ("CLOSE", f"close:{AAPL}", "strategy",
                                                                           "SELL")
    assert result.note == "EXIT_SIGNAL" and rig.jev.requests == [] and rig.orchestrator.requests == []


@pytest.mark.asyncio
async def test_an_exit_signal_for_a_conid_not_held_is_noted(rig):
    sell = SignalOpportunity("sig-" + "3" * 32, 9, "orb", AAPL, "SELL", 0.7, NOW, NOW)
    result = await rig.engine.on_exit_signal(rig.signal(sell))
    assert (result.decisions, result.note) == ((), "NOT_HELD")


@pytest.mark.asyncio
async def test_an_exit_signal_still_closes_when_trips_cannot_be_read(tmp_path):
    rig = await started(tmp_path, FakeReads(get_experiment_trips=trader_down()))
    sell = SignalOpportunity("sig-" + "3" * 32, 9, "orb", AAPL, "SELL", 0.7, NOW, NOW)
    result = await rig.engine.on_exit_signal(rig.signal(sell))
    assert [d.action for d in result.decisions] == ["CLOSE"] and result.note == "EXIT_SIGNAL_TRIPS_UNKNOWN"


@pytest.mark.asyncio
async def test_orchestrator_down_skips_discovery_but_signals_continue(rig):
    rig.orchestrator.script(ENTRY_MARKER, 404)
    first = await rig.engine.on_entry_cycle(rig.entry_cycle())
    assert "MODEL_FAILED_REJECTED" in first.note and len(rig.calls("discover_ai_candidates")) == 1
    second = await rig.engine.on_entry_cycle(rig.entry_cycle())
    assert (second.note, second.baselines) == ("ORCHESTRATOR_UNHEALTHY", ())
    assert len(rig.calls("discover_ai_candidates")) == 1
    held = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, None, 230.0, 10.0)
    assert (await rig.engine.on_position_cycle(rig.position_cycle((held,)))).note == "ORCHESTRATOR_UNHEALTHY"
    rig.jev.script(JEV_MARKER, ruling())
    assert len((await rig.engine.on_entry_signal(rig.signal())).decisions) == 1


def test_role_health_recovers_after_the_recheck_window():
    clock = FakeClock(NOW)
    health = RoleHealth(clock, 300)
    health.observe("jev", CallFailed("HTTP_404", outcome="REJECTED", attempt_key="k#1"))
    assert not health.healthy("jev") and health.healthy("orchestrator")
    clock.advance(299)
    assert not health.healthy("jev")
    clock.advance(1)
    assert health.healthy("jev")
    health.observe("jev", CallRefused("PRICE_UNAVAILABLE"))
    assert not health.healthy("jev")
    health.ok("jev")
    assert health.healthy("jev")
    for transient in (CallRefused("BUDGET_EXHAUSTED"), CallRefused("DEADLINE_EXPIRED"),
                      CallFailed("CALL_TIMEOUT", outcome="UNKNOWN", attempt_key="k#2")):
        health.observe("orchestrator", transient)
    assert health.healthy("orchestrator")


@pytest.mark.asyncio
async def test_an_unconfigured_strategy_is_noted_without_reads(rig):
    other = SignalOpportunity("sig-" + "4" * 32, 10, "unknown_strategy", AAPL, "BUY", 0.7, NOW, NOW)
    result = await rig.engine.on_entry_signal(rig.signal(other))
    assert (result.decisions, result.baselines, result.note) == ((), (), "STRATEGY_NOT_CONFIGURED")
    assert rig.reads.calls == []


@pytest.mark.asyncio
async def test_a_failed_discovery_writes_no_cycle_baselines(tmp_path):
    rig = await started(tmp_path, FakeReads(discover_ai_candidates=trader_down()))
    result = await rig.engine.on_entry_cycle(rig.entry_cycle())
    assert (result.decisions, result.baselines, result.note) == ((), (), "TRADER_UNREACHABLE")
    assert rig.orchestrator.requests == []


@pytest.mark.asyncio
async def test_a_partial_close_carries_its_quantity_and_a_matched_baseline(rig):
    enter_id = insert_enter(rig)
    position = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, enter_id, 230.05, 10.0)
    rig.orchestrator.script(CLOSE_MARKER, closes(("P1", "PARTIAL_CLOSE", 4, "trim")))
    ctx = rig.position_cycle((position,))
    result = await rig.engine.on_position_cycle(ctx)
    (close,) = result.decisions
    assert (close.action, close.action_key, close.quantity, close.decider) == (
        "PARTIAL_CLOSE", f"partial_close:{AAPL}", 4, "orchestrator")
    assert (close.stop_price, close.target_price) == (None, None)
    (matched,) = result.baselines
    assert matched.opportunity_id == derive_decision_id(ctx.slot.cycle_id, f"partial_close:{AAPL}")
    assert (matched.linked_round_trip_id, matched.linked_decision_id, matched.quantity) == ("rt-1", enter_id, 4)
    assert (matched.reference_price, matched.stop_price, matched.target_price) == (230.05, 225.4, 239.2)
    prompt = rig.orchestrator.requests[0].content.decode()
    assert '\\"stop\\":225.4' in prompt and '\\"shares\\":10' in prompt


@pytest.mark.asyncio
async def test_two_partial_closes_in_two_cycles_give_two_matched_records(rig):
    enter_id = insert_enter(rig)
    position = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, enter_id, 230.05, 10.0)
    rig.orchestrator.script(CLOSE_MARKER, closes(("P1", "PARTIAL_CLOSE", 4, "trim")),
                            closes(("P1", "CLOSE", None, "done")))
    first = await rig.engine.on_position_cycle(rig.position_cycle((position,)))
    later = NOW + dt.timedelta(minutes=15)
    rest = OwnedPosition("rt-1", AAPL, "AAPL", 6.0, NOW, enter_id, 230.05, 10.0)
    second = await rig.engine.on_position_cycle(rig.position_cycle((rest,), now=later))
    (a,), (b,) = first.baselines, second.baselines
    assert a.opportunity_id != b.opportunity_id
    assert (a.linked_decision_id, a.linked_round_trip_id) == (b.linked_decision_id, b.linked_round_trip_id)
    assert (a.quantity, b.quantity) == (4, 6) and a.quantity + b.quantity == 10


@pytest.mark.asyncio
async def test_budget_refusal_is_a_recorded_refusal_with_a_complete_follow_baseline(rig):
    await rig.gateway.budget.set_cap(0)
    result = await rig.engine.on_entry_signal(rig.signal())
    assert (result.decisions, result.note) == ((), "MODEL_REFUSED_BUDGET_EXHAUSTED")
    (follow,) = result.baselines
    assert follow.incomplete_reason is None and follow.reference_price == 230.0 and rig.jev.requests == []


@pytest.mark.asyncio
async def test_an_unknown_cap_refuses_jev_but_keeps_the_follow_baseline(tmp_path):
    rig = await started(tmp_path, cap=False)
    result = await rig.engine.on_entry_signal(rig.signal())
    assert (result.decisions, result.note) == ((), "MODEL_REFUSED_BUDGET_CAP_UNKNOWN")
    (follow,) = result.baselines
    assert follow.incomplete_reason is None and follow.reference_price == 230.0


@pytest.mark.asyncio
async def test_an_unrankable_cycle_sends_an_incomplete_fixed_rule(tmp_path):
    reads = FakeReads(discover_ai_candidates=response([candidate("MSFT", MSFT, change=None),
                                                       candidate("AAPL", AAPL, change=None)]))
    rig = await started(tmp_path, reads)
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": []}))
    result = await rig.engine.on_entry_cycle(rig.entry_cycle())
    by_id = {b.baseline_id: b for b in result.baselines}
    fixed = by_id["fixed_rule.v1"]
    assert (fixed.incomplete_reason, fixed.conid, fixed.reference_price) == ("ranking_unavailable", None, None)
    assert by_id["no_trade.v1"].incomplete_reason is None
    assert "FIXED_RULE_RANKING_UNAVAILABLE" in result.note


@pytest.mark.asyncio
async def test_a_cycle_with_no_eligible_candidate_still_records_no_trade(tmp_path):
    reads = FakeReads(discover_ai_candidates=response([candidate("PINKY", 99, status="FAIL", part="exchange")]))
    rig = await started(tmp_path, reads)
    result = await rig.engine.on_entry_cycle(rig.entry_cycle())
    assert result.note == "NO_ELIGIBLE_CANDIDATES" and [b.baseline_id for b in result.baselines] == ["no_trade.v1"]
    assert rig.orchestrator.requests == []


@pytest.mark.asyncio
async def test_a_fixed_rule_quote_failure_sends_an_incomplete_fixed_rule(tmp_path):
    def quote(body):
        return entry_quote_reply(conid=body["conid"], feed="delayed" if body["conid"] == MSFT else "live")
    reads = FakeReads(get_ai_entry_quote=quote,
                      discover_ai_candidates=response([candidate("MSFT", MSFT, change=3.0), candidate("AAPL", AAPL)]))
    rig = await started(tmp_path, reads)
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": []}))
    result = await rig.engine.on_entry_cycle(rig.entry_cycle())
    by_id = {b.baseline_id: b for b in result.baselines}
    fixed = by_id["fixed_rule.v1"]
    assert (fixed.incomplete_reason, fixed.conid, fixed.reference_price) == ("feed_not_accepted", MSFT, None)
    assert by_id["no_trade.v1"].incomplete_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("quote,reason", [
    (lambda body: entry_quote_reply(conid=body["conid"], at=NOW - dt.timedelta(seconds=60)), "quote_not_executable"),
    (lambda body: entry_quote_reply(conid=body["conid"], feed="iex_realtime"), "feed_not_accepted"),
    (trader_down(), "quote_unavailable")])
async def test_a_missing_quote_refuses_before_any_model_and_sends_an_incomplete_baseline(tmp_path, quote, reason):
    rig = await started(tmp_path, FakeReads(get_ai_entry_quote=quote))
    result = await rig.engine.on_entry_signal(rig.signal())
    assert result.decisions == () and rig.jev.requests == []
    (follow,) = result.baselines
    assert (follow.baseline_id, follow.incomplete_reason, follow.conid) == ("follow_signal.v1", reason, AAPL)
    assert (follow.side, follow.quantity, follow.reference_price) == (None, None, None)


@pytest.mark.asyncio
async def test_every_model_step_writes_one_ruling_row(rig):
    rig.jev.script(JEV_MARKER, ruling("REDUCE", 4), ruling())
    await rig.engine.on_entry_signal(rig.signal())
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": [{"candidate": "C1", "thesis": "x"}]}))
    cycle = rig.entry_cycle()
    await rig.engine.on_entry_cycle(cycle)
    rig.orchestrator.script(CLOSE_MARKER, "buy MSFT")
    held = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, None, 230.0, 10.0)
    await rig.engine.on_position_cycle(rig.position_cycle((held,)))
    rows = rig.rulings()
    assert [(step, outcome, code) for _, step, _, outcome, code in rows] == [
        ("jev", "REDUCE", "JEV_REDUCE"), ("entries", "PICKS", "1_ENTRIES"), ("jev", "TAKE", "JEV_TAKE"),
        ("closes", "REFUSED", "OUTPUT_NO_JSON")]
    assert rows[0][0] == ENTER_ID and rows[1][0] == cycle.slot.cycle_id
    assert rows[2][0] == derive_decision_id(cycle.slot.cycle_id, f"enter:{MSFT}")


# -- PR #86 thread 4211394337: an exit signal that arrives before our accepted entry has filled ------------

SELL = SignalOpportunity("sig-" + "3" * 32, 9, "orb", AAPL, "SELL", 0.7, NOW, NOW)
SESSION_CLOSE = dt.datetime(2026, 7, 17, 20, 0, tzinfo=dt.timezone.utc)       # 16:00 ET: every DAY order is done


def our_enter(rig, state, receipt_state=None, error_code=None, opportunity=SIGNAL, now=NOW):
    decision_id = insert_enter(rig, opportunity, now)
    rig.store.db.execute("UPDATE ai_submissions SET state = ?, receipt_state = ?, error_code = ? WHERE decision_id = ?",
                         [state, receipt_state, error_code, decision_id])
    return decision_id


def entry_row(decision_id, status="Submitted", filled=0.0, deleted=False):
    """One get_broker_order_evidence row: the entry leg of our ENTER's order group."""
    return {"order_group_id": f"og-aip-{decision_id}", "leg": "entry", "status": status, "filled_quantity": filled,
            "remaining_quantity": 9.0 - filled, "deleted": deleted}


def broker(*rows):
    return {"generation_id": 7, "promoted": True, "orders": list(rows)}


async def with_broker(tmp_path, evidence):
    return await started(tmp_path, FakeReads(get_broker_order_evidence=evidence))


def at(now, opportunity=SELL, rig=None):
    return SignalContext(now, EXPERIMENT, opportunity, rig.work(opportunity.opportunity_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("state,receipt_state", [("PENDING", None), ("UNKNOWN", None), ("ACCEPTED", "SUBMITTED"),
                                                 ("FINAL", "RESOLVED")])
async def test_an_exit_signal_waits_while_our_entry_may_still_fill(rig, state, receipt_state):
    our_enter(rig, state, receipt_state)
    result = await rig.engine.on_exit_signal(rig.signal(SELL))
    assert (result.decisions, result.baselines, result.note) == ((), (), "EXIT_WAITING_FOR_ENTRY")
    assert result.wait_until == SESSION_CLOSE                       # only the controller's loud backstop uses it


@pytest.mark.asyncio
@pytest.mark.parametrize("state,receipt_state,error_code", [
    ("ABANDONED", None, "EXPIRED_UNSENT"), ("NOT_ADMITTED", None, "NOT_FOUND_AFTER_EXPIRY"),
    ("FAILED", None, "VALIDATION_ERROR"), ("FINAL", "REJECTED", "STOP_INVALID"), ("ACCEPTED", "SUBMITTED", "X")])
async def test_an_entry_that_cannot_fill_does_not_hold_the_exit(rig, state, receipt_state, error_code):
    our_enter(rig, state, receipt_state, error_code)
    result = await rig.engine.on_exit_signal(rig.signal(SELL))
    assert (result.decisions, result.note, result.wait_until) == ((), "NOT_HELD", None)


@pytest.mark.asyncio
async def test_an_older_working_entry_holds_the_exit_behind_a_newer_refused_one(rig):     # PR #86 4211895474 (a)
    our_enter(rig, "FINAL", "RESOLVED")                                              # older: admitted, working
    newer = SignalOpportunity("sig-" + "5" * 32, 11, "orb", AAPL, "BUY", 0.7, NOW, NOW)
    our_enter(rig, "FINAL", "REJECTED", "ENTRY_ALREADY_WORKING", opportunity=newer,     # newer: refused
              now=NOW + dt.timedelta(seconds=30))
    result = await rig.engine.on_exit_signal(rig.signal(SELL))
    assert (result.decisions, result.note) == ((), "EXIT_WAITING_FOR_ENTRY")


@pytest.mark.asyncio
async def test_the_exit_waits_past_the_cutoff_until_the_broker_proves_the_entry_ended(tmp_path):   # 4211898491 (b)
    evidence = {"reply": broker()}
    rig = await with_broker(tmp_path, lambda body: evidence["reply"])
    enter_id = our_enter(rig, "FINAL", "RESOLVED")
    late = dt.datetime(2026, 7, 17, 20, 30, tzinfo=dt.timezone.utc)                  # after the close
    evidence["reply"] = broker(entry_row(enter_id, status="Submitted"))              # cancel issued, not proven
    assert (await rig.engine.on_exit_signal(at(late, rig=rig))).note == "EXIT_WAITING_FOR_ENTRY"
    evidence["reply"] = {"capture_error": "GENERATION_STAGING"}                       # unreadable: no proof
    assert (await rig.engine.on_exit_signal(at(late, rig=rig))).note == "EXIT_WAITING_FOR_ENTRY"
    evidence["reply"] = broker(entry_row(enter_id, status="Cancelled", filled=2.0))   # a late fill, trips lag
    assert (await rig.engine.on_exit_signal(at(late, rig=rig))).note == "EXIT_WAITING_FOR_FILL"
    evidence["reply"] = broker(entry_row(enter_id, status="Cancelled"))               # proven: ended, zero fill
    result = await rig.engine.on_exit_signal(at(late, rig=rig))
    assert (result.decisions, result.note, result.wait_until) == ((), "ENTRY_UNFILLED", None)


@pytest.mark.asyncio
async def test_a_fill_already_closed_by_its_bracket_ends_the_wait(tmp_path):
    evidence = {"reply": broker()}
    trips = {"experiment_id": EXPERIMENT.experiment_id, "trips": []}
    rig = await started(tmp_path, FakeReads(get_broker_order_evidence=lambda body: evidence["reply"],
                                            get_experiment_trips=lambda body: trips))
    enter_id = our_enter(rig, "FINAL", "RESOLVED")
    evidence["reply"] = broker(entry_row(enter_id, status="Filled", filled=9.0))
    assert (await rig.engine.on_exit_signal(rig.signal(SELL))).note == "EXIT_WAITING_FOR_FILL"   # trip not yet
    trips["trips"] = [{"round_trip_id": "rt-1", "conid": AAPL, "symbol": "AAPL", "opened_at": NOW.isoformat(),
                       "opened_quantity": 9.0, "closed_quantity": 9.0, "decision_id": enter_id, "state": "CLOSED",
                       "entry_avg_price": 230.0}]
    result = await rig.engine.on_exit_signal(rig.signal(SELL))
    assert (result.decisions, result.note, result.wait_until) == ((), "ENTRY_FILLED_AND_CLOSED", None)


@pytest.mark.asyncio
async def test_every_fillable_entry_must_be_proven_ended(tmp_path):
    evidence = {"reply": broker()}
    rig = await with_broker(tmp_path, lambda body: evidence["reply"])
    first = our_enter(rig, "FINAL", "RESOLVED")
    second = our_enter(rig, "ACCEPTED", "SUBMITTED", now=NOW + dt.timedelta(seconds=30),
                       opportunity=SignalOpportunity("sig-" + "6" * 32, 12, "orb", AAPL, "BUY", 0.7, NOW, NOW))
    evidence["reply"] = broker(entry_row(first, status="Cancelled"))
    assert (await rig.engine.on_exit_signal(rig.signal(SELL))).note == "EXIT_WAITING_FOR_ENTRY"
    evidence["reply"] = broker(entry_row(first, status="Cancelled"), entry_row(second, status="Inactive"))
    assert (await rig.engine.on_exit_signal(rig.signal(SELL))).note == "ENTRY_UNFILLED"


@pytest.mark.asyncio
async def test_a_trader_unreachable_for_evidence_keeps_the_exit_waiting(tmp_path):
    rig = await with_broker(tmp_path, trader_down())
    our_enter(rig, "FINAL", "RESOLVED")
    assert (await rig.engine.on_exit_signal(rig.signal(SELL))).note == "EXIT_WAITING_FOR_ENTRY"


@pytest.mark.asyncio
async def test_an_entry_in_another_conid_does_not_hold_the_exit(rig):
    our_enter(rig, "ACCEPTED", "SUBMITTED")
    msft_sell = SignalOpportunity("sig-" + "4" * 32, 10, "orb", MSFT, "SELL", 0.7, NOW, NOW)
    result = await rig.engine.on_exit_signal(rig.signal(msft_sell))
    assert (result.decisions, result.note) == ((), "NOT_HELD")


@pytest.mark.asyncio
async def test_a_held_conid_closes_even_with_an_entry_in_flight(tmp_path):
    trips = {"experiment_id": EXPERIMENT.experiment_id, "trips": [
        {"round_trip_id": "rt-1", "conid": AAPL, "symbol": "AAPL", "opened_at": NOW.isoformat(),
         "opened_quantity": 4.0, "closed_quantity": 0.0, "decision_id": ENTER_ID, "state": "OPEN",
         "entry_avg_price": 230.0}]}
    rig = await started(tmp_path, FakeReads(get_experiment_trips=trips))
    our_enter(rig, "ACCEPTED", "SUBMITTED")                          # partly filled: 4 shares are held
    (close,) = (await rig.engine.on_exit_signal(rig.signal(SELL))).decisions
    assert (close.action, close.decider) == ("CLOSE", "strategy")


@pytest.mark.asyncio
async def test_a_close_whose_fill_left_the_bracket_still_gets_an_incomplete_matched_record(rig):   # 4211394769
    enter_id = insert_enter(rig)                                    # bracket 225.4 / 239.2 around the 230 ask
    position = OwnedPosition("rt-1", AAPL, "AAPL", 10.0, NOW, enter_id, 240.0, 10.0)   # filled at 240
    rig.orchestrator.script(CLOSE_MARKER, closes(("P1", "CLOSE", None, "fade")))
    ctx = rig.position_cycle((position,))
    result = await rig.engine.on_position_cycle(ctx)
    (close,) = result.decisions
    (matched,) = result.baselines
    assert (matched.baseline_id, matched.opportunity_id, matched.incomplete_reason) == (
        "matched_entry_bracket_exit.v1", derive_decision_id(ctx.slot.cycle_id, close.action_key), "entry_not_comparable")
    assert (matched.linked_decision_id, matched.linked_round_trip_id, matched.reference_price) == (enter_id, "rt-1", None)
    assert result.note == "AAPL:MATCHED_ENTRY_INCOMPLETE"


# -- PR #86 thread 4211394562: owed cycle baselines do not depend on the orchestrator finishing ------------

def controller_for(rig, tmp_path):
    """Plan 5's real controller around the real engine; the trader side of the controller is a fake."""
    import asyncio  # noqa: F401  (the controller runs on this loop)

    from tests.ai.runtime.fakes import FakeLeadership, FakeTrader
    from trader.ai.config import ControllerConfig
    from trader.ai.controller import AiController, ExperimentWatch
    from trader.ai.journal import AttemptJournal
    from trader.ai.outbox import ReportingOutbox
    from trader.ai.signal_intake import SignalIntake
    trader, leadership, slots = FakeTrader(), FakeLeadership(1), SessionSlots()
    watch = ExperimentWatch(trader)
    submitter = Submitter(store=rig.store, supervisor=trader, leadership=leadership, clock=rig.clock, slots=slots,
                          experiment_state=watch.state)
    return AiController(
        config=ControllerConfig(heartbeat_path=str(tmp_path / "hb.json")), store=rig.store, clock=rig.clock,
        supervisor=trader, leadership=leadership, watch=watch, submitter=submitter,
        outbox=ReportingOutbox(store=rig.store, journal=AttemptJournal(rig.store), supervisor=trader, clock=rig.clock),
        intake=SignalIntake(store=rig.store, supervisor=trader, clock=rig.clock), slots=slots, engine=rig.engine,
        gateway=rig.gateway)


@pytest.mark.asyncio
async def test_owed_cycle_baselines_survive_a_cycle_cut_at_its_deadline(rig, tmp_path):
    import asyncio

    from tests.ai.runtime.fakes import FRIDAY, et
    from trader.ai.schedule import Slot
    controller = controller_for(rig, tmp_path)
    await controller.start()
    rig.orchestrator.hold = asyncio.Event()                         # the orchestrator never answers
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": []}))
    slot = Slot("entry", FRIDAY, et(11, 0), rig.clock.now() + dt.timedelta(seconds=1))
    await controller.record_cycle(slot, "RUNNING", None)
    await controller.run_cycle(slot)
    assert rig.store.db.execute("SELECT state, reason FROM ai_cycles", fetch="all") == [("TIMED_OUT", "SLOT_DEADLINE")]
    assert rig.store.db.execute("SELECT status FROM ai_discovery_reads", fetch="one") == ("OK",)
    owed = rig.store.db.execute("SELECT source_ref, state FROM ai_outbox WHERE kind = 'simulated' ORDER BY source_ref",
                                fetch="all")
    assert owed == [(f"fixed_rule.v1|{slot.cycle_id}", "PENDING"), (f"no_trade.v1|{slot.cycle_id}", "PENDING")]
    fixed = json.loads(rig.store.db.execute("SELECT body_json FROM ai_outbox WHERE source_ref LIKE 'fixed%'",
                                            fetch="one")[0])
    assert (fixed["conid"], fixed["reference_price"], fixed["incomplete_reason"]) == (MSFT, 230.0, None)


@pytest.mark.asyncio
async def test_a_finished_cycle_does_not_queue_its_baselines_twice(rig, tmp_path):
    from tests.ai.runtime.fakes import FRIDAY, et
    from trader.ai.schedule import Slot
    controller = controller_for(rig, tmp_path)
    await controller.start()
    rig.orchestrator.script(ENTRY_MARKER, json.dumps({"picks": []}))
    slot = Slot("entry", FRIDAY, et(11, 0), rig.clock.now() + dt.timedelta(minutes=15))
    await controller.record_cycle(slot, "RUNNING", None)
    await controller.run_cycle(slot)
    assert rig.store.db.execute("SELECT state FROM ai_cycles", fetch="one") == ("DONE",)
    assert rig.store.db.execute("SELECT COUNT(*) FROM ai_outbox WHERE kind = 'simulated'", fetch="one") == (2,)
