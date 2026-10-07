import datetime as dt

import pydantic
import pytest

from tests.scoreboard.common import ACCOUNT, EXP_ID
from tests.scoreboard.ingest_world import (DECIDED, DEPLOYMENT, INGESTED_AT, STARTED, FakeDecisions, FakeExperiments,
                                           FakeSizer, FakeTrips, close_fact, cost, cost_body, enter_fact,
                                           incomplete_body, make_ingest, matched_body, no_trade_body, sim, sim_body)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.ingest import AiIngest
from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest


@pytest.fixture
def ingest(store):
    return make_ingest(store, decisions=FakeDecisions(enter_fact()))


def codes(reply):
    return (reply["status"], reply["code"])


def test_a_cost_is_inserted_and_sealed(ingest, store):
    assert cost(ingest)["status"] == "INSERTED"
    assert store.fetch("ai_costs", {})[0]["cost_status"] == "confirmed" and store.verify_seals() == []


def test_redelivery_with_a_different_time_spelling_is_a_duplicate(ingest, store):
    assert cost(ingest)["status"] == "INSERTED"
    assert cost(ingest, called_at="2026-10-06T10:30:00-04:00")["status"] == "DUPLICATE"
    assert len(store.fetch("ai_costs", {})) == 1 and store.seal_count() == 1


def test_a_changed_body_under_the_same_id_is_refused(ingest):
    cost(ingest)
    assert codes(cost(ingest, cost_usd=9.0)) == ("REFUSED", "CONFLICTING_DUPLICATE")


def test_a_second_original_for_one_attempt_is_a_conflict(ingest):
    cost(ingest)
    assert codes(cost(ingest, record_id="cost-0000002")) == ("REFUSED", "CONFLICTING_DUPLICATE")


@pytest.mark.parametrize("changes", [
    dict(cost_status="unknown"), dict(cost_status="confirmed", cost_usd=None), dict(cost_usd=-1.0),
    dict(cost_usd=float("nan")), dict(called_at="2026-10-06T14:30:00"), dict(role="oracle"),
    dict(record_id="short"), dict(experiment_id="exp-1"), dict(extra_field=1)])
def test_bad_cost_bodies_never_reach_the_store(changes):
    with pytest.raises(pydantic.ValidationError):
        RecordAiCostRequest.model_validate(cost_body(**changes))


def test_unknown_cost_is_stored_null_not_zero(ingest, store):
    assert cost(ingest, cost_status="unknown", cost_usd=None)["status"] == "INSERTED"
    assert store.fetch("ai_costs", {})[0]["cost_usd"] is None


def test_unknown_experiment_and_calls_before_the_start_are_refused(store):
    ingest = make_ingest(store)
    assert codes(cost(ingest, experiment_id="exp-ffffffffffffffffffff")) == ("REFUSED", "EXPERIMENT_UNKNOWN")
    assert codes(cost(ingest, called_at="2026-10-05T12:00:00+00:00")) == ("REFUSED", "CALL_OUTSIDE_EXPERIMENT")


def test_a_cost_after_the_stop_is_still_recorded_but_a_simulated_decision_is_not(store):
    stopped = FakeExperiments()
    stopped.record.stopped_at = STARTED + dt.timedelta(hours=1)
    ingest = make_ingest(store, experiments=stopped)
    assert cost(ingest)["status"] == "INSERTED"                       # money spent is never dropped
    assert codes(sim(ingest)) == ("REFUSED", "DECIDED_OUTSIDE_EXPERIMENT")


def test_a_decision_link_is_validated_and_an_unknown_one_is_retryable(ingest, store):
    unknown = cost(ingest, decision_id="dec-99999999")
    assert codes(unknown) == ("REFUSED", "DECISION_LINK_UNKNOWN") and unknown["retryable"] is True
    assert cost(ingest, decision_id="dec-00000001")["status"] == "INSERTED"
    other = make_ingest(store, decisions=FakeDecisions(enter_fact(account_id="DU999")))
    assert codes(cost(other, record_id="cost-0000003", attempt_id="att-0000003", decision_id="dec-00000001")) == (
        "REFUSED", "DECISION_LINK_WRONG_ACCOUNT")


def test_correction_replaces_the_cost_once(ingest, store):
    cost(ingest, cost_status="estimated", cost_usd=0.4)
    fix = cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001", cost_status="confirmed",
               cost_usd=0.55)
    assert fix["status"] == "INSERTED"
    rows = {r["record_id"]: r for r in store.fetch("ai_costs", {})}
    assert rows["cost-0000002"]["correction_seq"] == 1 and rows["cost-0000001"]["cost_usd"] == 0.4
    again = cost(ingest, record_id="cost-0000003", corrects_record_id="cost-0000001", cost_status="confirmed",
                 cost_usd=0.56)
    assert again["status"] == "INSERTED" and store.fetch("ai_costs", {"record_id": "cost-0000003"})[0][
        "correction_seq"] == 2


def test_correction_cannot_lower_the_status(ingest):
    cost(ingest)
    assert codes(cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001",
                      cost_status="estimated", cost_usd=0.1)) == ("REFUSED", "CORRECTION_DOWNGRADE")


def test_correction_rules(ingest):
    assert cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001")["retryable"] is True
    cost(ingest)
    cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001", cost_usd=0.6)
    assert codes(cost(ingest, record_id="cost-0000004", corrects_record_id="cost-0000002")) == (
        "REFUSED", "CORRECTION_OF_CORRECTION")
    assert codes(cost(ingest, record_id="cost-0000005", corrects_record_id="cost-0000001", model="other")) == (
        "REFUSED", "CORRECTION_IDENTITY_MISMATCH")


def test_a_simulated_decision_is_inserted_with_its_session_date(ingest, store):
    assert sim(ingest)["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    assert row["session_date"] == dt.date(2026, 10, 6) and row["baseline_id"] == "follow_signal.v1"
    assert store.fetch("simulated_outcomes", {}) == []


def test_simulated_redelivery_is_a_duplicate_and_a_changed_body_is_a_conflict(ingest):
    sim(ingest)
    assert sim(ingest)["status"] == "DUPLICATE"
    assert codes(sim(ingest, reference_price=100.5)) == ("REFUSED", "CONFLICTING_DUPLICATE")


def test_sized_baselines_take_the_trader_size_and_keep_it_on_redelivery(store):
    sizer = FakeSizer(quantity=7)
    ingest = make_ingest(store, sizer=sizer)
    assert sim(ingest)["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    assert (row["quantity"], row["quantity_source"]) == (7, "trader_sizing")
    assert '"binding":"gross_fraction"' in row["sizing_json"]
    assert sizer.calls == [dict(account_id=ACCOUNT, deployment_digest=DEPLOYMENT, conid=265598,
                                reference_price=100.0, stop_price=98.0)]
    sizer.quantity = 9                                                    # the broker moved
    assert sim(ingest)["status"] == "DUPLICATE" and len(sizer.calls) == 1   # no re-sizing, first size kept
    assert store.fetch("simulated_decisions", {})[0]["quantity"] == 7


@pytest.mark.parametrize("make,code", [
    (lambda store: make_ingest(store, sizer=FakeSizer(refuse="QUANTITY_BELOW_ONE_SHARE")), "QUANTITY_BELOW_ONE_SHARE"),
    (lambda store: make_ingest(store, now=INGESTED_AT + dt.timedelta(minutes=5)), "SIZING_TOO_LATE"),
])
def test_a_sizing_failure_is_an_incomplete_record(store, make, code):
    assert sim(make(store))["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert row["quantity"] is None and code in row["sizing_json"]
    assert (outcome["status"], outcome["reason"], outcome["pnl_usd"]) == ("INCOMPLETE", "sizing_unavailable", None)


def test_no_sizer_is_sizing_unavailable(store):
    ingest = AiIngest(store=store, experiments=FakeExperiments(), decisions=FakeDecisions(),
                      calendar=XNYSCalendarPolicy(), now=lambda: INGESTED_AT, sizer=None)
    sim(ingest)
    assert store.fetch("simulated_outcomes", {})[0]["reason"] == "sizing_unavailable"


def test_a_linked_follow_baseline_takes_the_real_enter_quantity(store):
    sizer = FakeSizer(quantity=99)
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(entry_quantity=6)), sizer=sizer)
    assert sim(ingest, linked_decision_id="dec-00000001")["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    assert (row["quantity"], row["quantity_source"], sizer.calls) == (6, "linked_entry", [])


def test_a_linked_enter_that_was_never_placed_is_sized_like_an_unlinked_one(store):
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(entry_quantity=None)), sizer=FakeSizer(quantity=5))
    sim(ingest, linked_decision_id="dec-00000001")
    assert store.fetch("simulated_decisions", {})[0]["quantity_source"] == "trader_sizing"


def matched_world(store, *closes, trips=None):
    return make_ingest(store, decisions=FakeDecisions(enter_fact(), *(closes or (close_fact(),))), trips=trips)


def test_the_matched_entry_stores_its_requested_quantity_and_is_not_sized(store):
    sizer = FakeSizer(quantity=99)
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(), close_fact()), sizer=sizer)
    assert sim(ingest, matched_body())["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    assert (row["quantity"], row["quantity_source"], row["linked_round_trip_id"], sizer.calls) == (
        4, "client", "rt-1", [])


def test_two_partial_closes_of_one_trip_are_two_records(store):
    ingest = matched_world(store, close_fact(), close_fact("dec-00000032", action="CLOSE"))
    assert sim(ingest, matched_body())["status"] == "INSERTED"                           # PARTIAL_CLOSE 4
    assert sim(ingest, matched_body(record_id="sim-0000021", opportunity_id="dec-00000032",
                                    quantity=6))["status"] == "INSERTED"                 # CLOSE of the other 6
    rows = store.fetch("simulated_decisions", {"baseline_id": "matched_entry_bracket_exit.v1"})
    assert {(r["opportunity_id"], r["quantity"], r["linked_round_trip_id"]) for r in rows} == {
        ("dec-00000031", 4, "rt-1"), ("dec-00000032", 6, "rt-1")}


def test_the_trip_comes_from_the_trader_not_the_caller(store):                   # second PR #75 review
    ingest = matched_world(store, close_fact(), close_fact("dec-00000032", action="CLOSE"))
    changed = sim(ingest, matched_body(linked_round_trip_id="rt-other"))
    assert (changed["status"], changed["code"], changed["retryable"]) == ("REFUSED", "MATCHED_ENTRY_TRIP_MISMATCH", False)
    assert sim(ingest, matched_body(linked_round_trip_id=None))["status"] == "INSERTED"
    assert store.fetch("simulated_decisions", {})[0]["linked_round_trip_id"] == "rt-1"     # derived, not supplied


@pytest.mark.parametrize("close,code,retryable", [
    (None, "DECISION_LINK_UNKNOWN", True),
    (close_fact(action="ENTER"), "MATCHED_CLOSE_INVALID", False),
    (close_fact(experiment_id="exp-ffffffffffffffffffff"), "MATCHED_CLOSE_INVALID", False),
    (close_fact(conid=4815747), "MATCHED_CLOSE_INVALID", False),
    (close_fact(received_at=STARTED), "MATCHED_CLOSE_INVALID", False),            # before the trip opened
    (close_fact(account_id="DU999"), "DECISION_LINK_WRONG_ACCOUNT", False),         # review 4210054838
])
def test_the_close_must_be_a_close_of_that_trip(store, close, code, retryable):
    facts = FakeDecisions(enter_fact(), *([close] if close is not None else []))
    reply = sim(make_ingest(store, decisions=facts), matched_body())
    assert (reply["code"], reply["retryable"]) == (code, retryable)


def test_a_trip_the_scoreboard_has_not_built_yet_is_retried(store):
    reply = sim(matched_world(store, trips=FakeTrips(**{"dec-00000001": None})), matched_body())
    assert (reply["code"], reply["retryable"]) == ("MATCHED_ENTRY_TRIP_UNKNOWN", True)


def test_a_sizer_quote_failure_is_incomplete_not_complete(store):               # second PR #75 review
    ingest = make_ingest(store, sizer=FakeSizer(refuse="QUOTE_STALE", reason="quote_not_executable"))
    sim(ingest)
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (outcome["status"], outcome["reason"]) == ("INCOMPLETE", "quote_not_executable")


@pytest.mark.parametrize("reason", ["quote_unavailable", "feed_not_accepted", "quote_not_executable", "budget_refused",
                                    "model_failed"])
def test_an_incomplete_baseline_is_stored_incomplete_at_once(ingest, store, reason):
    sizer_calls_before = len(ingest._sizer.calls)
    assert sim(ingest, incomplete_body(reason))["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (row["incomplete_reason"], row["quantity"], row["reference_price"]) == (reason, None, None)
    assert (outcome["status"], outcome["reason"], outcome["exit_kind"], outcome["pnl_usd"], outcome["bar_source"]) == (
        "INCOMPLETE", reason, "NONE", None, "none")
    assert len(ingest._sizer.calls) == sizer_calls_before                 # nothing to size
    assert sim(ingest, incomplete_body(reason))["status"] == "DUPLICATE" and store.seal_count() == 2


@pytest.mark.parametrize("body", [
    incomplete_body(reference_price=100.0),                     # an incomplete record never carries a price
    incomplete_body(side="BUY"),
    incomplete_body(conid=None),                                # the instrument is known unless nothing ranked
    no_trade_body(incomplete_reason="quote_unavailable"),       # no_trade is always complete
    sim_body(quantity=10),                                      # the trader sizes follow_signal
    sim_body(baseline_id="fixed_rule.v1", cohort="self_found", deployment_digest=None),
    matched_body(quantity=None),                                # the matched entry carries its close's quantity
    matched_body(linked_decision_id=None),                      # ... and its ENTER decision
    sim_body(linked_round_trip_id="rt-1"),                      # trip linkage is matched-entry only
    sim_body(incomplete_reason="stale_vibes"),
    incomplete_body("ranking_unavailable", conid=None),         # ranking_unavailable is fixed_rule only
])
def test_shapes_by_baseline_are_enforced_by_the_wire_model(body):
    with pytest.raises(pydantic.ValidationError):
        RecordSimulatedDecisionRequest.model_validate(body)


def test_one_decision_per_opportunity_and_baseline(ingest):
    sim(ingest)
    assert codes(sim(ingest, record_id="sim-0000002")) == ("REFUSED", "CONFLICTING_DUPLICATE")
    assert sim(ingest, record_id="sim-0000003", baseline_id="fixed_rule.v1", cohort="self_found")["status"] == "INSERTED"


@pytest.mark.parametrize("changes", [
    dict(side="SELL"), dict(quantity=0), dict(stop_price=101.0), dict(target_price=99.0), dict(conid=None),
    dict(reference_price=float("inf")), dict(decided_at="2026-10-06T14:30:00"), dict(deployment_digest="sha256:x")])
def test_bad_simulated_bodies_never_reach_the_store(changes):
    with pytest.raises(pydantic.ValidationError):
        RecordSimulatedDecisionRequest.model_validate({**sim_body(), **changes})


def test_no_trade_with_prices_is_refused_by_the_model():
    with pytest.raises(pydantic.ValidationError):
        RecordSimulatedDecisionRequest.model_validate({**no_trade_body(), "quantity": 5})


def test_baseline_cohort_and_entry_window_rules(ingest):
    assert codes(sim(ingest, baseline_id="follow_signal.v9")) == ("REFUSED", "UNKNOWN_BASELINE")
    assert codes(sim(ingest, cohort="self_found")) == ("REFUSED", "COHORT_NOT_ALLOWED")
    assert codes(sim(ingest, decided_at="2026-10-06T19:45:00+00:00")) == ("REFUSED", "DECIDED_OUTSIDE_ENTRY_WINDOW")
    assert codes(sim(ingest, decided_at="2026-10-10T14:30:00+00:00")) == ("REFUSED", "DECIDED_OUTSIDE_ENTRY_WINDOW")


def test_linked_decision_must_be_an_enter_on_the_same_conid(store):
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(), enter_fact("dec-00000002", action="CLOSE")))
    assert sim(ingest, linked_decision_id="dec-00000001")["status"] == "INSERTED"
    assert codes(sim(ingest, record_id="sim-0000002", opportunity_id="sig-2",
                     linked_decision_id="dec-00000002")) == ("REFUSED", "DECISION_LINK_NOT_ENTER")
    assert codes(sim(ingest, record_id="sim-0000003", opportunity_id="sig-3", conid=4815747,
                     linked_decision_id="dec-00000001")) == ("REFUSED", "DECISION_LINK_CONID_MISMATCH")


def test_an_unrankable_fixed_rule_cycle_is_stored_incomplete(ingest, store):   # second PR #75 review
    body = incomplete_body("ranking_unavailable", record_id="sim-0000040", baseline_id="fixed_rule.v1",
                           cohort="self_found", opportunity_id="cyc-entry-20261006-1030", conid=None)
    assert sim(ingest, body)["status"] == "INSERTED"
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (outcome["status"], outcome["reason"]) == ("INCOMPLETE", "ranking_unavailable")


def test_a_decision_of_another_experiment_is_never_linked(store):          # review focus 6
    other = enter_fact("dec-00000005", experiment_id="exp-ffffffffffffffffffff")   # same account, time and conid
    ingest = make_ingest(store, decisions=FakeDecisions(other))
    reply = sim(ingest, linked_decision_id="dec-00000005")
    assert codes(reply) == ("REFUSED", "DECISION_LINK_OTHER_EXPERIMENT") and reply["retryable"] is False
    reply = cost(ingest, decision_id="dec-00000005")
    assert codes(reply) == ("REFUSED", "DECISION_LINK_OTHER_EXPERIMENT") and reply["retryable"] is False
    assert store.fetch("simulated_decisions", {}) == [] and store.fetch("ai_costs", {}) == []


def test_no_trade_is_complete_with_zero_pnl_at_once(ingest, store):
    assert sim(ingest, no_trade_body())["status"] == "INSERTED"
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (outcome["status"], outcome["pnl_usd"], outcome["trades"], outcome["bar_source"]) == (
        "COMPLETE", 0.0, 0, "none")
    assert sim(ingest, no_trade_body())["status"] == "DUPLICATE" and store.seal_count() == 2


def test_a_close_of_another_account_writes_nothing(store):                     # review 4210054838
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(), close_fact(account_id="DU999")))
    reply = sim(ingest, matched_body())
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "DECISION_LINK_WRONG_ACCOUNT", False)
    assert store.fetch("simulated_decisions", {}) == [] and store.seal_count() == 0
