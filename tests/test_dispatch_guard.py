from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.trading.approval_context import (
    ApprovalContext,
    ExecutableMarketEvidence,
    WhatIfEvidence,
)
from trader.trading.command_coordinator import CommandRequest, RiskDirection
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.dispatch_guard import DispatchGuard, DispatchGuardError
from trader.trading.proposal_command_service import ExecutableQuote


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU123"
CONID = 265598


def _position(quantity=10.0, value=2100.0):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=CONID, symbol="AAPL", sec_type="STK",
        exchange="NASDAQ", currency="USD", quantity=quantity,
        average_cost=200.0, market_price=210.0, market_value=value,
        unrealized_pnl=100.0, realized_pnl=0.0, daily_pnl=-10.0,
        deleted=False, revision=1, source_timestamp=NOW,
    )


def _snapshot(*, generation=2, cursor=20, quantity=10.0, mode="paper"):
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=cursor,
        promoted_at=NOW - dt.timedelta(seconds=1), account_id=ACCOUNT,
        account_mode=mode, net_liquidation=100_000.0, daily_pnl=-100.0,
        positions=(_position(quantity=quantity, value=abs(quantity) * 210),),
        working_orders=(),
    )


def _quote(*, price=210.0, age=0.0, feed="live", state="continuous",
           bid=209.9, ask=210.0, bid_size=10_000.0, ask_size=10_000.0):
    return ExecutableQuote(
        conid=CONID, side="ask", price=price,
        market_timestamp=NOW - dt.timedelta(seconds=age),
        feed_type=feed, session_state=state, bid=bid, ask=ask,
        bid_size=bid_size, ask_size=ask_size,
    )


def _approved(*, mode="paper", direction=RiskDirection.INCREASING,
              quantity=5.0, quote=None, snapshot=None, what_if=True):
    quote = quote or _quote()
    return ApprovalContext(
        conid=CONID, side="BUY", quantity=quantity,
        reference_price=210.0, max_drift_bps=50.0,
        risk_direction=direction,
        broker=snapshot or _snapshot(mode=mode),
        market=ExecutableMarketEvidence(quote=quote, received_at=NOW),
        what_if=(WhatIfEvidence(response={"initMarginAfter": 1000.0}, received_at=NOW)
                 if what_if else None),
    )


class _Broker:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def capture(self, account_id):
        return self.snapshot


class _Quotes:
    def __init__(self, quote):
        self.quote = quote

    def executable_quote(self, conid, *, side):
        return self.quote


class _Margin:
    def __init__(self, response=None):
        self.response = response

    def what_if_margin(self, conid, side, quantity):
        return self.response


class _Controls:
    def __init__(self, paused=False):
        self.paused = paused

    def require_unpaused(self, account_id):
        if self.paused:
            raise RuntimeError("paused")


class _Risk:
    def check_leverage(self, margin, net_liq):
        return type("Result", (), {"approved": True, "reason": ""})()


def _request():
    return CommandRequest(
        command_id="cmd-1", action="approve_proposal", account_id=ACCOUNT,
        target_type="proposal", target_id="1", expected_version=1,
        body={"proposal_id": 1},
        source="dashboard", principal="dashboard",
    )


def _guard(*, snapshot=None, quote=None, margin=None, mode="paper", paused=False,
           max_notional=25_000.0):
    policy = CommandAuthorityPolicy(
        enabled=True, live_enabled=(mode == "live"),
        live_account_id=(ACCOUNT if mode == "live" else None),
        max_order_notional=max_notional, max_drift_bps=50.0,
    )
    return DispatchGuard(
        broker=_Broker(snapshot or _snapshot(mode=mode)),
        quotes=_Quotes(quote or _quote()), margin=_Margin(margin),
        controls=_Controls(paused), risk_gate=_Risk(), policy=policy,
        account_id=ACCOUNT, account_mode=mode,
    )


def test_revalidate_returns_permit_with_refreshed_independent_timestamps():
    permit = _guard(margin={"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0}).revalidate(
        _approved(), _request(), NOW
    )

    assert permit.generation_id == 2
    assert permit.source_cursor == 20
    assert permit.quote_timestamp == NOW
    assert permit.what_if_timestamp == NOW


@pytest.mark.parametrize(
    ("quote", "code"),
    [
        (_quote(price=float("nan")), "EXECUTABLE_QUOTE_INVALID"),
        (_quote(age=6), "QUOTE_STALE"),
        (_quote(feed="delayed"), "FEED_NOT_LIVE"),
        (_quote(state="halted"), "SESSION_INCOMPATIBLE"),
        (_quote(bid=211, ask=210), "CROSSED_MARKET"),
    ],
)
def test_live_market_failures_block_dispatch(quote, code):
    guard = _guard(quote=quote, margin={"initMarginAfter": 1000}, mode="live")

    with pytest.raises(DispatchGuardError) as error:
        guard.revalidate(_approved(mode="live", quote=_quote()), _request(), NOW)

    assert error.value.code == code


def test_live_missing_what_if_and_notional_limit_block_dispatch():
    with pytest.raises(DispatchGuardError) as missing:
        _guard(mode="live", margin=None).revalidate(
            _approved(mode="live"), _request(), NOW
        )
    assert missing.value.code == "WHAT_IF_UNAVAILABLE"

    with pytest.raises(DispatchGuardError) as notional:
        _guard(mode="live", margin={"ok": True}, max_notional=500).revalidate(
            _approved(mode="live", quantity=5), _request(), NOW
        )
    assert notional.value.code == "ORDER_NOTIONAL_LIMIT"


def test_paper_notional_limit_blocks_dispatch():
    """#1: the per-order notional ceiling is enforced in PAPER too, not only
    live — the hard per-trade size backstop an autonomous paper loop relies on
    (the risk gate's concentration cap does not bound a fresh entry)."""
    with pytest.raises(DispatchGuardError) as notional:
        _guard(mode="paper", max_notional=500).revalidate(
            _approved(mode="paper", quantity=5), _request(), NOW
        )
    assert notional.value.code == "ORDER_NOTIONAL_LIMIT"


@pytest.mark.parametrize("margin", [{}, {"initMarginAfter": float("nan")},
                                     {"initMarginAfter": 1, "equityWithLoanAfter": float("inf")}])
def test_live_invalid_what_if_blocks_dispatch(margin):
    with pytest.raises(DispatchGuardError) as error:
        _guard(mode="live", margin=margin).revalidate(
            _approved(mode="live"), _request(), NOW
        )

    assert error.value.code == "WHAT_IF_INVALID"


def test_paper_missing_what_if_is_explicitly_recorded_not_zero_filled():
    permit = _guard(mode="paper", margin=None).revalidate(
        _approved(mode="paper"), _request(), NOW
    )

    assert permit.what_if_timestamp is None
    assert permit.warnings == ("WHAT_IF_UNAVAILABLE_PAPER",)


@pytest.mark.parametrize(
    ("quote", "code"),
    [
        (_quote(age=6), "QUOTE_STALE"),
        (_quote(feed="delayed"), "FEED_NOT_LIVE"),
        (_quote(state="halted"), "SESSION_INCOMPATIBLE"),
    ],
)
def test_paper_automation_rechecks_market_readiness_at_dispatch(quote, code):
    request = replace(_request(), action="execute_automated_intent", source="strategy_service")
    with pytest.raises(DispatchGuardError) as error:
        _guard(mode="paper", quote=quote).revalidate(_approved(), request, NOW)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (_snapshot(generation=1, cursor=20), "GENERATION_REGRESSION"),
        (_snapshot(generation=2, cursor=19), "GENERATION_REGRESSION"),
        (_snapshot(generation=2, cursor=20, quantity=11), "BROKER_STATE_CHANGED"),
        (_snapshot(generation=2, cursor=20, mode="live"), "ACCOUNT_MODE_MISMATCH"),
    ],
)
def test_broker_fence_or_target_state_change_blocks_dispatch(snapshot, code):
    with pytest.raises(DispatchGuardError) as error:
        _guard(snapshot=snapshot).revalidate(_approved(), _request(), NOW)

    assert error.value.code == code


def test_pause_is_rechecked_immediately_before_dispatch():
    with pytest.raises(DispatchGuardError) as error:
        _guard(paused=True).revalidate(_approved(), _request(), NOW)

    assert error.value.code == "TRADING_PAUSED"


@pytest.mark.parametrize(
    ("held", "action", "quantity", "permitted"),
    [
        (10.0, "SELL", 10.0, True),
        (10.0, "SELL", 9.0, True),
        (10.0, "SELL", 11.0, False),
        (-10.0, "BUY", 10.0, True),
        (-10.0, "BUY", 11.0, False),
        (0.0, "SELL", 1.0, False),
    ],
)
def test_reducing_permit_never_flips_or_increases_exposure(held, action, quantity, permitted):
    approved = _approved(
        direction=RiskDirection.REDUCING, quantity=quantity,
        snapshot=_snapshot(quantity=held),
    )
    approved = replace(approved, side=action)
    guard = _guard(snapshot=_snapshot(quantity=held))

    if permitted:
        assert guard.revalidate(approved, _request(), NOW).generation_id == 2
    else:
        with pytest.raises(DispatchGuardError) as error:
            guard.revalidate(approved, _request(), NOW)
        assert error.value.code == "REDUCTION_NOT_MONOTONIC"


# --- Allocation ceiling re-check at dispatch (issue #47) ----------------------

from types import SimpleNamespace

from trader.promotion.allocation_policy import AllocationPolicy
from trader.trading.approval_context import AllocationDispatchEvidence

ARTIFACT_ID = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _evidence(ceiling=0.06, authority_digest=None):
    return AllocationDispatchEvidence(
        artifact_digest=ARTIFACT_ID, artifact_max_gross=0.06,
        authority_digest=authority_digest, effective_gross_ceiling=ceiling,
    )


def _authority(max_gross):
    return SimpleNamespace(
        account_id=ACCOUNT, account_mode="paper", artifact_digest=ARTIFACT_ID,
        max_gross_allocation=max_gross, authority_digest=None, stage="CANARY",
        expires_at=dt.datetime(2099, 1, 1, tzinfo=UTC),
    )


def _automated_request():
    return CommandRequest(
        command_id="cmd-auto", action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id="i-1", expected_version=None,
        body={}, source="strategy_service",
    )


def _allocation_guard(authority=None, lookup=None):
    guard = _guard(margin={"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0})
    guard._allocation_policy = AllocationPolicy(now=lambda: NOW)
    guard._allocation_authority_lookup = lookup or (lambda account, artifact: authority)
    return guard


def _with_allocation(approved, evidence):
    return replace(approved, allocation=evidence)


def test_dispatch_refuses_when_ceiling_tightened_after_approval():
    # The entry is about 3.2% of equity: it fit under 6% and does not fit under 2%.
    guard = _allocation_guard(authority=_authority(0.02))
    approved = _with_allocation(_approved(), _evidence(ceiling=0.06))

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(approved, _automated_request(), NOW)

    assert caught.value.code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"


def test_dispatch_passes_when_ceiling_is_unchanged():
    guard = _allocation_guard(authority=None)
    approved = _with_allocation(_approved(), _evidence(ceiling=0.06))

    assert guard.revalidate(approved, _automated_request(), NOW).generation_id == 2


def test_dispatch_passes_when_ceiling_tightened_but_order_still_fits():
    guard = _allocation_guard(authority=_authority(0.05))
    approved = _with_allocation(_approved(), _evidence(ceiling=0.06))

    assert guard.revalidate(approved, _automated_request(), NOW).generation_id == 2


def test_dispatch_keeps_refusing_a_looser_ceiling_than_approved():
    guard = _allocation_guard(authority=_authority(0.09))
    approved = _with_allocation(_approved(), _evidence(ceiling=0.03))

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(approved, _automated_request(), NOW)

    assert caught.value.code == "ALLOCATION_CEILING_CHANGED"


def test_reducing_order_is_not_blocked_after_ceiling_tightens():
    guard = _allocation_guard(authority=_authority(0.001))
    approved = replace(
        _with_allocation(_approved(direction=RiskDirection.REDUCING, quantity=5.0),
                         _evidence(ceiling=0.06)),
        side="SELL",
    )

    assert guard.revalidate(approved, _automated_request(), NOW).generation_id == 2


def test_automated_entry_without_allocation_evidence_is_refused():
    guard = _allocation_guard(authority=None)

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_approved(), _automated_request(), NOW)

    assert caught.value.code == "ALLOCATION_EVIDENCE_MISSING"


def test_unreadable_allocation_authority_refuses_the_entry():
    def broken_lookup(account, artifact):
        raise RuntimeError("store offline")

    guard = _allocation_guard(lookup=broken_lookup)
    approved = _with_allocation(_approved(), _evidence())

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(approved, _automated_request(), NOW)

    assert caught.value.code == "ALLOCATION_EVIDENCE_UNAVAILABLE"


def test_manual_proposal_entry_without_allocation_evidence_is_unchanged():
    guard = _allocation_guard(authority=_authority(0.001))

    assert guard.revalidate(_approved(), _request(), NOW).generation_id == 2


def test_no_allocation_policy_keeps_todays_behaviour():
    guard = _guard(margin={"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0})

    assert guard.revalidate(_approved(), _automated_request(), NOW).generation_id == 2


def test_suspended_authority_in_the_store_refuses_the_entry(tmp_path):
    from trader.data.allocation_authority_store import (
        AllocationAuthorityStore, apply_allocation_authority_migrations,
    )
    from trader.data.domain_journal import DomainJournal
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator

    db = DuckDBConnection.get_instance(str(tmp_path / "auth.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_allocation_authority_migrations(migrator)
    store = AllocationAuthorityStore(journal=journal, db=db, now=lambda: NOW)
    db.execute(
        "INSERT INTO allocation_authorities "
        "(authority_digest, strategy_id, account_id, account_mode, stage, "
        "artifact_digest, allowlist_digest, ruleset_digest, max_gross_allocation, "
        "evidence_digest, public_key_id, operator, reason, issued_at, expires_at, "
        "event, recorded_at) VALUES "
        "('d', 's', ?, 'paper', 'CANARY', ?, 'al', 'ru', 0.0, 'ev', 'k', "
        "'op', 'suspended', ?, ?, 'OVERRIDE', ?)",
        [ACCOUNT, ARTIFACT_ID, NOW, NOW + dt.timedelta(days=1), NOW],
    )
    assert store.active_for(ACCOUNT, ARTIFACT_ID) is None  # why the guard needs more

    guard = _allocation_guard(lookup=store.authority_for_dispatch)
    approved = _with_allocation(_approved(), _evidence(ceiling=0.06))

    with pytest.raises(DispatchGuardError):
        guard.revalidate(approved, _automated_request(), NOW)


# --- Plan 3 Task 2: current risk limits at dispatch (on top of #47) ---------

from trader.automation.risk_limits import PAPER_LIMITS  # noqa: E402


def _limits_guard(current_limits, **guard_kw):
    guard = _allocation_guard(authority=None)
    guard._current_limits = current_limits
    for name, value in guard_kw.items():
        setattr(guard, name, value)
    return guard


def test_current_risk_limits_tighten_the_dispatch_ceiling():
    # 2,100 held + 1,050 new = 3.15% of 100k: fits the approved 6%, not the current 3%.
    guard = _limits_guard(lambda request: replace(PAPER_LIMITS, gross_fraction=0.03))
    approved = _with_allocation(_approved(), _evidence(ceiling=0.06))

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(approved, _automated_request(), NOW)

    assert caught.value.code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"


def test_unchanged_limits_still_dispatch():
    guard = _limits_guard(lambda request: PAPER_LIMITS)
    approved = _with_allocation(_approved(), _evidence(ceiling=0.06))

    assert guard.revalidate(approved, _automated_request(), NOW).generation_id == 2


def test_default_limits_provider_is_the_paper_constant():
    guard = _allocation_guard(authority=None)

    assert guard._current_limits(_automated_request()) == PAPER_LIMITS


def test_looser_current_ceiling_keeps_its_refusal_under_the_new_name():  # R2
    decision = AllocationPolicy(now=lambda: NOW).revalidate_dispatch(
        broker=_snapshot(), approved_broker=_snapshot(), conid=CONID, side="BUY",
        quantity=1.0, entry_price=210.0, authority=None, artifact_max_gross=0.15,
        artifact_digest=ARTIFACT_ID, authority_digest=None, effective_gross_ceiling=0.06,
        risk_limits_gross=0.10,
    )

    assert "ALLOCATION_CEILING_CHANGED" in decision.reason_codes
    assert "ALLOCATION_CEILING_TIGHTENED" not in decision.reason_codes


def test_limits_provider_failure_refuses():
    def boom(request):
        raise RuntimeError("store down")

    guard = _limits_guard(boom)

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_with_allocation(_approved(), _evidence()), _automated_request(), NOW)

    assert caught.value.code == "LIMITS_UNAVAILABLE"


def test_ai_paper_action_gets_the_automated_quote_rules():
    guard = _guard(quote=_quote(feed="delayed"))
    request = replace(_automated_request(), action="submit_ai_paper_decision")

    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_approved(), request, NOW)

    assert caught.value.code == "FEED_NOT_LIVE"


def test_automated_entry_actions_name_both_paths():
    from trader.trading.dispatch_guard import AUTOMATED_ENTRY_ACTIONS

    assert AUTOMATED_ENTRY_ACTIONS == frozenset({"execute_automated_intent", "submit_ai_paper_decision"})


# --- Plan 3 Task 6: ai_paper entry limits re-checked at dispatch ------------

from trader.data.broker_state import BrokerRiskSnapshot as _Snapshot  # noqa: E402
from trader.trading.approval_context import EntryLimitsEvidence  # noqa: E402


def _ai_request():
    return replace(_automated_request(), action="submit_ai_paper_decision",
                   body={"expires_at": (NOW + dt.timedelta(minutes=5)).isoformat()})


def _two_positions():
    """Two held conids, neither the entry's: the entry needs a third slot."""
    others = tuple(replace(_position(), conid=conid, market_value=1_000.0) for conid in (998, 999))
    return replace(_snapshot(), positions=others)


def _ai_entry_approval(*, quantity=5.0, snapshot=None, limits=PAPER_LIMITS):
    evidence = EntryLimitsEvidence(limits=limits, stop_price=200.0, liquidity_max_shares=1e6,
                                   notional_cap=1e9, daily_loss_anchor=100_000.0, high_water_mark=100_000.0)
    return replace(_approved(quantity=quantity, snapshot=snapshot), entry_limits=evidence)


def _entry_guard(current_limits, snapshot=None):
    guard = _guard(snapshot=snapshot, margin={"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0})
    guard._current_limits = current_limits
    return guard


def test_entry_limits_tightened_between_approval_and_dispatch_refuse():
    guard = _entry_guard(lambda r: replace(PAPER_LIMITS, position_fraction=0.02))   # 2,100 held + 1,050 > 2%
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_ai_entry_approval(), _ai_request(), NOW)
    assert caught.value.code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"


@pytest.mark.parametrize("tight,snapshot,code", [
    ({"daily_loss_fraction": 0.001}, None, "DAILY_LOSS"),  # -100 on the 100k anchor
    ({"max_positions": 1}, "two_positions", "LIMIT_TIGHTENED_BEFORE_DISPATCH"),
    ({"trade_risk_fraction": 0.0001}, None,                # 10 risk / 10.00 stop distance = 1 share
     "LIMIT_TIGHTENED_BEFORE_DISPATCH")])
def test_each_tightened_field_is_rechecked(tight, snapshot, code):
    snap = _two_positions() if snapshot == "two_positions" else None
    guard = _entry_guard(lambda r: replace(PAPER_LIMITS, **tight), snapshot=snap)
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_ai_entry_approval(snapshot=snap), _ai_request(), NOW)
    assert caught.value.code == code


def test_unchanged_entry_limits_skip_the_recheck(monkeypatch):
    import trader.automation.ai_paper_sizing as sizing
    monkeypatch.setattr(sizing, "entry_limit_violations",
                        lambda *a, **k: pytest.fail("re-checked unchanged limits"))
    assert _entry_guard(lambda r: PAPER_LIMITS).revalidate(_ai_entry_approval(), _ai_request(), NOW)


def test_a_looser_current_policy_never_loosens_the_approved_entry():
    guard = _entry_guard(lambda r: replace(PAPER_LIMITS, gross_fraction=0.15))
    assert guard.revalidate(_ai_entry_approval(), _ai_request(), NOW).generation_id == 2


def test_entry_limits_provider_failure_refuses():
    def boom(request):
        raise RuntimeError("store down")
    with pytest.raises(DispatchGuardError) as caught:
        _entry_guard(boom).revalidate(_ai_entry_approval(), _ai_request(), NOW)
    assert caught.value.code == "LIMITS_UNAVAILABLE"


# --- Review #31: loss limits are re-checked at dispatch even when limits did not change ---

def _anchored_approval(*, anchor=1_000_000.0, hwm=100_000.0):
    evidence = EntryLimitsEvidence(limits=PAPER_LIMITS, stop_price=200.0, liquidity_max_shares=1e6,
                                   notional_cap=1e9, daily_loss_anchor=anchor, high_water_mark=hwm)
    return replace(_approved(), entry_limits=evidence)


def test_unchanged_limits_still_refuse_a_daily_loss_breached_after_approval():
    # Approved at -100 on a 1m anchor; the broker now shows -6,000 against the 0.5% / 5,000 cap.
    breached = replace(_snapshot(), daily_pnl=-6_000.0)
    guard = _entry_guard(lambda r: PAPER_LIMITS, snapshot=breached)
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_anchored_approval(), _ai_request(), NOW)
    assert caught.value.code == "DAILY_LOSS"


def test_unchanged_limits_still_refuse_a_drawdown_breached_after_approval():
    # 3% drawdown from a 103,200 high-water mark: the broker NLV is 100,000.
    guard = _entry_guard(lambda r: PAPER_LIMITS)
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_anchored_approval(anchor=100_000.0, hwm=103_200.0), _ai_request(), NOW)
    assert caught.value.code == "DRAWDOWN"


@pytest.mark.parametrize("field", ["daily_pnl", "net_liquidation"])
def test_unknown_broker_pnl_refuses_the_entry(field):
    unknown = replace(_snapshot(), **{field: float("nan")})
    guard = _entry_guard(lambda r: PAPER_LIMITS, snapshot=unknown)
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_anchored_approval(), _ai_request(), NOW)
    assert caught.value.code == "LOSS_STATE_UNKNOWN"


# --- Issue #74: the accepted-feed set is explicit -------------------------------

IEX_FEEDS = frozenset({"live", "iex_realtime"})


def _iex_quote(**changes):
    return replace(_quote(feed="iex_realtime"), **changes)


def _feed_guard(quote, *, mode="paper", feeds=IEX_FEEDS):
    guard = _guard(mode=mode, quote=quote)
    return DispatchGuard(
        broker=guard._broker, quotes=guard._quotes, margin=guard._margin, controls=guard._controls,
        risk_gate=guard._risk_gate, policy=guard._policy, account_id=ACCOUNT, account_mode=mode,
        accepted_feeds=feeds,
    )


def test_the_default_guard_refuses_an_iex_quote_for_an_automated_entry():
    with pytest.raises(DispatchGuardError) as caught:
        _guard(quote=_iex_quote()).revalidate(_approved(), _automated_request(), NOW)
    assert caught.value.code == "FEED_NOT_LIVE"


def test_a_paper_guard_with_the_iex_set_dispatches_on_an_iex_quote():
    permit = _feed_guard(_iex_quote()).revalidate(_approved(), _automated_request(), NOW)
    assert permit.quote_timestamp == NOW


@pytest.mark.parametrize(("quote", "code"), [
    (_iex_quote(market_timestamp=NOW - dt.timedelta(seconds=6)), "QUOTE_STALE"),
    (_iex_quote(bid=210.5, ask=210.0), "CROSSED_MARKET"),
    (_iex_quote(session_state="closed"), "SESSION_INCOMPATIBLE"),
    (_quote(feed="delayed"), "FEED_NOT_LIVE"),
])
def test_iex_quotes_meet_the_unchanged_market_checks(quote, code):
    with pytest.raises(DispatchGuardError) as caught:
        _feed_guard(quote).revalidate(_approved(), _automated_request(), NOW)
    assert caught.value.code == code


def test_a_live_account_guard_cannot_accept_the_iex_feed():
    with pytest.raises(ValueError, match="only the live feed"):
        _feed_guard(_iex_quote(), mode="live")


# --- PR #76 review: IB halt veto, and spread and depth on the final quote -------

def _fallback_guard(ib_quote, iex_quote):
    from trader.trading.paper_quote_fallback import FallbackQuoteAuthority

    guard = _feed_guard(ib_quote)
    guard._quotes = FallbackQuoteAuthority(_Quotes(ib_quote), _Quotes(iex_quote), account_mode="paper")
    return guard


@pytest.mark.parametrize(("ib_quote", "code"), [
    (_quote(feed="delayed", state="halted"), "FEED_NOT_LIVE"),
    (_quote(feed="live", state="halted"), "SESSION_INCOMPATIBLE"),
])
def test_an_ib_halt_vetoes_a_fresh_iex_quote_at_dispatch(ib_quote, code):   # thread 4208684073
    guard = _fallback_guard(ib_quote, _iex_quote())
    assert guard._quotes.executable_quote(CONID, side="ask").session_state == "halted"
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_approved(), _automated_request(), NOW)
    assert caught.value.code == code


def _wide(quote):
    return replace(quote, bid=200.0)            # (210 - 200) / 210 = 476 bps; approved at 10 bps


def _no_depth(quote):
    return replace(quote, ask_size=0.0)


def _unknown_depth(quote):
    return replace(quote, ask_size=None)


def _approved_at_10_bps(mode="paper"):
    return _approved(mode=mode, quote=_quote(bid=209.79))


@pytest.mark.parametrize(("change", "code"), [
    (_wide, "SPREAD_BPS"), (_no_depth, "DEPTH_EXCEEDED"), (_unknown_depth, "DEPTH_EXCEEDED"),
])
@pytest.mark.parametrize("final", [_quote(), _iex_quote()], ids=["ib_live", "iex"])
def test_automated_entry_rechecks_spread_and_depth_on_the_final_quote(final, change, code):  # thread 4208690152
    with pytest.raises(DispatchGuardError) as caught:
        _feed_guard(change(final)).revalidate(_approved_at_10_bps(), _automated_request(), NOW)
    assert caught.value.code == code
    assert caught.value.retryable is True


@pytest.mark.parametrize(("change", "code"), [(_wide, "SPREAD_BPS"), (_no_depth, "DEPTH_EXCEEDED")])
def test_live_entry_rechecks_spread_and_depth_on_the_final_quote(change, code):
    guard = _guard(mode="live", quote=change(_quote()), margin={"initMarginAfter": 1000.0})
    with pytest.raises(DispatchGuardError) as caught:
        guard.revalidate(_approved_at_10_bps(mode="live"), _request(), NOW)
    assert caught.value.code == code


def test_depth_must_cover_the_whole_order():
    with pytest.raises(DispatchGuardError) as caught:
        _feed_guard(_quote(ask_size=4.0)).revalidate(_approved(quantity=5.0), _automated_request(), NOW)
    assert caught.value.code == "DEPTH_EXCEEDED"
    assert _feed_guard(_quote(ask_size=5.0)).revalidate(_approved(quantity=5.0), _automated_request(), NOW)


@pytest.mark.parametrize("change", [_wide, _no_depth, _unknown_depth])
def test_manual_paper_proposals_keep_todays_quote_rules(change):
    assert _guard(quote=change(_quote())).revalidate(_approved_at_10_bps(), _request(), NOW)
