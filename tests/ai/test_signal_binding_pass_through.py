"""SP2c Plan 2 Task 10: the controller copies the signal's deployment binding into the ENTER (ruling 19)."""
import datetime as dt

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import FakeSignals, et
from trader.ai.engine import ProposedDecision
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.signal_intake import SignalIntake, SignalIntakeError, _parse_signal
from trader.ai.store import AiStore
from trader.ai.submitter import build_body

D = {"deployment_digest": "sha256:" + "b" * 64, "deployment_version": "sha256:" + "d" * 64,
     "source_digest": "sha256:" + "a" * 64}
RAW = {"cursor": 1, "source_event_id": "sig-" + "0" * 32, "strategy_name": "aidv-dddddddddddddddd",
       "conid": 265598, "action": "BUY", "probability": 0.5, "signal_time": "2026-10-12T15:00:00+00:00",
       "recorded_at": "2026-10-12T15:00:01+00:00", **D}


def test_the_binding_reaches_the_opportunity():
    opportunity = _parse_signal(RAW)
    assert (opportunity.deployment_version, opportunity.source_digest) == (D["deployment_version"],
                                                                            D["source_digest"])


@pytest.mark.parametrize("change", [{"deployment_version": None}, {"source_digest": "x"}])
def test_a_partial_binding_is_malformed(change):
    with pytest.raises(SignalIntakeError):
        _parse_signal({**RAW, **change})


@pytest.mark.parametrize("change", [{"deployment_digest": None}])
def test_a_binding_missing_its_base_digest_is_malformed(change):
    with pytest.raises(SignalIntakeError):
        _parse_signal({**RAW, **change})


def test_the_enter_body_carries_the_binding():
    decision = ProposedDecision(action_key="enter:265598", action="ENTER", conid=265598, side="BUY",
                                decider="jev", evidence_digest="sha256:" + "e" * 64,
                                deployment_digest=D["deployment_digest"], policy_revision=1, stop_price=98.0,
                                deployment_version=D["deployment_version"], source_digest=D["source_digest"])
    body = build_body(decision, decision_id="dec-00000001",
                      expires_at=dt.datetime(2026, 10, 12, 15, 5, tzinfo=dt.timezone.utc))
    assert (body["deployment_version"], body["source_digest"]) == (D["deployment_version"], D["source_digest"])


@pytest.mark.asyncio
async def test_a_bound_signal_keeps_its_binding_through_intake(tmp_path):
    clock = FakeClock(et(11, 0, 30))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    signals = FakeSignals()
    signals.add(**D)
    intake = SignalIntake(store=store, supervisor=signals, clock=clock)
    assert len(await intake.poll()) == 1
    ((opportunity, state),) = await intake.open_opportunities()
    assert state == "NEW"
    assert (opportunity.deployment_digest, opportunity.deployment_version, opportunity.source_digest) == (
        D["deployment_digest"], D["deployment_version"], D["source_digest"])


@pytest.mark.parametrize("bar_size", ["15 min", 15, ""])
def test_an_unknown_bar_size_is_malformed(bar_size):                          # issue #146
    with pytest.raises(SignalIntakeError, match="bar_size"):
        _parse_signal({**RAW, "bar_size": bar_size})


def test_the_bar_size_reaches_the_opportunity_and_its_absence_is_kept():
    assert _parse_signal({**RAW, "bar_size": "5 mins"}).bar_size == "5 mins"
    assert _parse_signal(RAW).bar_size is None
