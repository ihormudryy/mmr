"""Offline production evidence tests: no broker, configuration, or network."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import exchange_calendars as xcals
import pandas as pd

from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.models import EntryPolicy, ExecutionIntent, StopPolicy, TimeExitPolicy
from trader.data.broker_state import BrokerRiskSnapshot
from trader.trading.approval_context import ApprovalContextError
from trader.trading.proposal_command_service import ExecutableQuote

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, tzinfo=UTC)
ACCOUNT = "DU123"
CONID = 265598
STRATEGY = "paper-test-strategy"


def intent(**changes):
    fields = dict(
        artifact_id="artifact-test", session_id="session-test", bar_id="bar-test",
        signal_id="signal-test", account_mode="paper", conid=CONID, side="BUY",
        requested_quantity=Decimal("7"), risk_fraction=Decimal("0.001"),
        entry_policy=EntryPolicy("LIMIT", Decimal("5"), "DAY"),
        stop_policy=StopPolicy(Decimal("99"), "STP"), target_policy=None,
        time_exit_policy=TimeExitPolicy(10, NOW + dt.timedelta(hours=2)),
        artifact_digest="artifact-digest", eligibility_attestation_digest="eligibility-digest",
        signal_timestamp=NOW, completed_bar_timestamp=NOW - dt.timedelta(minutes=1),
    )
    fields.update(changes)
    digest_fields = {k: asdict(v) if hasattr(v, "__dataclass_fields__") else v for k, v in fields.items()}
    fields["intent_id"] = derive_intent_id(digest_fields)
    fields["command_id"] = derive_command_id(fields["intent_id"])
    return ExecutionIntent(**fields)


def broker(**changes):
    fields = dict(
        generation_id=17, source_cursor=42, promoted_at=NOW,
        account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000.0,
        daily_pnl=-125.0, positions=(), working_orders=(),
    )
    fields.update(changes)
    return BrokerRiskSnapshot(**fields)


def quote(**changes):
    fields = dict(
        conid=CONID, side="BUY", price=100.05, market_timestamp=NOW,
        feed_type="live", session_state="continuous", bid=99.95, ask=100.05,
        bid_size=123.0, ask_size=456.0,
    )
    fields.update(changes)
    return ExecutableQuote(**fields)


class Ports:
    def __init__(self):
        self.snapshot = broker()
        self.quote = quote()
        self.calls = []

    def capture(self, account_id):
        self.calls.append(("broker", account_id))
        return self.snapshot

    def executable_quote(self, conid, *, side):
        self.calls.append(("quote", conid, side))
        return self.quote

    def what_if_margin(self, conid, side, quantity):
        self.calls.append(("margin", conid, side, quantity))
        return {"initMarginAfter": 50.0}


def adapter(ports, **changes):
    from trader.automation.production_evidence import ProductionAutomationEvidence

    args = dict(
        broker=ports, quotes=ports, margin=ports, history=None, journal=None,
        account_id=ACCOUNT, account_mode="paper", strategy_id=STRATEGY,
        max_drift_bps=37.0, now=lambda: NOW,
    )
    args.update(changes)
    return ProductionAutomationEvidence(**args)


def test_approval_captures_real_fenced_broker_quote_and_requested_quantity():
    ports = Ports()
    evidence = adapter(ports)
    approval = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    assert approval.broker is ports.snapshot
    assert approval.quote is ports.quote
    assert approval.quantity == 7.0
    assert approval.reference_price == 100.05
    assert approval.max_drift_bps == 37.0
    assert approval.market.received_at == NOW
    assert approval.what_if.response == {"initMarginAfter": 50.0}
    assert ports.calls == [("broker", ACCOUNT), ("quote", CONID, "BUY"), ("margin", CONID, "BUY", 7.0)]


@pytest.mark.parametrize("intent_changes,config,code", [
    ({"requested_quantity": None}, {}, "QUANTITY_REQUIRED"),
    ({"requested_quantity": Decimal("1e1000")}, {}, "QUANTITY_INVALID"),
    ({"account_mode": "live"}, {}, "PAPER_ONLY"),
    ({}, {"account_mode": "live"}, "PAPER_ONLY"),
    ({}, {"account_id": "U123"}, "PAPER_ONLY"),
    ({}, {"strategy_id": None}, "STRATEGY_REQUIRED"),
    ({}, {"strategy_id": ""}, "STRATEGY_REQUIRED"),
    ({}, {"max_drift_bps": float("nan")}, "DRIFT_INVALID"),
])
def test_invalid_capture_inputs_fail_closed_before_authority_reads(intent_changes, config, code):
    ports = Ports()
    evidence = adapter(ports, **config)  # dormant registration remains possible
    with pytest.raises(ApprovalContextError, match=code):
        evidence.approval_factory(intent=intent(**intent_changes), command=SimpleNamespace())
    assert ports.calls == []


@pytest.mark.parametrize("changes,code", [
    ({"feed_type": "delayed"}, "FEED_NOT_LIVE"),
    ({"feed_type": "frozen"}, "FEED_NOT_LIVE"),
    ({"market_timestamp": NOW - dt.timedelta(seconds=6)}, "QUOTE_STALE"),
    ({"market_timestamp": NOW + dt.timedelta(seconds=31)}, "QUOTE_CLOCK_INVALID"),
    ({"market_timestamp": NOW.replace(tzinfo=None)}, "QUOTE_CLOCK_INVALID"),
    ({"price": float("nan")}, "QUOTE_INVALID"),
    ({"ask": float("inf")}, "QUOTE_INVALID"),
    ({"bid": None}, "QUOTE_INVALID"),
    ({"ask": 99.0}, "QUOTE_INVALID"),
    ({"price": 99.8}, "QUOTE_INVALID"),  # reference/last is not crossable
    ({"conid": 99}, "QUOTE_MISMATCH"),
    ({"side": "SELL"}, "QUOTE_MISMATCH"),
    ({"session_state": "halted"}, "QUOTE_SESSION_INVALID"),
    ({"session_state": "unknown"}, "QUOTE_SESSION_INVALID"),
])
def test_unusable_quote_evidence_is_refused(changes, code):
    ports = Ports()
    ports.quote = quote(**changes)
    with pytest.raises(ApprovalContextError, match=code):
        adapter(ports).approval_factory(intent=intent(), command=SimpleNamespace())


@pytest.mark.parametrize("changes,code", [
    ({"net_liquidation": float("nan")}, "BROKER_EVIDENCE_INVALID"),
    ({"net_liquidation": 0}, "BROKER_EVIDENCE_INVALID"),
    ({"daily_pnl": float("inf")}, "BROKER_EVIDENCE_INVALID"),
    ({"generation_id": 0}, "BROKER_FENCE_INVALID"),
    ({"source_cursor": -1}, "BROKER_FENCE_INVALID"),
    ({"account_mode": "live"}, "PAPER_ONLY"),
])
def test_unusable_broker_evidence_is_refused(changes, code):
    ports = Ports()
    ports.snapshot = broker(**changes)
    with pytest.raises(ApprovalContextError, match=code):
        adapter(ports).approval_factory(intent=intent(), command=SimpleNamespace())


def test_broker_read_failure_does_not_disclose_provider_error():
    ports = Ports()
    def fail(_account_id):
        raise RuntimeError("credential-do-not-expose")
    ports.capture = fail
    with pytest.raises(ApprovalContextError) as caught:
        adapter(ports).approval_factory(intent=intent(), command=SimpleNamespace())
    assert caught.value.code == "EVIDENCE_UNAVAILABLE"
    assert "credential-do-not-expose" not in str(caught.value)


def test_short_sale_is_refused():
    ports = Ports()
    ports.quote = quote(side="SELL", price=99.95)
    with pytest.raises(ApprovalContextError, match="LONG_ONLY"):
        adapter(ports).approval_factory(intent=intent(side="SELL"), command=SimpleNamespace())


@pytest.fixture
def journal(tmp_path):
    from trader.data.domain_journal import DomainJournal
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.promotion.canary_risk import apply_canary_risk_migration
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    result = DomainJournal(db)
    result.migrate(migrator)
    apply_canary_risk_migration(migrator)
    return result


def daily_frame(now=NOW):
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(now.date() - dt.timedelta(days=90), now.date())
    closed = [day for day in sessions if calendar.session_close(day) < pd.Timestamp(now)][-20:]
    return pd.DataFrame(
        {"close": [100.0 + n for n in range(20)], "volume": [1_000_000.0 + 100 * n for n in range(20)],
         "bar_size": "1 day", "what_to_show": 1},
        index=pd.DatetimeIndex(closed).tz_localize("America/New_York").rename("date"),
    )


@pytest.fixture
def history(tmp_path):
    from trader.data.data_access import TickStorage
    from trader.objects import BarSize
    storage = TickStorage(str(tmp_path / "history.duckdb"))
    storage.get_tickdata(BarSize.Days1).write(CONID, daily_frame())
    return storage


def test_session_uses_closed_daily_history_same_quote_and_persisted_hwm(history, journal):
    from trader.automation.session_risk import AutomationSessionState
    from trader.objects import BarSize
    from trader.promotion.canary_risk import CanaryRiskStore
    ports = Ports()
    evidence = adapter(ports, history=history, journal=journal)
    command = SimpleNamespace()
    approved = evidence.approval_factory(intent=intent(), command=command)
    # A mutable authority advances after capture; the session must not recapture.
    ports.snapshot = broker(net_liquidation=75_000.0, generation_id=18, source_cursor=45)
    ports.quote = quote(price=101.0, ask=101.0, ask_size=1.0)
    partial = pd.DataFrame(
        {"close": [99999.0], "volume": [1e12], "bar_size": ["1 day"], "what_to_show": [1]},
        index=pd.DatetimeIndex([NOW.astimezone(dt.timezone.utc).date()]).tz_localize("America/New_York").rename("date"),
    )
    history.get_tickdata(BarSize.Days1).write(CONID, partial)
    intraday = daily_frame().assign(close=1e10, volume=1e10, bar_size="1 min")
    history.get_tickdata(BarSize.Mins1).write(CONID, intraday)
    state = evidence.session_state_factory(intent=intent(), command=command, approval=approved)
    assert isinstance(state, AutomationSessionState)
    assert state.expected_account_id == ACCOUNT
    assert state.opening_stabilization == dt.timedelta(minutes=5)
    assert state.high_water_mark == 100_000.0
    assert state.liquidity.price == approved.quote.price
    assert state.liquidity.top_of_book_depth == 456.0
    expected = daily_frame()
    assert state.liquidity.adv_shares_20d == pytest.approx(expected.volume.mean())
    assert state.liquidity.median_dollar_volume_20d == pytest.approx((expected.close * expected.volume).median())
    assert state.liquidity.spread_bps == pytest.approx((100.05 - 99.95) / 100.05 * 10_000)
    assert state.liquidity.feed_type == "live"
    assert state.liquidity.session_state == "continuous"
    assert len(ports.calls) == 3
    assert CanaryRiskStore(journal, STRATEGY, ACCOUNT).get_high_water_mark() == 100_000.0
    restarted = adapter(ports, history=history, journal=journal)
    later = restarted.approval_factory(intent=intent(), command=command)
    assert restarted.session_state_factory(intent=intent(), command=command, approval=later).high_water_mark == 100_000.0


class FrameHistory:
    def __init__(self, frame):
        self.frame = frame

    def get_tickdata(self, bar_size):
        from trader.objects import BarSize
        assert bar_size == BarSize.Days1
        return self

    def read(self, *, contract, date_range):
        assert contract == CONID
        return self.frame


@pytest.mark.parametrize("change", [
    "missing", "empty", "stale", "short", "gap", "duplicate", "nan_close",
    "inf_volume", "negative_volume", "unknown_bar_size", "non_trade", "naive_index",
])
def test_bad_daily_history_is_not_liquidity_evidence(change, journal):
    frame = daily_frame()
    if change == "empty":
        frame = pd.DataFrame()
    elif change == "stale":
        frame.index -= dt.timedelta(days=40)
    elif change == "short":
        frame = frame.iloc[1:]
    elif change == "gap":
        frame = frame.drop(frame.index[10])
    elif change == "duplicate":
        frame = pd.concat([frame, frame.iloc[[-1]]])
    elif change == "nan_close":
        frame.iloc[0, frame.columns.get_loc("close")] = float("nan")
    elif change == "inf_volume":
        frame.iloc[0, frame.columns.get_loc("volume")] = float("inf")
    elif change == "negative_volume":
        frame.iloc[0, frame.columns.get_loc("volume")] = -1
    elif change == "unknown_bar_size":
        frame["bar_size"] = None
    elif change == "non_trade":
        frame["what_to_show"] = 2
    elif change == "naive_index":
        frame.index = frame.index.tz_localize(None)
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=None if change == "missing" else FrameHistory(frame))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError, match="HISTORY_UNAVAILABLE|HISTORY_INVALID"):
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)


@pytest.mark.parametrize("depth", [None, float("nan"), float("inf"), -1.0])
def test_entry_missing_or_invalid_same_snapshot_depth_fails_closed(depth, journal):
    ports = Ports()
    ports.quote = quote(ask_size=depth)
    evidence = adapter(ports, journal=journal, history=FrameHistory(daily_frame()))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError, match="DEPTH_INVALID"):
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)


def test_unavailable_history_error_is_opaque(journal):
    class FailedHistory(FrameHistory):
        def read(self, **kwargs):
            raise RuntimeError("history-secret-do-not-expose")
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=FailedHistory(None))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError) as caught:
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)
    assert caught.value.code == "HISTORY_UNAVAILABLE"
    assert "history-secret" not in str(caught.value)


@pytest.mark.parametrize("changes", [{"quantity": 1.0}, {"conid": 99}, {"side": "SELL"}])
def test_session_rejects_approval_not_bound_to_this_intent(changes, journal):
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=FrameHistory(daily_frame()))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError, match="APPROVAL_MISMATCH"):
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=replace(approved, **changes))


def test_sell_reduction_does_not_require_entry_history_or_depth(journal):
    from trader.data.broker_state import BrokerPositionRow
    ports = Ports()
    position = BrokerPositionRow(
        account_id=ACCOUNT, conid=CONID, symbol="TEST", sec_type="STK", exchange="NASDAQ",
        currency="USD", quantity=10.0, average_cost=100.0, market_price=100.0,
        market_value=1000.0, unrealized_pnl=0.0, realized_pnl=0.0, daily_pnl=0.0,
        deleted=False, revision=1, source_timestamp=NOW,
    )
    ports.snapshot = broker(positions=(position,))
    ports.quote = quote(side="SELL", price=99.95, bid_size=None, ask_size=None)
    evidence = adapter(ports, journal=journal)
    selling = intent(side="SELL")
    approved = evidence.approval_factory(intent=selling, command=SimpleNamespace())
    assert approved.risk_direction == "REDUCING"
    state = evidence.session_state_factory(intent=selling, command=SimpleNamespace(), approval=approved)
    assert state.liquidity is None
    assert state.high_water_mark == 100_000.0


def test_hwm_failure_is_fail_closed_and_opaque(monkeypatch):
    from trader.promotion.canary_risk import CanaryRiskStore
    def fail(self, value, now):
        raise RuntimeError("private-db-path-or-secret")
    monkeypatch.setattr(CanaryRiskStore, "update_high_water_mark", fail)
    ports = Ports()
    evidence = adapter(ports, history=FrameHistory(daily_frame()))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError) as caught:
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)
    assert caught.value.code == "HWM_UNAVAILABLE"
    assert "private-db-path" not in str(caught.value)


def artifact(**changes):
    from trader.automation.artifact_verifier import VerifiedArtifact
    from trader.automation.strategy_binding import AttestedStrategy
    fields = dict(
        attested_strategy=AttestedStrategy(
            strategy_path="strategies/orb.py", class_name="OpeningRangeBreakout",
            source_digest="src-1", parameters={}, instruments=frozenset({str(CONID)}),
            bar_size="1 min", order_notional=1_000_000.0),
        artifact_id="artifact-test", manifest_digest="artifact-digest", dataset_manifest_digest="dataset-digest",
        parameters={}, allowlist=(str(CONID),), max_gross_allocation=0.1,
        expires_at=NOW + dt.timedelta(days=1), public_key_id="test-key", verification_reason_codes=("OK",),
    )
    fields.update(changes)
    return VerifiedArtifact(**fields)


@pytest.mark.parametrize("ceiling,expected", [(0.1, 0.06), (0.06, 0.06), (0.02, 0.02)])
def test_allocation_is_typed_and_most_restrictive_paper_artifact_ceiling(ceiling, expected):
    from trader.automation.session_risk import AllocationCeiling
    command = SimpleNamespace(body={"max_gross_fraction": 1.0})
    allocation = adapter(Ports()).allocation_factory(
        intent=intent(), artifact=artifact(max_gross_allocation=ceiling), command=command,
    )
    assert isinstance(allocation, AllocationCeiling)
    assert allocation.max_gross_fraction == expected
    assert allocation.authority_digest is None  # do not invent signed allocation authority


@pytest.mark.parametrize("changes", [
    {"max_gross_allocation": float("nan")}, {"max_gross_allocation": float("inf")},
    {"max_gross_allocation": 0.0}, {"max_gross_allocation": -0.1},
    {"artifact_id": "wrong-artifact"}, {"expires_at": NOW},
])
def test_invalid_artifact_cannot_supply_allocation(changes):
    with pytest.raises(ApprovalContextError, match="ALLOCATION_INVALID|ARTIFACT_MISMATCH"):
        adapter(Ports()).allocation_factory(intent=intent(), artifact=artifact(**changes), command=SimpleNamespace())


def test_nonfinite_derived_liquidity_fails_closed(journal):
    # Individual positive finite bars must not overflow into accepted evidence.
    frame = daily_frame().assign(close=1e301, volume=1e7)
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=FrameHistory(frame))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError, match="HISTORY_INVALID"):
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)


def test_quote_that_ages_during_history_read_is_not_returned_as_fresh(journal):
    clock = [NOW]
    class SlowHistory(FrameHistory):
        def read(self, **kwargs):
            clock[0] += dt.timedelta(seconds=6)
            return self.frame
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=SlowHistory(daily_frame()), now=lambda: clock[0])
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    with pytest.raises(ApprovalContextError, match="QUOTE_STALE"):
        evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)


@pytest.mark.parametrize("now", [
    dt.datetime(2026, 7, 19, 15, tzinfo=UTC),  # weekend
    dt.datetime(2026, 11, 27, 17, 55, tzinfo=UTC),  # early close not yet complete
    dt.datetime(2026, 11, 27, 18, 1, tzinfo=UTC),  # early close complete
    dt.datetime(2026, 11, 2, 15, tzinfo=UTC),  # DST changed since previous session
])
def test_history_window_tracks_official_closes_holidays_and_dst(now, journal):
    frame = daily_frame(now)
    ports = Ports()
    ports.quote = quote(market_timestamp=now)
    ports.snapshot = broker(promoted_at=now)
    evidence = adapter(ports, journal=journal, history=FrameHistory(frame), now=lambda: now)
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    state = evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)
    assert state.liquidity.adv_shares_20d == pytest.approx(frame.volume.mean())


def test_hwm_is_scoped_by_exact_strategy_and_account(journal):
    from trader.promotion.canary_risk import CanaryRiskStore
    CanaryRiskStore(journal, "other-strategy", ACCOUNT).update_high_water_mark(999_000.0, NOW)
    CanaryRiskStore(journal, STRATEGY, "DU999").update_high_water_mark(888_000.0, NOW)
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=FrameHistory(daily_frame()))
    approved = evidence.approval_factory(intent=intent(), command=SimpleNamespace())
    state = evidence.session_state_factory(intent=intent(), command=SimpleNamespace(), approval=approved)
    assert state.high_water_mark == 100_000.0


def test_real_typed_evidence_satisfies_session_risk_controller(history, journal):
    from trader.automation.session_risk import SessionRiskController
    ports = Ports()
    evidence = adapter(ports, journal=journal, history=history)
    buy = intent()
    bundle = artifact()
    command = SimpleNamespace()
    approved = evidence.approval_factory(intent=buy, command=command)
    state = evidence.session_state_factory(intent=buy, command=command, approval=approved)
    allocation = evidence.allocation_factory(intent=buy, artifact=bundle, command=command)
    decision = SessionRiskController(now=lambda: NOW).evaluate(buy, bundle, approved, state, allocation)
    assert decision.approved, decision.reason_codes
    assert decision.approved_quantity == Decimal("7")
