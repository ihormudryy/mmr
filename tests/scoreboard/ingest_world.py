import datetime as dt
from types import SimpleNamespace

from tests.scoreboard.common import ACCOUNT, EXP_ID, NOW
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.ingest import AiIngest
from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest
from trader.scoreboard.ports import DecisionFact, SizedBaseline, SizingUnavailable, TripFact

UTC = dt.timezone.utc
STARTED = dt.datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
DECIDED = "2026-10-06T14:30:00+00:00"          # 10:30 ET, Tuesday; flatten start is 19:45 UTC
INGESTED_AT = dt.datetime(2026, 10, 6, 14, 30, 30, tzinfo=UTC)   # 30 s after DECIDED: inside SIZING_MAX_LAG
DEPLOYMENT = "sha256:" + "d" * 64


class FakeSizer:
    """Stands in for SP1 sizing: a fixed size, or a SizingUnavailable code."""

    def __init__(self, quantity=10, refuse=None, reason="sizing_unavailable"):
        self.quantity, self.refuse, self.reason, self.calls = quantity, refuse, reason, []

    def size(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse:
            raise SizingUnavailable(self.refuse, {"binding": "none"}, reason=self.reason)
        return SizedBaseline(self.quantity, {"binding": "gross_fraction", "max_quantity": self.quantity})


class FakeExperiments:
    def __init__(self, **changes):
        self.record = SimpleNamespace(experiment_id=EXP_ID, account_id=ACCOUNT, started_at=STARTED,
                                      stopped_at=None, state="ARMED", **changes)

    def get(self, experiment_id):
        return self.record if experiment_id == self.record.experiment_id else None


class FakeDecisions:
    def __init__(self, *facts):
        self.facts = {f.decision_id: f for f in facts}

    def get(self, decision_id):
        return self.facts.get(decision_id)


def enter_fact(decision_id="dec-00000001", conid=265598, **changes):
    values = dict(decision_id=decision_id, account_id=ACCOUNT, experiment_id=EXP_ID, conid=conid, action="ENTER",
                  received_at=STARTED + dt.timedelta(days=1), entry_quantity=10)
    values.update(changes)
    return DecisionFact(**values)


class FakeTrips:
    """round_trips as the trader sees them: entry decision id -> TripFact."""

    def __init__(self, **by_entry):
        self.by_entry = {"dec-00000001": TripFact("rt-1", 265598, STARTED + dt.timedelta(days=1), 10.0), **by_entry}

    def opened_by(self, experiment_id, entry_decision_id):
        return self.by_entry.get(entry_decision_id) if experiment_id == EXP_ID else None

    def by_id(self, experiment_id, round_trip_id):
        found = [t for t in self.by_entry.values() if t is not None and t.round_trip_id == round_trip_id]
        return found[0] if experiment_id == EXP_ID and found else None


def close_fact(decision_id="dec-00000031", action="PARTIAL_CLOSE", **changes):
    values = dict(decision_id=decision_id, account_id=ACCOUNT, experiment_id=EXP_ID, conid=265598, action=action,
                  received_at=STARTED + dt.timedelta(days=1, hours=1))
    values.update(changes)
    return DecisionFact(**values)


def make_ingest(store, *, experiments=None, decisions=None, sizer=None, now=INGESTED_AT, trips=None):
    return AiIngest(store=store, experiments=experiments or FakeExperiments(), decisions=decisions or FakeDecisions(),
                    calendar=XNYSCalendarPolicy(), now=lambda: now, sizer=sizer or FakeSizer(),
                    trips=trips or FakeTrips())


def cost_body(**changes):
    body = dict(record_id="cost-0000001", experiment_id=EXP_ID, role="jev", provider="openrouter", model="m1",
                attempt_id="att-0000001", input_tokens=100, output_tokens=20, cost_usd=0.5,
                cost_status="confirmed", called_at=DECIDED, served_kind="cycle", served_id="cycle-1")
    body.update(changes)
    return body


def sim_body(**changes):
    """follow_signal.v1: the trader sizes it, so quantity is null and the deployment is named."""
    body = dict(record_id="sim-0000001", experiment_id=EXP_ID, baseline_id="follow_signal.v1",
                cohort="strategy_signal", opportunity_id="sig-1", conid=265598, side="BUY", quantity=None,
                reference_price=100.0, stop_price=98.0, target_price=104.0, decided_at=DECIDED,
                deployment_digest=DEPLOYMENT)
    body.update(changes)
    return body


def matched_body(**changes):
    """One record per model close: the opportunity is the close's decision id; the record holds the entry."""
    return sim_body(**{**dict(record_id="sim-0000020", baseline_id="matched_entry_bracket_exit.v1",
                              cohort="model_close", opportunity_id="dec-00000031", quantity=4,
                              deployment_digest=None, linked_decision_id="dec-00000001",
                              linked_round_trip_id="rt-1"), **changes})


def incomplete_body(reason="quote_unavailable", **changes):
    return sim_body(**{**dict(record_id="sim-0000030", opportunity_id="sig-30", side=None, reference_price=None,
                              stop_price=None, target_price=None, incomplete_reason=reason), **changes})


def no_trade_body(**changes):
    return sim_body(**{**dict(record_id="sim-0000009", baseline_id="no_trade.v1", cohort="self_found",
                              opportunity_id="opp-9", side=None, quantity=None, reference_price=None,
                              stop_price=None, target_price=None, deployment_digest=None), **changes})


def cost(ingest, **changes):
    return ingest.record_cost(RecordAiCostRequest.model_validate(cost_body(**changes)))


def sim(ingest, body=None, **changes):
    return ingest.record_simulated(RecordSimulatedDecisionRequest.model_validate({**(body or sim_body()), **changes}))
