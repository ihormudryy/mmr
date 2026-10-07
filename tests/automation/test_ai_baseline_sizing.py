"""SP2 Plan 2 Ruling 19: a baseline is sized exactly as a real ai_paper ENTER of the same deployment.

Plan 3 Ruling 19: a discretionary deployment is sized as a real discretionary ENTER, on the scope rule (#85).
"""
import dataclasses
import json
import datetime as dt
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import (
    ACCOUNT, CONID, NOW, FakeUniverse, Quotes, SnapshotSequence, order, pos, prepare, quote, secdef, snapshot,
)
from tests.automation.discretionary_world import discretionary_world
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.ai_paper_filter import AiEntryFilter
from trader.trading.trading_filter import TradingFilter
from trader.automation.ai_baseline_sizing import AiPaperBaselineSizer
from trader.automation.ai_risk_policy import PolicyRefused
from trader.automation.liquidity_policy import LiquidityPolicy
from trader.automation.production_evidence import liquidity_from_history
from trader.automation.risk_limits import PAPER_LIMITS
from trader.scoreboard.ports import SizingUnavailable
from trader.trading.approval_context import ApprovalContextError
from trader.trading.quote_feeds import LIVE_ONLY_FEEDS, PAPER_IEX_FEEDS

DIGEST = "sha256:" + "d" * 64


def deployment(**changes):
    values = dict(decider_verdict="DEPLOY", conids=(CONID,), evidence_order_notional=1e9, style="intraday_long")
    values.update(changes)
    return SimpleNamespace(**values)


def sizer(parts, *, limits=PAPER_LIMITS, dep=None, feeds=LIVE_ONLY_FEEDS, policy=None):
    return AiPaperBaselineSizer(
        broker=parts["broker"], quotes=parts["quotes"], history=parts["history"],
        policy=policy or SimpleNamespace(effective_limits=lambda: limits),
        deployments=SimpleNamespace(get_sealed_any=lambda digest: dep or deployment()),
        accepted_feeds=feeds, entry_filter=parts["entry_filter"], now=lambda: NOW, config=AiPaperConfig(enabled=True))


def size(parts, **changes):
    return sizer(parts, **changes).size(account_id=ACCOUNT, deployment_digest=DIGEST, conid=CONID,
                                        reference_price=100.0, stop_price=98.0)


def _spread_refused(parts):
    """SP1's LiquidityPolicy refuses the same quote on its spread rule."""
    current = parts["quotes"].value
    evidence = liquidity_from_history(parts["history"], CONID, current, NOW)
    reasons = LiquidityPolicy().evaluate(Decimal("1"), evidence, now=NOW).reason_codes
    assert "SPREAD_BPS" in reasons
    raise ApprovalContextError("SPREAD_BPS", "spread above the policy maximum")


def test_baseline_size_equals_the_real_entry_size_when_gross_binds(parts):          # review focus 7
    # 30,000 held in another conid on 1,000,000 equity: the 6 % gross bound leaves (60,000 - 30,000) / 100.10
    # = 299 shares, below the 5 % position bound (499). SP1's prepare_entry and the sizer must agree on 299.
    held = [pos(conid=CONID + 1, quantity=300.0)]
    parts["broker"] = SnapshotSequence(snapshot(positions=held))
    real = prepare(parts, notional=1e9).quantity                  # SP1's own ENTER sizing (prepare_entry)
    parts["broker"] = SnapshotSequence(snapshot(positions=held))
    sized = sizer(parts).size(account_id=ACCOUNT, deployment_digest=DIGEST, conid=CONID,
                              reference_price=100.0, stop_price=98.0)
    assert sized.quantity == real == 299
    assert sized.inputs["max_quantity"] == real and sized.inputs["feed"] == "live"


@pytest.mark.parametrize("change,code", [
    (dict(dep=deployment(decider_verdict="SHADOW")), "DEPLOYMENT_NOT_DEPLOYABLE"),
    (dict(dep=deployment(conids=(1,))), "CONID_NOT_IN_DEPLOYMENT"),
])
def test_a_deployment_a_real_enter_could_not_use_cannot_size(parts, change, code):
    with pytest.raises(SizingUnavailable) as exc:
        sizer(parts, **change).size(account_id=ACCOUNT, deployment_digest=DIGEST, conid=CONID,
                                    reference_price=100.0, stop_price=98.0)
    assert exc.value.code == code
@pytest.mark.parametrize("bad,code", [
    (dict(market_timestamp=NOW - dt.timedelta(seconds=60)), "QUOTE_STALE"),     # an allowed live feed, but stale
    (dict(session_state="halted"), "QUOTE_SESSION_INVALID"),
    (dict(session_state="closed"), "QUOTE_SESSION_INVALID"),
    (dict(bid=100.5, ask=100.0), "QUOTE_INVALID"),                               # not crossable
    (dict(bid=99.0, ask=100.0, price=100.0), "SPREAD_BPS"),                       # 100 bps > MAX_SPREAD_BPS
    (dict(feed_type="iex_realtime"), "FEED_NOT_LIVE"),                           # not in LIVE_ONLY_FEEDS
])
def test_a_quote_a_real_enter_would_refuse_cannot_size(parts, bad, code):        # second PR #75 review
    parts["quotes"] = Quotes(dataclasses.replace(quote(), **bad))
    with pytest.raises(SizingUnavailable) as exc:
        sizer(parts).size(account_id=ACCOUNT, deployment_digest=DIGEST, conid=CONID,
                          reference_price=100.0, stop_price=98.0)
    assert (exc.value.code, exc.value.reason) == (code, "quote_not_executable")
    with pytest.raises(ApprovalContextError):                                    # SP1 refuses the same evidence
        prepare(parts) if code != "SPREAD_BPS" else _spread_refused(parts)


def test_no_quote_cannot_size(parts):
    parts["quotes"] = SimpleNamespace(executable_quote=lambda conid, side: None)
    with pytest.raises(SizingUnavailable) as exc:
        size(parts)
    assert (exc.value.code, exc.value.reason) == ("QUOTE_UNAVAILABLE", "quote_unavailable")


def test_an_iex_quote_sizes_only_with_the_paper_fallback_set(parts):
    parts["quotes"] = Quotes(quote(feed="iex_realtime"))
    assert size(parts, feeds=PAPER_IEX_FEEDS).inputs["feed"] == "iex_realtime"
    with pytest.raises(SizingUnavailable) as exc:
        size(parts)
    assert exc.value.code == "FEED_NOT_LIVE"


def test_full_pending_slots_cannot_size(parts):
    working = [order(conid=CONID + 10 + i) for i in range(PAPER_LIMITS.max_pending_entry_orders)]
    parts["broker"] = SnapshotSequence(snapshot(working=working))
    with pytest.raises(SizingUnavailable) as exc:
        size(parts)
    assert exc.value.code == "MAX_PENDING_ENTRIES"


def test_limits_leaving_less_than_one_share_cannot_size(parts):
    tight = replace(PAPER_LIMITS, gross_fraction=0.0001)          # 100 USD of gross on 1,000,000 equity
    with pytest.raises(SizingUnavailable) as exc:
        size(parts, limits=tight)
    assert exc.value.code == "QUANTITY_BELOW_ONE_SHARE"
    assert exc.value.inputs["max_quantity"] == 0 and exc.value.inputs["limits"]["gross_fraction"] == 0.0001


def test_no_effective_limits_cannot_size(parts):
    def refuse():
        raise PolicyRefused("NO_EFFECTIVE_LIMITS", "no policy is published")
    with pytest.raises(SizingUnavailable) as exc:
        size(parts, policy=SimpleNamespace(effective_limits=refuse))
    assert (exc.value.code, exc.value.reason) == ("NO_EFFECTIVE_LIMITS", "sizing_unavailable")


def test_the_sizer_writes_nothing(parts, monkeypatch):
    import trader.automation.ai_paper_evidence as evidence_module
    journal = parts["journal"]

    def tables():
        db = journal.connect()
        names = [row[0] for row in db.execute("SELECT table_name FROM information_schema.tables").fetchall()]
        return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}

    captured = []
    monkeypatch.setattr(evidence_module, "capture_approval_context",
                        lambda *args, **kwargs: captured.append(1))
    before = tables()
    assert size(parts).quantity >= 1
    assert tables() == before and captured == []


def _denylisted(parts):
    parts["entry_filter"] = AiEntryFilter(universe=FakeUniverse({CONID: secdef()}),
                                          load_filter=lambda: TradingFilter(denylist=["AAPL"]))


@pytest.mark.parametrize("fault,code", [                                       # review 4210055360
    (lambda parts: parts.update(broker=SnapshotSequence(snapshot(mode="live"))), "PAPER_ONLY"),
    (lambda parts: parts.update(broker=SnapshotSequence(snapshot(generation=0))), "BROKER_FENCE_INVALID"),
    (lambda parts: parts.update(broker=SnapshotSequence(snapshot(cursor=-1))), "BROKER_FENCE_INVALID"),
    (lambda parts: parts.update(broker=SnapshotSequence(snapshot(net_liquidation=0.0))), "BROKER_EVIDENCE_INVALID"),
    (_denylisted, "TRADING_FILTER_DENIED"),
])
def test_evidence_a_real_enter_refuses_cannot_size_a_baseline(parts, fault, code):
    fault(parts)
    with pytest.raises(SizingUnavailable) as exc:
        size(parts)
    assert (exc.value.code, exc.value.reason) == (code, "sizing_unavailable")
    fault(parts)                                                               # a fresh broker sequence
    with pytest.raises(ApprovalContextError, match=code):                        # SP1's ENTER refuses it too
        prepare(parts)


def test_a_non_paper_account_id_cannot_size(parts):
    with pytest.raises(SizingUnavailable) as exc:
        sizer(parts).size(account_id="U1234567", deployment_digest=DIGEST, conid=CONID,
                          reference_price=100.0, stop_price=98.0)
    assert exc.value.code == "PAPER_ONLY"



# -- discretionary deployments (Plan 3 Ruling 19, issue #85) ----------------------------------------

def world_sizer(world, *, scope="world", history=None):
    """The world's own broker, quotes, policy, store, filter, config and scope service; by default no local
    history to read."""
    world.start_session()                       # a real ENTER opens the session before it sizes
    return AiPaperBaselineSizer(
        broker=world.broker, quotes=world.quotes, history=history, policy=world.policy,
        deployments=world.deployments, accepted_feeds=world.accepted_feeds, entry_filter=world.entry_filter,
        now=world.clock, scope=world.scope if scope == "world" else scope, config=world.service._config)


def size_discretionary(world, *, scope="world"):
    return world_sizer(world, scope=scope).size(account_id=ACCOUNT, deployment_digest=world.ddigest, conid=CONID,
                                                reference_price=100.0, stop_price=98.0)


def test_a_discretionary_baseline_equals_the_real_discretionary_enter_when_the_scope_cap_binds(tmp_path):
    world = discretionary_world(tmp_path, rule={"max_order_share_of_dollar_volume": 0.0004})   # $40,000
    sized = size_discretionary(world)
    receipt = world.submit(deployment_digest=world.ddigest, stop_price=98.0)
    assert receipt.state == "SUBMITTED", receipt
    # floor(40,000 / 100.10) = 399, below the 5 % position bound (499): the scope rule's cap binds.
    assert sized.quantity == receipt.outcome["quantity"] == 399
    assert sized.inputs["notional_cap"] == pytest.approx(40_000.0)


def test_a_discretionary_baseline_records_nothing(tmp_path):
    world = discretionary_world(tmp_path)
    assert size_discretionary(world).quantity >= 1
    assert world.db.execute("SELECT COUNT(*) FROM discretionary_scope_checks", fetch="one") == (0,)


@pytest.mark.parametrize("change,part", [
    (lambda w: w.contracts.set(CONID, primary_exchange="AMEX"), "exchange"),
    (lambda w: w.volumes.set(volume=300_000.0), "dollar_volume"),
    (lambda w: w.filter_file.write(denylist=["AAPL"]), "trading_filter"),
    (lambda w: w.contracts.fail_with("IB contract details failed: TimeoutError"), "evidence_stale"),
])
def test_an_instrument_outside_the_scope_rule_is_never_sized(tmp_path, change, part):
    world = discretionary_world(tmp_path)
    change(world)
    with pytest.raises(SizingUnavailable) as exc:
        size_discretionary(world)
    assert (exc.value.code, exc.value.reason, exc.value.inputs["part"]) == (
        "OUT_OF_DISCRETIONARY_SCOPE", "sizing_unavailable", part)
    receipt = world.submit(deployment_digest=world.ddigest)              # the real ENTER refuses it too
    assert (receipt.error_code, receipt.outcome["detail"]["part"]) == ("OUT_OF_DISCRETIONARY_SCOPE", part)


def test_without_a_scope_service_a_discretionary_baseline_cannot_size(tmp_path):
    world = discretionary_world(tmp_path)
    with pytest.raises(SizingUnavailable) as exc:
        size_discretionary(world, scope=None)
    assert (exc.value.code, exc.value.inputs["part"]) == ("OUT_OF_DISCRETIONARY_SCOPE", "evidence_stale")


def test_a_strategy_deployment_never_reads_the_scope_rule(tmp_path):
    world = discretionary_world(tmp_path)
    sized = world_sizer(world, history=world.evidence.inner._history).size(account_id=ACCOUNT, deployment_digest=world.digest, conid=CONID,
                        reference_price=100.0, stop_price=98.0)
    receipt = world.submit(stop_price=98.0)                              # world.digest: the strategy deployment
    assert sized.quantity == receipt.outcome["quantity"]
    assert world.contracts.calls == 0 and world.volumes.calls == 0


@pytest.mark.parametrize("digest", ["digest", "ddigest"])           # strategy, discretionary
def test_a_style_the_real_enter_refuses_cannot_size(tmp_path, digest):
    world = discretionary_world(tmp_path)
    world.service._config = AiPaperConfig(enabled=True, styles=())      # the deployment's style is not enabled
    with pytest.raises(SizingUnavailable) as exc:
        world_sizer(world, history=world.evidence.inner._history).size(
            account_id=ACCOUNT, deployment_digest=getattr(world, digest), conid=CONID,
            reference_price=100.0, stop_price=98.0)
    assert (exc.value.code, exc.value.reason) == ("STYLE_NOT_ENABLED", "sizing_unavailable")
    assert world.contracts.calls == 0                                    # refused before the scope rule
    assert world.submit(deployment_digest=getattr(world, digest)).error_code == "STYLE_NOT_ENABLED"


def _quote_falls_on_capture(world):
    """$5.10 until the broker is read, then $4.90: below the scope rule's $5 price floor (PR #87 review)."""
    world.quotes.set(bid=5.095, ask=5.10)
    read = world.broker.capture

    def capture(account_id):
        snapshot_ = read(account_id)
        world.quotes.set(bid=4.895, ask=4.90)
        return snapshot_
    world.broker.capture = capture


def test_a_baseline_is_sized_only_on_the_quote_the_scope_rule_checked(tmp_path):     # PR #87 thread 4211628563
    from tests.scoreboard.ingest_world import FakeExperiments, make_ingest, sim, sim_body
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.scoreboard.schema import apply_scoreboard_migrations
    from trader.scoreboard.store import ScoreboardStore

    world = discretionary_world(tmp_path)
    sizer_ = world_sizer(world)
    _quote_falls_on_capture(world)
    scoreboard = DuckDBConnection(str(tmp_path / "scoreboard.duckdb"))
    apply_scoreboard_migrations(SchemaMigrator(scoreboard))
    store = ScoreboardStore(scoreboard, now=world.clock)
    experiments = FakeExperiments()
    experiments.record.account_id = ACCOUNT
    ingest = make_ingest(store, experiments=experiments, sizer=sizer_)
    body = sim_body(baseline_id="fixed_rule.v1", cohort="self_found", opportunity_id="opp-1",
                    deployment_digest=world.ddigest, conid=CONID, reference_price=5.10, stop_price=4.70,
                    target_price=5.50)
    assert sim(ingest, body)["status"] == "INSERTED"
    (row,) = store.fetch("simulated_decisions", {})
    (outcome,) = store.fetch("simulated_outcomes", {})
    assert (row["quantity"], row["quantity_source"]) == (None, None)
    sizing = json.loads(row["sizing_json"])
    assert (sizing["code"], sizing["part"]) == ("OUT_OF_DISCRETIONARY_SCOPE", "price")
    assert (outcome["status"], outcome["reason"]) == ("INCOMPLETE", "sizing_unavailable")
    _quote_falls_on_capture(world)                                       # the real ENTER on the same sequence
    receipt = world.submit(deployment_digest=world.ddigest, stop_price=4.70)
    assert (receipt.state, receipt.error_code, receipt.outcome["detail"]["part"]) == (
        "REJECTED", "OUT_OF_DISCRETIONARY_SCOPE", "price")
