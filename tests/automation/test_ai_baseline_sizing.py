"""SP2 Plan 2 Ruling 19: a baseline is sized exactly as a real ai_paper ENTER of the same deployment."""
import dataclasses
import datetime as dt
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import (
    ACCOUNT, CONID, NOW, FakeUniverse, Quotes, SnapshotSequence, order, pos, prepare, quote, secdef, snapshot,
)
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
    values = dict(decider_verdict="DEPLOY", conids=(CONID,), evidence_order_notional=1e9)
    values.update(changes)
    return SimpleNamespace(**values)


def sizer(parts, *, limits=PAPER_LIMITS, dep=None, feeds=LIVE_ONLY_FEEDS, policy=None):
    return AiPaperBaselineSizer(
        broker=parts["broker"], quotes=parts["quotes"], history=parts["history"],
        policy=policy or SimpleNamespace(effective_limits=lambda: limits),
        deployments=SimpleNamespace(get_sealed=lambda digest: dep or deployment()),
        accepted_feeds=feeds, entry_filter=parts["entry_filter"], now=lambda: NOW)


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

