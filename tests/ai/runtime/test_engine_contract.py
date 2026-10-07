"""SP2 Plan 5 Task 5: the DecisionEngine contract and code-owned ids (spec 5.3, 8)."""
import dataclasses
import datetime as dt
import math

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, et
from tests.ai.runtime.scripted_engine import ScriptedEngine
from trader.ai.engine import (
    DecisionEngine, ExperimentView, ModelWork, ProposedDecision, SimulatedBaseline, owned_positions_from_trips,
)
from trader.ai.gateway import DecisionDeadline
from trader.ai.ids import derive_decision_id

SIG = "sig-" + "1" * 32
EVIDENCE = "sha256:" + "c" * 64


def enter(**changes):
    base = dict(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                evidence_digest=EVIDENCE, deployment_digest="sha256:" + "a" * 64, policy_revision=1,
                stop_price=225.4, target_price=234.6, quantity=3)
    base.update(changes)
    return ProposedDecision(**base)


def test_decision_ids_are_derived_from_source_and_action_identity():
    first = derive_decision_id(SIG, f"enter:{AAPL}")
    assert first == derive_decision_id(SIG, f"enter:{AAPL}")                  # a redelivery is the same decision
    assert first != derive_decision_id(SIG, "enter:272093") != derive_decision_id("cyc-entry-20260717-1100",
                                                                                   f"enter:{AAPL}")
    assert first.startswith("dec-") and len(first) == 36


def test_command_id_matches_the_trader_rule():
    from trader.ai.ids import command_id_for
    from trader.automation.ai_paper_decision import command_id_for as trader_rule
    decision_id = derive_decision_id(SIG, f"enter:{AAPL}")
    assert command_id_for(decision_id) == trader_rule(decision_id)


@pytest.mark.parametrize("changes", [
    {"action_key": "Enter:1"}, {"action_key": "enter"}, {"action": "BUY"}, {"conid": True}, {"conid": 0},
    {"side": "SHORT"}, {"quantity": 0}, {"quantity": 2.0}, {"stop_price": math.nan}, {"stop_price": -1.0},
    {"evidence_digest": "abc"}, {"decider": "Jev"}, {"policy_revision": True}])
def test_a_proposed_decision_refuses_bad_values(changes):
    with pytest.raises(ValueError):
        enter(**changes)


def test_baselines_follow_the_index_pairs_and_take_at_most_one_link():
    now = et(11, 0)
    SimulatedBaseline("no_trade.v1", "self_found", "cyc-entry-20260717-1100", now)
    with pytest.raises(ValueError):
        SimulatedBaseline("no_trade.v1", "strategy_signal", "x", now)
    with pytest.raises(ValueError):
        SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, now, linked_action_key=f"enter:{AAPL}",
                          linked_decision_id="dec-" + "0" * 32)
    with pytest.raises(ValueError):
        SimulatedBaseline("no_trade.v1", "self_found", "has space", now)


@pytest.mark.parametrize("fields", [
    dict(quantity=3),                                              # the trader sizes follow_signal (Plan 2 R19)
    dict(deployment_digest=None),
    dict(incomplete_reason="quote_unavailable"),                   # an incomplete record has no prices
    dict(linked_round_trip_id="rt-1"),                             # matched-entry only
])
def test_baselines_take_plan_2_shapes(fields):
    base = dict(conid=AAPL, side="BUY", reference_price=230.0, stop_price=225.4, target_price=234.6,
                deployment_digest="sha256:" + "a" * 64)
    SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, et(11, 0), **base)          # the valid shape
    with pytest.raises(ValueError):
        SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, et(11, 0), **{**base, **fields})
    SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, et(11, 0), conid=AAPL,
                      incomplete_reason="feed_not_accepted")                                     # incomplete shape


@pytest.mark.asyncio
async def test_model_work_registers_its_context_and_owns_request_keys():
    registered = []

    async def register(key, kind, served_id):
        registered.append((key, kind, served_id))
    clock = FakeClock(et(11, 0))
    work = ModelWork(context_key=SIG, served_kind="signal", served_id=SIG, source_id=SIG,
                     experiment_id="exp-" + "0" * 20, gateway=object(),
                     deadline=DecisionDeadline(clock, 60), register=register)
    assert work.request_key("orchestrator", 1) == f"{SIG}/orchestrator/1"
    with pytest.raises(ValueError):
        work.request_key("judge", 1)
    child = await work.for_action(f"enter:{AAPL}")
    decision_id = derive_decision_id(SIG, f"enter:{AAPL}")
    assert (child.context_key, child.served_kind, child.deadline) == (decision_id, "decision", work.deadline)
    assert registered == [(decision_id, "decision", decision_id)]
    assert child.request_key("jev", 1) == f"{decision_id}/jev/1"


def test_experiment_view_reads_the_trader_reply_and_refuses_bad_shapes():
    reply = {"experiment": {"experiment_id": "exp-" + "a" * 20, "state": "ARMED",
                            "started_at": "2026-07-17T15:00:00+00:00"}, "entry_block": None}
    view = ExperimentView.from_reply(reply)
    assert (view.state, view.started_at, view.entry_block) == ("ARMED", et(11, 0), None)
    assert ExperimentView.from_reply({"experiment": None, "entry_block": None}) is None
    for bad in ({"experiment": {**reply["experiment"], "state": "RUNNING"}, "entry_block": None},
                {"experiment": {**reply["experiment"], "started_at": "2026-07-17T15:00:00"}, "entry_block": None},
                {"experiment": {**reply["experiment"], "experiment_id": "exp-1"}, "entry_block": None}):
        with pytest.raises(ValueError):
            ExperimentView.from_reply(bad)


def test_owned_positions_are_the_open_trips_with_quantity_left():
    trip = {"round_trip_id": "rt-1", "conid": AAPL, "symbol": "AAPL", "direction": "LONG",
            "opened_at": "2026-07-17T15:05:00+00:00", "closed_at": None, "opened_quantity": 3.0,
            "closed_quantity": 1.0, "decision_id": "dec-" + "1" * 32, "state": "OPEN"}
    closed = {**trip, "round_trip_id": "rt-2", "state": "CLOSED"}
    positions = owned_positions_from_trips({"experiment_id": "exp-" + "a" * 20, "trips": [trip, closed]})
    assert [(p.round_trip_id, p.open_quantity) for p in positions] == [("rt-1", 2.0)]
    with pytest.raises(ValueError):
        owned_positions_from_trips({"experiment_id": "x", "error_code": "EXPERIMENT_NOT_FOUND", "trips": None})


def test_the_scripted_engine_satisfies_the_protocol():
    engine: DecisionEngine = ScriptedEngine()
    assert all(callable(getattr(engine, name)) for name in
               ("on_entry_signal", "on_exit_signal", "on_entry_cycle", "on_position_cycle"))
    assert dataclasses.is_dataclass(enter())
