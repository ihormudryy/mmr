"""SP2 Plan 6 Task 2: fresh quote-authority evidence, the code-owned ceiling and the read seam (spec 5.3, 10, 11)."""
import dataclasses
import datetime as dt

import pytest

from tests.ai.decisions.fakes import (
    AAPL, DISCRETIONARY_DIGEST, LIMITS, NOW, STRATEGY_DIGEST, FakeReads, entry_quote_reply, trader_down,
)
from tests.ai.fakes import FakeClock
from trader.ai.evidence import (
    SP1_ADV_FRACTION, AccountFacts, Bracket, DeploymentFacts, EntrySource, EvidenceRefused, PolicyFacts,
    complete_entry_evidence, estimate_entry_ceiling, evidence_digest, fresh_quote, gather_entry_evidence,
    price_entry,
)
from trader.ai.replay import ReplayEvidence, ReplayRecorder, ReplaySession
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore
from trader.ai.tools import LiveTools, ReplayTools, ToolUnavailable
from trader.automation.risk_limits import RiskLimits

SOURCE = EntrySource(kind="strategy", conid=AAPL, deployment_digest=STRATEGY_DIGEST, stop_fraction=0.02,
                     target_fraction=0.04, median_dollar_volume=None, facts={"strategy": "orb"}, untrusted=())
DISCRETIONARY = dataclasses.replace(SOURCE, kind="discretionary", deployment_digest=DISCRETIONARY_DIGEST,
                                    median_dollar_volume=100_000_000.0)


def live_tools(tmp_path, reads, unit_key="dec-" + "1" * 32):
    clock = FakeClock(NOW)
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    return LiveTools(unit_key=unit_key, reads=reads, recorder=ReplayRecorder(store), clock=clock, gateway=None,
                     deadline=None), store


@pytest.mark.parametrize("reply,code", [
    ({**entry_quote_reply(), "quote": None}, "QUOTE_UNAVAILABLE"), (entry_quote_reply(conid=1), "QUOTE_UNAVAILABLE"),
    ({**entry_quote_reply(), "accepted_feeds": []}, "QUOTE_UNAVAILABLE"),
    (entry_quote_reply(bid=0.0), "QUOTE_INVALID"), (entry_quote_reply(bid=231.0), "QUOTE_INVALID"),
    (entry_quote_reply(session_state="halted"), "QUOTE_NOT_CONTINUOUS"), (entry_quote_reply(at=None), "QUOTE_STALE"),
    (entry_quote_reply(at=NOW - dt.timedelta(seconds=16)), "QUOTE_STALE"),
    (entry_quote_reply(at=NOW + dt.timedelta(seconds=6)), "QUOTE_STALE"),
    (entry_quote_reply(at=NOW.replace(tzinfo=None)), "QUOTE_STALE"),
    (entry_quote_reply(feed="delayed"), "QUOTE_FEED_NOT_ACCEPTED"),
    (entry_quote_reply(feed="iex_realtime"), "QUOTE_FEED_NOT_ACCEPTED"),          # no paper fallback on the trader
])
def test_a_quote_must_be_fresh_sane_and_of_an_accepted_feed(reply, code):
    with pytest.raises(EvidenceRefused) as exc:
        fresh_quote(reply, AAPL, NOW, 15)
    assert exc.value.code == code


def test_the_trader_decides_the_feed_set_and_the_feed_reaches_the_digest():                # owner #74
    iex = fresh_quote(entry_quote_reply(feed="iex_realtime", accepted=("iex_realtime", "live")), AAPL, NOW, 15)
    live = fresh_quote(entry_quote_reply(), AAPL, NOW, 15)
    assert (iex.feed, live.feed) == ("iex_realtime", "live")
    body = {"v": "entry_evidence.v1", "conid": AAPL}
    assert (evidence_digest({**body, "quote": dataclasses.asdict(iex)})
            != evidence_digest({**body, "quote": dataclasses.asdict(live)}))


def test_the_ceiling_is_sp1_sizing_on_what_the_ai_side_can_read():
    limits = RiskLimits.from_json(LIMITS)
    account = AccountFacts(net_liquidation=100_000.0, positions={AAPL: (2.0, 200.0), 1: (10.0, 300.0)})
    # trade risk 0.002*100000/(230-225.4)=43.4; position (5000-460)/230=19.7; gross (6000-3400)/230=11.3
    assert estimate_entry_ceiling(limits, account, conid=AAPL, price=230.0, stop=225.4, notional_cap=25_000.0,
                                  liquidity_max_shares=None) == 11


def test_the_adv_fraction_matches_sp1():
    from trader.automation.liquidity_policy import MAX_ADV_FRACTION
    assert SP1_ADV_FRACTION == MAX_ADV_FRACTION


@pytest.mark.asyncio
async def test_live_reads_are_recorded_and_replay_serves_them_without_fetching(tmp_path):
    reads = FakeReads()
    live, store = live_tools(tmp_path, reads)
    evidence = await gather_entry_evidence(live, SOURCE, quote_max_age_seconds=15)
    await live.finish("cfg-digest")
    assert (evidence.reference_price, evidence.stop_price, evidence.target_price) == (230.0, 225.4, 239.2)
    assert evidence.policy_revision == 1 and evidence.digest.startswith("sha256:") and evidence.quote.feed == "live"
    session = ReplaySession(ReplayEvidence.load(store, "dec-" + "1" * 32))
    replayed = await gather_entry_evidence(ReplayTools(session, "dec-" + "1" * 32), SOURCE, quote_max_age_seconds=15)
    assert replayed == evidence and len(reads.calls) == 5


@pytest.mark.asyncio
async def test_a_failed_read_is_recorded_and_replays_as_the_same_failure(tmp_path):
    live, store = live_tools(tmp_path, FakeReads(get_ai_risk_policy=trader_down()), unit_key="dec-" + "2" * 32)
    with pytest.raises(ToolUnavailable) as live_error:
        await gather_entry_evidence(live, SOURCE, quote_max_age_seconds=15)
    await live.finish("cfg")
    session = ReplaySession(ReplayEvidence.load(store, "dec-" + "2" * 32))
    with pytest.raises(ToolUnavailable) as replay_error:
        await gather_entry_evidence(ReplayTools(session, "dec-" + "2" * 32), SOURCE, quote_max_age_seconds=15)
    assert (live_error.value.code, replay_error.value.code) == ("TRADER_UNREACHABLE", "TRADER_UNREACHABLE")


@pytest.mark.parametrize("reply", [
    {"latest_published_revision": None, "effective": None, "latest_published": None},
    {"latest_published_revision": True, "effective": LIMITS, "latest_published": LIMITS},
    {"latest_published_revision": 0, "effective": LIMITS, "latest_published": LIMITS}])
def test_no_policy_is_no_accepted_policy(reply):
    with pytest.raises(EvidenceRefused) as exc:
        PolicyFacts.from_reply(reply)
    assert exc.value.code == "NO_ACCEPTED_POLICY"


def test_a_policy_with_unreadable_limits_is_invalid():
    with pytest.raises(EvidenceRefused) as exc:
        PolicyFacts.from_reply({"latest_published_revision": 2, "effective": {"max_positions": 3},
                                "latest_published": None})
    assert exc.value.code == "POLICY_INVALID"
    facts = PolicyFacts.from_reply({"latest_published_revision": 2, "effective": None, "latest_published": LIMITS})
    assert facts.revision == 2 and facts.limits == RiskLimits.from_json(LIMITS)


def strategy_reply(**deployment):
    body = {"decider_verdict": "DEPLOY", "conids": [AAPL], "evidence_order_notional": 25_000.0, **deployment}
    return {"digest": STRATEGY_DIGEST, "error_code": None, "kind": "strategy", "deployment": body}


@pytest.mark.parametrize("reply,code", [
    ({"digest": STRATEGY_DIGEST, "error_code": "DEPLOYMENT_NOT_FOUND", "kind": None, "deployment": None},
     "DEPLOYMENT_NOT_FOUND"),
    ({**strategy_reply(), "kind": "discretionary"}, "DEPLOYMENT_KIND_MISMATCH"),
    (strategy_reply(decider_verdict="SHADOW"), "DEPLOYMENT_NOT_DEPLOYABLE"),
    (strategy_reply(conids=[1]), "CONID_NOT_IN_DEPLOYMENT"),
    (strategy_reply(evidence_order_notional=0.0), "DEPLOYMENT_INVALID")])
def test_deployment_checks_run_before_any_model(reply, code):
    with pytest.raises(EvidenceRefused) as exc:
        DeploymentFacts.from_reply(reply, SOURCE)
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_a_ceiling_below_one_share_is_refused(tmp_path):
    tiny = {"NetLiquidation": {"value": "100.0", "currency": "USD"}}
    live, _ = live_tools(tmp_path, FakeReads(get_account_values=tiny))
    with pytest.raises(EvidenceRefused) as exc:
        await gather_entry_evidence(live, SOURCE, quote_max_age_seconds=15)
    assert exc.value.code == "QUANTITY_BELOW_ONE_SHARE"


def test_discretionary_notional_and_adv_come_from_the_median():
    reply = {"digest": DISCRETIONARY_DIGEST, "error_code": None, "kind": "discretionary",
             "deployment": {"kind": "discretionary", "scope_rule": {"max_order_share_of_dollar_volume": 0.01}}}
    facts = DeploymentFacts.from_reply(reply, DISCRETIONARY)
    notional, liquidity = facts.caps(price=500.0, median_dollar_volume=100_000_000.0)
    assert notional == 1_000_000.0 and liquidity == pytest.approx(SP1_ADV_FRACTION * 100_000_000.0 / 500.0)
    assert liquidity == pytest.approx(500.0)
    for median in (None, 0.0):
        with pytest.raises(EvidenceRefused) as exc:
            facts.caps(price=500.0, median_dollar_volume=median)
        assert exc.value.code == "EVIDENCE_STALE"


@pytest.mark.parametrize("ask,stop_fraction,target_fraction", [(0.05, 0.02, 0.04), (0.01, 0.1, 0.1)])
def test_bracket_must_straddle_the_ask(ask, stop_fraction, target_fraction):
    with pytest.raises(EvidenceRefused) as exc:
        Bracket.around(ask, stop_fraction, target_fraction)
    assert exc.value.code == "BRACKET_INVALID"
    assert Bracket.around(230.0, 0.02, 0.04) == Bracket(225.4, 239.2)


@pytest.mark.parametrize("account", [
    {"NetLiquidation": {"value": "100000.0", "currency": "EUR"}}, {"NetLiquidation": {"value": "nan",
                                                                                      "currency": "USD"}},
    {"NetLiquidation": {"value": "-5", "currency": "USD"}}, {}])
def test_net_liquidation_in_another_currency_is_refused(account):
    with pytest.raises(EvidenceRefused) as exc:
        AccountFacts.from_replies(account, {"positions": []})
    assert exc.value.code == "ACCOUNT_UNAVAILABLE"


def test_positions_are_keyed_by_conid_and_flat_rows_skipped():
    facts = AccountFacts.from_replies(
        {"NetLiquidation": {"value": "1000.0", "currency": "USD"}},
        {"positions": [{"instrument_id": AAPL, "position": 3.0, "average_cost": 200.0},
                       {"instrument_id": 7, "position": 0.0, "average_cost": 10.0}]})
    assert dict(facts.positions) == {AAPL: (3.0, 200.0)}


@pytest.mark.asyncio
async def test_a_priced_entry_survives_a_later_read_failure(tmp_path):
    live, _ = live_tools(tmp_path, FakeReads(get_ai_risk_policy=trader_down()))
    priced = await price_entry(live, SOURCE, quote_max_age_seconds=15)
    assert (priced.reference_price, priced.stop_price, priced.target_price, priced.quote.feed) == (
        230.0, 225.4, 239.2, "live")
    with pytest.raises(ToolUnavailable):
        await complete_entry_evidence(live, SOURCE, priced)


def test_an_entry_source_round_trips_through_json():
    source = dataclasses.replace(SOURCE, untrusted=(("news", "headline"),))
    assert EntrySource.from_json(source.to_json()) == source
    with pytest.raises(ValueError):
        EntrySource.from_json({**source.to_json(), "extra": 1})
