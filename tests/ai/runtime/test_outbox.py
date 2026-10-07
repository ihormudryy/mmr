"""SP2 Plan 5 Task 7: costs and baselines reach the trader once (spec 7, 9; Rulings 12-14)."""
import datetime as dt
import json

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, FakeIngest, FakeLeadership, ScriptedTrader, et, receipt, \
    write_correction, write_cost_event
from trader.ai.engine import ProposedDecision, SimulatedBaseline
from trader.ai.ids import attempt_ref, cost_record_id, derive_decision_id
from trader.ai.journal import AttemptJournal
from trader.ai.model_client import Usage
from trader.ai.outbox import ReportingOutbox, micros_to_usd, register_context_in_tx
from trader.ai.runtime_schema import ALL_MIGRATIONS, read_cursor
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter
from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest

EXP = "exp-" + "a" * 20
SIG = "sig-" + "1" * 32


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock(et(11, 0))
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.ingest = FakeIngest()
        self.outbox = ReportingOutbox(store=self.store, journal=AttemptJournal(self.store), supervisor=self.ingest,
                                      clock=self.clock)
        self.store.transaction(lambda conn: register_context_in_tx(
            conn, context_key=SIG, experiment_id=EXP, served_kind="signal", served_id=SIG, now=self.clock.now()))

    def event(self, kind, cost=80_000, usage=None):
        return write_cost_event(self.store, request_key=f"{SIG}/jev/1", kind=kind, cost_micros=cost,
                                usage=usage, now=self.clock.now())

    def rows(self):
        return self.store.db.execute("SELECT record_id, state, attempts, last_code, delivered_status, source_ref, "
                                     "attempt_key, body_json FROM ai_outbox ORDER BY created_seq", fetch="all")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_money_moves_from_micros_to_usd_exactly():
    assert (micros_to_usd(240_000), micros_to_usd(1), micros_to_usd(0)) == (0.24, 0.000001, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,cost,usage,status,usd", [
    ("CONFIRMED", 15_000, Usage(1000, 200), "confirmed", 0.015),
    ("ESTIMATED_UNKNOWN", 80_000, None, "estimated", 0.08),        # the reserved worst case, never confirmed
    ("NONE", 0, None, "confirmed", 0.0)])                           # proven not sent: costs nothing
async def test_each_cost_kind_maps_to_one_valid_record_ai_cost_body(rig, kind, cost, usage, status, usd):
    attempt_key = rig.event(kind, cost, usage)
    assert await rig.outbox.pump_costs() == 1
    record_id, state, _, _, _, source_ref, kept_attempt, body_json = rig.rows()[0]
    body = json.loads(body_json)
    assert (state, source_ref, kept_attempt) == ("PENDING", f"{attempt_key}:{kind}", attempt_key)
    assert record_id == body["record_id"] == cost_record_id(f"{attempt_key}:{kind}")
    assert (body["cost_status"], body["cost_usd"], body["attempt_id"]) == (status, usd, attempt_ref(attempt_key))
    assert (body["experiment_id"], body["served_kind"], body["served_id"], body["decision_id"]) == \
        (EXP, "signal", SIG, None)
    assert body["called_at"] == "2026-07-17T15:00:00+00:00"
    RecordAiCostRequest.model_validate(body)                  # a real Plan 4 event id passes Plan 2's wire model


@pytest.mark.asyncio
async def test_a_correction_points_at_its_estimated_original_and_repeats_its_identity(rig):
    attempt_key = rig.event("ESTIMATED_UNKNOWN")
    rig.clock.advance(600)
    write_correction(rig.store, attempt_key, usage=Usage(900, 100), cost_micros=1_400, now=rig.clock.now())
    await rig.outbox.pump_costs()
    original, correction = (json.loads(row[7]) for row in rig.rows())
    assert correction["corrects_record_id"] == original["record_id"]
    assert (correction["cost_status"], correction["cost_usd"]) == ("confirmed", 0.0014)
    for key in ("role", "provider", "model", "attempt_id", "called_at", "experiment_id"):
        assert correction[key] == original[key]
    RecordAiCostRequest.model_validate(correction)


@pytest.mark.asyncio
async def test_cost_rows_and_the_cursor_commit_together(rig, monkeypatch):
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    import trader.ai.outbox as outbox_module
    real = outbox_module.set_cursor_in_tx

    def crash(*args, **kwargs):
        raise RuntimeError("process died inside the transaction")
    monkeypatch.setattr(outbox_module, "set_cursor_in_tx", crash)
    with pytest.raises(RuntimeError):
        await rig.outbox.pump_costs()
    assert rig.rows() == [] and await read_cursor(rig.store, "cost_events") == 0
    monkeypatch.setattr(outbox_module, "set_cursor_in_tx", real)
    assert await rig.outbox.pump_costs() == 1 and await rig.outbox.pump_costs() == 0
    assert len(rig.rows()) == 1


@pytest.mark.asyncio
async def test_a_cost_without_its_context_is_dead_and_visible(rig):
    write_cost_event(rig.store, request_key="cyc-entry-20260717-1100/orchestrator/1", kind="NONE",
                     cost_micros=0, now=rig.clock.now())
    await rig.outbox.pump_costs()
    assert rig.rows()[0][1:4] == ("DEAD", 0, "CONTEXT_MISSING")
    assert (await rig.outbox.counts())["dead"] == 1


@pytest.mark.asyncio
async def test_delivery_survives_a_trader_outage(rig):                                 # Review Focus 5
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    await rig.outbox.pump_costs()
    rig.ingest.script = ["down", "down"]
    assert await rig.outbox.deliver_due() == 0
    rig.clock.advance(5)
    assert await rig.outbox.deliver_due() == 0
    assert rig.rows()[0][1:4] == ("PENDING", 2, "TRADER_UNREACHABLE")
    assert await rig.outbox.deliver_due() == 0                  # backoff: 10 s after the second failure
    rig.clock.advance(10)
    assert await rig.outbox.deliver_due() == 1
    assert rig.rows()[0][1] == "DELIVERED" and len(rig.ingest.rows) == 1


@pytest.mark.asyncio
async def test_a_lost_acknowledgement_never_duplicates(rig):                            # Review Focus 5
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    await rig.outbox.pump_costs()
    rig.ingest.script = ["lose_ack"]
    await rig.outbox.deliver_due()
    rig.clock.advance(5)
    await rig.outbox.deliver_due()
    assert rig.rows()[0][1] == "DELIVERED" and rig.rows()[0][4] == "DUPLICATE"
    assert len(rig.ingest.rows) == 1 and len(rig.ingest.calls) == 2


@pytest.mark.asyncio
async def test_refusals_follow_the_retryable_flag(rig):
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    rig.clock.advance(1)
    rig.event("NONE", 0)
    await rig.outbox.pump_costs()
    rig.ingest.script = [("refuse", "CORRECTION_TARGET_UNKNOWN", True), ("refuse", "EXPERIMENT_UNKNOWN", False)]
    await rig.outbox.deliver_due()
    assert [(row[1], row[3]) for row in rig.rows()] == [("PENDING", "CORRECTION_TARGET_UNKNOWN"),
                                                         ("DEAD", "EXPERIMENT_UNKNOWN")]


@pytest.mark.asyncio
async def test_a_baseline_waits_for_its_linked_decision(rig):
    leadership = FakeLeadership(1)
    trader = ScriptedTrader()
    submitter = Submitter(store=rig.store, supervisor=trader, leadership=leadership, clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    enter = ProposedDecision(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                             evidence_digest="sha256:" + "c" * 64, deployment_digest="sha256:" + "a" * 64,
                             policy_revision=1, stop_price=225.4, target_price=234.6, quantity=3)
    follow = SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, rig.clock.now(), conid=AAPL, side="BUY",
                               reference_price=230.0, stop_price=225.4, target_price=234.6,
                               deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}")
    decision_id = derive_decision_id(SIG, f"enter:{AAPL}")
    now = rig.clock.now()

    def commit(conn):
        submitter.insert_in_tx(conn, source_kind="entry_signal", source_id=SIG, decision=enter,
                               expires_at=now + dt.timedelta(minutes=5), epoch=1, now=now)
        rig.outbox.enqueue_simulated_in_tx(conn, experiment_id=EXP, baseline=follow,
                                           wait_for_decision_id=decision_id, now=now)
    await rig.store.atransaction(commit)
    await rig.outbox.deliver_due()
    assert rig.ingest.calls == [] and rig.rows()[0][1] == "WAITING"
    await submitter.send_due()                                       # the trader accepts it
    await rig.outbox.deliver_due()
    body = rig.ingest.rows[rig.rows()[0][0]]
    assert body["linked_decision_id"] == decision_id
    RecordSimulatedDecisionRequest.model_validate(body)


@pytest.mark.asyncio
async def test_a_baseline_of_a_decision_that_never_reached_the_trader_drops_the_link(rig):
    leadership = FakeLeadership(1)
    submitter = Submitter(store=rig.store, supervisor=ScriptedTrader(), leadership=leadership, clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    enter = ProposedDecision(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                             evidence_digest="sha256:" + "c" * 64, quantity=3, stop_price=225.4)
    follow = SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, rig.clock.now(), conid=AAPL, side="BUY",
                               reference_price=230.0, stop_price=225.4, target_price=234.6,
                               deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}")
    now = rig.clock.now()

    def commit(conn):
        submitter.insert_in_tx(conn, source_kind="entry_signal", source_id=SIG, decision=enter,
                               expires_at=now + dt.timedelta(seconds=60), epoch=1, now=now)
        rig.outbox.enqueue_simulated_in_tx(conn, experiment_id=EXP, baseline=follow,
                                           wait_for_decision_id=derive_decision_id(SIG, f"enter:{AAPL}"), now=now)
    await rig.store.atransaction(commit)
    leadership.epoch = None
    rig.clock.advance(58)
    await submitter.send_due()                                       # expired unsent: ABANDONED
    await rig.outbox.deliver_due()
    assert list(rig.ingest.rows.values())[0]["linked_decision_id"] is None


CYCLE = "cyc-position-20260717-1100"
OLD_ENTER = derive_decision_id("sig-" + "2" * 32, f"enter:{AAPL}")


async def plan_partial_close_with_matched_entry(rig, submitter, ttl_seconds):
    partial = ProposedDecision(action_key=f"partial_close:{AAPL}", action="PARTIAL_CLOSE", conid=AAPL, side="SELL",
                               decider="orchestrator", evidence_digest="sha256:" + "d" * 64, quantity=1)
    close_id = derive_decision_id(CYCLE, partial.action_key)
    matched = SimulatedBaseline("matched_entry_bracket_exit.v1", "model_close", close_id, rig.clock.now(),
                                conid=AAPL, side="BUY", quantity=1, reference_price=230.0, stop_price=225.4,
                                target_price=234.6, linked_decision_id=OLD_ENTER, linked_round_trip_id="rt-1")
    now = rig.clock.now()

    def commit(conn):
        submitter.insert_in_tx(conn, source_kind="position_cycle", source_id=CYCLE, decision=partial,
                               expires_at=now + dt.timedelta(seconds=ttl_seconds), epoch=1, now=now)
        rig.outbox.enqueue_simulated_in_tx(conn, experiment_id=EXP, baseline=matched, wait_for_decision_id=close_id,
                                           now=now)
    await rig.store.atransaction(commit)
    return close_id


@pytest.mark.asyncio
async def test_a_matched_entry_waits_for_its_own_close_to_resolve(rig):
    leadership, trader = FakeLeadership(1), ScriptedTrader()
    submitter = Submitter(store=rig.store, supervisor=trader, leadership=leadership, clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    close_id = await plan_partial_close_with_matched_entry(rig, submitter, ttl_seconds=300)
    await rig.outbox.deliver_due()
    assert rig.ingest.calls == [] and rig.rows()[0][1] == "WAITING"
    await submitter.send_due()                                       # the close reaches the trader
    assert (await submitter.get(close_id)).state == "ACCEPTED"
    await rig.outbox.deliver_due()
    assert rig.ingest.calls == [] and rig.rows()[0][1] == "WAITING"  # SUBMITTED may still become REJECTED
    trader.ledger[close_id] = receipt(close_id, "RESOLVED")
    await submitter.reconcile_once()
    assert (await submitter.get(close_id)).state == "FINAL"
    await rig.outbox.deliver_due()
    body = rig.ingest.rows[rig.rows()[0][0]]
    assert (body["opportunity_id"], body["linked_decision_id"], body["quantity"]) == (close_id, OLD_ENTER, 1)
    RecordSimulatedDecisionRequest.model_validate(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["POSITION_NOT_OWNED", "VALIDATION_ERROR"])
async def test_a_matched_entry_of_a_rejected_close_is_dropped(rig, refusal):          # PR #84 thread 4210304478
    trader = ScriptedTrader()
    trader.script = [("receipt", "REJECTED", refusal)]
    submitter = Submitter(store=rig.store, supervisor=trader, leadership=FakeLeadership(1), clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    close_id = await plan_partial_close_with_matched_entry(rig, submitter, ttl_seconds=300)
    await submitter.send_due()
    assert (await submitter.get(close_id)).state == "FINAL"
    await rig.outbox.deliver_due()
    assert rig.ingest.calls == [] and rig.rows()[0][1] == "DROPPED"
    assert (await rig.outbox.counts())["dropped"] == 1


@pytest.mark.asyncio
async def test_a_follow_signal_of_a_rejected_enter_carries_no_link(rig):
    trader = ScriptedTrader()
    trader.script = [("receipt", "REJECTED", "OUT_OF_DISCRETIONARY_SCOPE")]
    submitter = Submitter(store=rig.store, supervisor=trader, leadership=FakeLeadership(1), clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    enter = ProposedDecision(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                             evidence_digest="sha256:" + "c" * 64, quantity=3, stop_price=225.4)
    follow = SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, rig.clock.now(), conid=AAPL, side="BUY",
                               reference_price=230.0, stop_price=225.4, target_price=234.6,
                               deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}")
    now = rig.clock.now()

    def commit(conn):
        submitter.insert_in_tx(conn, source_kind="entry_signal", source_id=SIG, decision=enter,
                               expires_at=now + dt.timedelta(seconds=300), epoch=1, now=now)
        rig.outbox.enqueue_simulated_in_tx(conn, experiment_id=EXP, baseline=follow,
                                           wait_for_decision_id=derive_decision_id(SIG, f"enter:{AAPL}"), now=now)
    await rig.store.atransaction(commit)
    await submitter.send_due()
    await rig.outbox.deliver_due()
    assert list(rig.ingest.rows.values())[0]["linked_decision_id"] is None


@pytest.mark.asyncio
async def test_a_matched_entry_whose_close_never_reached_the_trader_is_dropped(rig):
    leadership = FakeLeadership(1)
    submitter = Submitter(store=rig.store, supervisor=ScriptedTrader(), leadership=leadership, clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    close_id = await plan_partial_close_with_matched_entry(rig, submitter, ttl_seconds=60)
    leadership.epoch = None
    rig.clock.advance(58)
    await submitter.send_due()                                       # expired unsent
    assert (await submitter.get(close_id)).state == "ABANDONED"
    await rig.outbox.deliver_due()
    assert rig.ingest.calls == [] and rig.rows()[0][1] == "DROPPED"
    assert (await rig.outbox.counts())["dropped"] == 1


@pytest.mark.asyncio
async def test_a_correction_waits_until_its_estimate_is_delivered(rig):                # PR #84 thread 4210304972
    attempt_key = rig.event("ESTIMATED_UNKNOWN")
    await rig.outbox.pump_costs()
    rig.ingest.script = ["down"]
    await rig.outbox.deliver_due()                                   # the estimate goes into a 5 s backoff
    write_correction(rig.store, attempt_key, usage=Usage(900, 100), cost_micros=1_400, now=rig.clock.now())
    await rig.outbox.pump_costs()
    estimate_id, correction_id = (row[0] for row in rig.rows())
    await rig.outbox.deliver_due()                                   # the estimate is not due yet
    assert [record for _, record in rig.ingest.calls] == [estimate_id]   # the correction was not tried
    rig.clock.advance(5)
    assert await rig.outbox.deliver_due() == 2
    assert [record for _, record in rig.ingest.calls] == [estimate_id, estimate_id, correction_id]
    assert [row[3] for row in rig.rows()] == ["TRADER_UNREACHABLE", None]   # never CORRECTION_TARGET_UNKNOWN


@pytest.mark.asyncio
async def test_a_correction_of_a_dead_estimate_is_dead_too(rig):
    attempt_key = rig.event("ESTIMATED_UNKNOWN")
    await rig.outbox.pump_costs()
    rig.ingest.script = [("refuse", "EXPERIMENT_UNKNOWN", False)]
    await rig.outbox.deliver_due()
    write_correction(rig.store, attempt_key, usage=Usage(900, 100), cost_micros=1_400, now=rig.clock.now())
    await rig.outbox.pump_costs()
    await rig.outbox.deliver_due()
    assert [(row[1], row[3]) for row in rig.rows()] == [("DEAD", "EXPERIMENT_UNKNOWN"), ("DEAD", "CORRECTED_RECORD_DEAD")]
    assert len(rig.ingest.calls) == 1
