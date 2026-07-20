"""P3 Task 5 — broker-native protective entry saga.

Contract (plan §Task 5):
* Bracket/OCA construction is deterministic: order refs, transmit ordering,
  parent/child quantities, stop side/price; never unrestricted MARKET entry.
* Broker events (not submit returns) advance working/filled states.
* Entry fill without confirmed working protection trips P1 breaker and starts
  verified liquidation.
* DispatchGuard re-runs immediately before the first IB side effect.
* SessionRiskController.evaluate runs before dispatch.
* Durable saga state survives restart; duplicate broker events are idempotent.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.approval_context import ApprovalContext, ExecutableMarketEvidence
from trader.trading.circuit_breaker import BreakerSignal
from trader.trading.command_coordinator import (
    CommandLedger,
    RiskDirection,
    apply_command_ledger_migration,
)
from trader.trading.dispatch_guard import DispatchGuardError, DispatchPermit
from trader.trading.order_correlation import encode_order_ref
from trader.trading.proposal_command_service import ExecutableQuote

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU111111"
CONID = 265598


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _intent_fields(**overrides):
    entry = EntryPolicy(order_type="LIMIT", limit_offset_bps=Decimal("5"), tif="DAY")
    stop = StopPolicy(stop_price=Decimal("150"), order_type="STP")
    target = TargetPolicy(target_price=Decimal("200"), order_type="LMT")
    time_exit = TimeExitPolicy(max_hold_bars=10, close_by=NOW + dt.timedelta(hours=2))
    fields = dict(
        artifact_id="artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        session_id="session-1",
        bar_id="bar-1",
        signal_id="signal-1",
        account_mode="paper",
        conid=CONID,
        side="BUY",
        requested_quantity=Decimal("10"),
        risk_fraction=Decimal("0.002"),
        entry_policy=entry,
        stop_policy=stop,
        target_policy=target,
        time_exit_policy=time_exit,
        artifact_digest="digest-artifact-1",
        eligibility_attestation_digest="digest-attest-1",
        signal_timestamp=NOW,
        completed_bar_timestamp=NOW - dt.timedelta(minutes=1),
    )
    fields.update(overrides)
    dict_fields = {
        k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
        for k, v in fields.items()
    }
    fields["intent_id"] = derive_intent_id(dict_fields)
    fields["command_id"] = derive_command_id(fields["intent_id"])
    return fields


def make_intent(**overrides) -> ExecutionIntent:
    return ExecutionIntent(**_intent_fields(**overrides))


def _quote(price: float = 160.0) -> ExecutableQuote:
    return ExecutableQuote(
        conid=CONID,
        side="BUY",
        price=price,
        market_timestamp=NOW - dt.timedelta(seconds=1),
        feed_type="realtime",
        session_state="open",
        bid=price - 0.01,
        ask=price + 0.01,
    )


def _snapshot(generation: int = 1) -> BrokerRiskSnapshot:
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=generation, promoted_at=NOW,
        account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000.0,
        daily_pnl=0.0, positions=(), working_orders=(),
    )


def make_approval(
    *,
    quantity: float = 10.0,
    side: str = "BUY",
    price: float = 160.0,
    generation: int = 1,
) -> ApprovalContext:
    quote = _quote(price)
    return ApprovalContext(
        conid=CONID,
        side=side,
        quantity=quantity,
        reference_price=price,
        max_drift_bps=50.0,
        risk_direction=RiskDirection.INCREASING,
        broker=_snapshot(generation),
        market=ExecutableMarketEvidence(quote=quote, received_at=NOW),
        what_if=None,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDispatchGuard:
    def __init__(self):
        self.calls = 0
        self._error: Optional[Exception] = None
        self.permit = DispatchPermit(
            generation_id=1, source_cursor=1,
            quote_timestamp=NOW, what_if_timestamp=None,
        )

    def fail_with(self, exc: Exception):
        self._error = exc

    def revalidate(self, approved, request, now):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self.permit


class FakeSessionRisk:
    def __init__(self, *, approved: bool = True, quantity: Decimal = Decimal("10")):
        self.calls = 0
        self._approved = approved
        self._quantity = quantity
        self._signals: tuple = ()

    def reject(self, *codes: str):
        self._approved = False
        self._reason = codes

    def evaluate(self, intent, artifact, approval_context, session_state, allocation):
        self.calls += 1
        if not self._approved:
            return SimpleNamespace(
                approved=False,
                reason_codes=getattr(self, "_reason", ("RISK_REJECTED",)),
                approved_quantity=None,
                breaker_signals=self._signals,
            )
        return SimpleNamespace(
            approved=True,
            reason_codes=(),
            approved_quantity=self._quantity,
            breaker_signals=(),
            equity_risk_fraction=0.001,
        )


class FakeBracketDispatch:
    """Records bracket submits; never talks to IB."""

    def __init__(self):
        self.calls: list[dict] = []
        self._raise: Optional[BaseException] = None
        self._reject: Optional[str] = None
        self._next_id = 5001

    def raise_on_submit(self, exc: BaseException):
        self._raise = exc

    def reject_with(self, message: str):
        self._reject = message

    def submit_bracket(self, *, plan, intent, account_id: str):
        if self._raise is not None:
            raise self._raise
        if self._reject is not None:
            from trader.trading.command_coordinator import BrokerRejectedError
            raise BrokerRejectedError(self._reject)
        self.calls.append({
            "plan": plan,
            "intent_id": intent.intent_id,
            "account_id": account_id,
            "order_ref": plan.order_ref,
            "order_group_id": plan.order_group_id,
        })
        n = len(plan.legs)
        ids = list(range(self._next_id, self._next_id + n))
        self._next_id += n
        return SimpleNamespace(order_ids=ids, order_group_id=plan.order_group_id,
                               order_ref=plan.order_ref)


class FakeBreaker:
    def __init__(self):
        self.signals: list[BreakerSignal] = []

    def record(self, signal: BreakerSignal):
        self.signals.append(signal)
        return SimpleNamespace(state="TRIPPED", reason_code=signal.kind)


class FakeLiquidation:
    def __init__(self):
        self.starts: list[tuple] = []

    def start(self, account_id, cause_command_id, deadline):
        self.starts.append((account_id, cause_command_id, deadline))
        return SimpleNamespace(
            account_id=account_id, cause_command_id=cause_command_id,
            state="REQUESTED", deadline=deadline,
        )


class FakeCommandRequest:
    def __init__(self, command_id: str, account_id: str = ACCOUNT):
        self.command_id = command_id
        self.account_id = account_id
        self.action = "execute_automated_intent"
        self.source = "strategy_service"
        self.body = {}
        self.expected_version = None


def _build_saga(tmp_path: Path, **overrides):
    from trader.automation.protective_order_saga import (
        ProtectiveOrderSaga,
        apply_protective_order_saga_migration,
    )

    db = DuckDBConnection.get_instance(str(tmp_path / "saga.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_protective_order_saga_migration(migrator)
    ledger = CommandLedger(journal)

    guard = overrides.pop("guard", FakeDispatchGuard())
    risk = overrides.pop("risk", FakeSessionRisk())
    dispatch = overrides.pop("dispatch", FakeBracketDispatch())
    breaker = overrides.pop("breaker", FakeBreaker())
    liquidation = overrides.pop("liquidation", FakeLiquidation())
    now = overrides.pop("now", lambda: NOW)

    saga = ProtectiveOrderSaga(
        journal=journal,
        ledger=ledger,
        dispatch=dispatch,
        dispatch_guard=guard,
        session_risk=risk,
        breaker=breaker,
        liquidation=liquidation,
        account_id=ACCOUNT,
        account_mode="paper",
        now=now,
        db=db,
        **overrides,
    )
    return saga, guard, risk, dispatch, breaker, liquidation, journal, ledger, db


# ---------------------------------------------------------------------------
# Bracket / OCA construction (pure)
# ---------------------------------------------------------------------------

def test_bracket_plan_uses_deterministic_order_ref_and_transmit_ordering():
    from trader.automation.protective_order_saga import build_bracket_plan

    intent = make_intent()
    order_group_id = f"og-{intent.command_id}"
    plan = build_bracket_plan(
        intent,
        quantity=Decimal("10"),
        limit_price=Decimal("160.08"),
        order_group_id=order_group_id,
    )

    assert plan.order_group_id == order_group_id
    assert plan.order_ref == encode_order_ref(order_group_id)
    assert plan.exit_type == "BRACKET"
    assert plan.oca_group == f"oca-{order_group_id}"

    roles = [leg.role for leg in plan.legs]
    assert roles == ["entry", "take_profit", "stop"]

    entry, tp, stop = plan.legs
    assert entry.transmit is False
    assert tp.transmit is False
    assert stop.transmit is True  # last child transmits the whole bracket

    assert entry.action == "BUY"
    assert tp.action == "SELL"
    assert stop.action == "SELL"
    assert entry.quantity == tp.quantity == stop.quantity == Decimal("10")
    assert stop.stop_price == Decimal("150")
    assert tp.limit_price == Decimal("200")
    assert entry.limit_price == Decimal("160.08")
    assert entry.order_type == "LMT"
    assert stop.order_type == "STP"
    assert tp.parent_role == "entry"
    assert stop.parent_role == "entry"
    assert tp.oca_group == plan.oca_group
    assert stop.oca_group == plan.oca_group
    assert entry.oca_group is None
    for leg in plan.legs:
        assert leg.order_ref == plan.order_ref


def test_stop_only_bracket_when_no_target():
    from trader.automation.protective_order_saga import build_bracket_plan

    intent = make_intent(target_policy=None)
    plan = build_bracket_plan(
        intent, quantity=Decimal("5"), limit_price=Decimal("160"),
        order_group_id=f"og-{intent.command_id}",
    )
    assert plan.exit_type == "STOP_LOSS"
    assert [leg.role for leg in plan.legs] == ["entry", "stop"]
    assert plan.legs[0].transmit is False
    assert plan.legs[1].transmit is True
    assert plan.legs[1].oca_group is None  # single child — no OCA pair


def test_marketable_limit_computes_offset_limit_never_market():
    from trader.automation.protective_order_saga import (
        build_bracket_plan,
        compute_entry_limit,
    )

    intent = make_intent(
        entry_policy=EntryPolicy(
            order_type="MARKETABLE_LIMIT",
            limit_offset_bps=Decimal("10"),
            tif="DAY",
        ),
    )
    # BUY marketable = ask * (1 + offset_bps/10000)
    limit = compute_entry_limit(intent, ask=Decimal("160"), bid=Decimal("159.9"))
    assert limit == Decimal("160.16")  # 160 * 1.0010
    plan = build_bracket_plan(
        intent, quantity=Decimal("10"), limit_price=limit,
        order_group_id=f"og-{intent.command_id}",
    )
    assert plan.legs[0].order_type == "LMT"
    assert all(leg.order_type != "MKT" for leg in plan.legs)


def test_build_bracket_plan_rejects_unrestricted_market_entry():
    from trader.automation.protective_order_saga import build_bracket_plan

    intent = make_intent()
    with pytest.raises(ValueError, match="MARKET|unrestricted|LIMIT"):
        build_bracket_plan(
            intent, quantity=Decimal("10"), limit_price=None,
            order_group_id=f"og-{intent.command_id}",
            force_market_entry=True,
        )


def test_sell_entry_stop_is_buy_above_price():
    from trader.automation.protective_order_saga import build_bracket_plan

    intent = make_intent(
        side="SELL",
        stop_policy=StopPolicy(stop_price=Decimal("170"), order_type="STP"),
        target_policy=TargetPolicy(target_price=Decimal("140"), order_type="LMT"),
    )
    plan = build_bracket_plan(
        intent, quantity=Decimal("10"), limit_price=Decimal("160"),
        order_group_id=f"og-{intent.command_id}",
    )
    entry, tp, stop = plan.legs
    assert entry.action == "SELL"
    assert stop.action == "BUY"
    assert tp.action == "BUY"
    assert stop.stop_price == Decimal("170")


# ---------------------------------------------------------------------------
# start(): risk + guard before IB, submit does not advance working
# ---------------------------------------------------------------------------

def test_start_evaluates_session_risk_and_reruns_dispatch_guard_before_submit(tmp_path):
    saga, guard, risk, dispatch, *_ = _build_saga(tmp_path)
    intent = make_intent()
    approval = make_approval()
    request = FakeCommandRequest(intent.command_id)

    state = saga.start(
        intent=intent,
        approval=approval,
        request=request,
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )

    assert risk.calls == 1
    assert guard.calls == 1
    assert len(dispatch.calls) == 1
    # Submit returns order ids, but saga stays SUBMITTING until broker events.
    assert state.state == "SUBMITTING"
    assert state.order_group_id == f"og-{intent.command_id}"
    assert state.order_ref == encode_order_ref(state.order_group_id)
    assert state.submitted_order_ids  # recorded for correlation, not as working proof


def test_start_rejects_when_session_risk_denies_without_ib_side_effect(tmp_path):
    risk = FakeSessionRisk()
    risk.reject("DAILY_LOSS_BREACH")
    saga, guard, risk, dispatch, *_ = _build_saga(tmp_path, risk=risk)
    intent = make_intent()

    state = saga.start(
        intent=intent,
        approval=make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )

    assert state.state == "CLOSED"
    assert state.error_code == "DAILY_LOSS_BREACH"
    assert risk.calls == 1
    assert guard.calls == 0
    assert dispatch.calls == []


def test_start_rejects_when_dispatch_guard_fails_before_ib(tmp_path):
    guard = FakeDispatchGuard()
    guard.fail_with(DispatchGuardError("ORDER_NOTIONAL_LIMIT", "too large"))
    saga, guard, risk, dispatch, *_ = _build_saga(tmp_path, guard=guard)
    intent = make_intent()

    state = saga.start(
        intent=intent,
        approval=make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )

    assert state.error_code == "ORDER_NOTIONAL_LIMIT"
    assert guard.calls == 1
    assert dispatch.calls == []
    assert state.state == "CLOSED"


def test_parent_rejection_marks_rejected_without_protection_path(tmp_path):
    dispatch = FakeBracketDispatch()
    dispatch.reject_with("entry rejected by IB")
    saga, _g, _r, _d, _b, liquidation, *_ = _build_saga(tmp_path, dispatch=dispatch)
    intent = make_intent()

    state = saga.start(
        intent=intent,
        approval=make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )
    assert state.state in ("CLOSED", "SAFETY_FAILED", "OUTCOME_UNKNOWN")
    assert state.error_code in ("BROKER_REJECTED", "PARENT_REJECTED")
    # Clean rejection before live exposure — no liquidation
    assert liquidation.starts == []


def test_disconnect_during_submit_is_outcome_unknown(tmp_path):
    dispatch = FakeBracketDispatch()
    dispatch.raise_on_submit(TimeoutError("IB disconnect"))
    saga, *_ = _build_saga(tmp_path, dispatch=dispatch)
    intent = make_intent()

    state = saga.start(
        intent=intent,
        approval=make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )
    assert state.state == "OUTCOME_UNKNOWN"
    assert state.error_code == "DISPATCH_AMBIGUOUS"


# ---------------------------------------------------------------------------
# Broker events advance state
# ---------------------------------------------------------------------------

def _started(tmp_path, **kw):
    saga, guard, risk, dispatch, breaker, liquidation, journal, ledger, db = _build_saga(
        tmp_path, **kw,
    )
    intent = make_intent()
    state = saga.start(
        intent=intent,
        approval=make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )
    return saga, intent, state, breaker, liquidation, dispatch


def _event(
    order_group_id: str,
    *,
    leg: str,
    status: str,
    filled: float = 0.0,
    total: float = 10.0,
    event_id: str | None = None,
    order_id: int = 1,
):
    from trader.automation.protective_order_saga import BrokerOrderEvent

    return BrokerOrderEvent(
        order_group_id=order_group_id,
        leg=leg,
        status=status,
        filled_quantity=filled,
        total_quantity=total,
        order_id=order_id,
        event_id=event_id or f"{order_group_id}:{leg}:{status}:{filled}",
        source_timestamp=NOW,
    )


def test_broker_events_advance_entry_working_then_protected(tmp_path):
    saga, intent, state, breaker, liquidation, _ = _started(tmp_path)
    og = state.order_group_id

    # Working acks for all legs — still no fill
    state = saga.on_broker_event(_event(og, leg="entry", status="Submitted"))
    assert state.state == "ENTRY_WORKING"
    state = saga.on_broker_event(_event(og, leg="stop", status="Submitted", order_id=2))
    state = saga.on_broker_event(_event(og, leg="take_profit", status="Submitted", order_id=3))
    assert state.state == "ENTRY_WORKING"
    assert state.protection_working is True

    # Full entry fill with protection still working → PROTECTED
    state = saga.on_broker_event(
        _event(og, leg="entry", status="Filled", filled=10.0, total=10.0),
    )
    assert state.state == "PROTECTED"
    assert breaker.signals == []
    assert liquidation.starts == []


def test_partial_parent_fill_adjusts_protection_quantity(tmp_path):
    saga, intent, state, breaker, liquidation, dispatch = _started(tmp_path)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))
    saga.on_broker_event(_event(og, leg="stop", status="Submitted", order_id=2))
    saga.on_broker_event(_event(og, leg="take_profit", status="Submitted", order_id=3))

    state = saga.on_broker_event(
        _event(og, leg="entry", status="Submitted", filled=4.0, total=10.0),
    )
    assert state.state == "PARTIALLY_FILLED"
    assert state.filled_quantity == Decimal("4")
    assert state.protection_quantity == Decimal("4")
    # Protection qty adjust recorded (dispatch may be asked to resize children)
    assert state.protection_adjusted is True
    assert breaker.signals == []


def test_entry_fill_without_working_protection_trips_breaker_and_liquidates(tmp_path):
    saga, intent, state, breaker, liquidation, _ = _started(tmp_path)
    og = state.order_group_id

    # Entry goes working, but stop never confirms
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))
    state = saga.on_broker_event(
        _event(og, leg="entry", status="Filled", filled=10.0, total=10.0),
    )

    assert state.state == "SAFETY_FAILED"
    assert state.error_code == "MISSING_PROTECTION"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert len(liquidation.starts) == 1
    assert liquidation.starts[0][0] == ACCOUNT
    assert liquidation.starts[0][1] == intent.command_id


def test_child_rejection_after_entry_working_is_safety_failed(tmp_path):
    saga, intent, state, breaker, liquidation, _ = _started(tmp_path)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))
    state = saga.on_broker_event(
        _event(og, leg="stop", status="Inactive", order_id=2),
    )
    # Still no fill — wait; but if entry later fills without stop → SAFETY
    state = saga.on_broker_event(
        _event(og, leg="entry", status="Filled", filled=10.0, total=10.0),
    )
    assert state.state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert liquidation.starts


def test_stop_fill_transitions_to_exiting_then_closed(tmp_path):
    saga, intent, state, *_ = _started(tmp_path)
    og = state.order_group_id
    for leg, oid in (("entry", 1), ("stop", 2), ("take_profit", 3)):
        saga.on_broker_event(_event(og, leg=leg, status="Submitted", order_id=oid))
    saga.on_broker_event(_event(og, leg="entry", status="Filled", filled=10.0))
    state = saga.on_broker_event(
        _event(og, leg="stop", status="Filled", filled=10.0, order_id=2),
    )
    assert state.state in ("EXITING", "CLOSED")
    # OCA cancel of TP
    state = saga.on_broker_event(
        _event(og, leg="take_profit", status="Cancelled", order_id=3),
    )
    assert state.state == "CLOSED"


def test_target_fill_closes_with_oca_stop_cancel(tmp_path):
    saga, intent, state, *_ = _started(tmp_path)
    og = state.order_group_id
    for leg, oid in (("entry", 1), ("stop", 2), ("take_profit", 3)):
        saga.on_broker_event(_event(og, leg=leg, status="Submitted", order_id=oid))
    saga.on_broker_event(_event(og, leg="entry", status="Filled", filled=10.0))
    state = saga.on_broker_event(
        _event(og, leg="take_profit", status="Filled", filled=10.0, order_id=3),
    )
    assert state.state in ("EXITING", "CLOSED")
    state = saga.on_broker_event(
        _event(og, leg="stop", status="Cancelled", order_id=2),
    )
    assert state.state == "CLOSED"


def test_cancel_race_on_entry_before_fill_closes_cleanly(tmp_path):
    saga, intent, state, breaker, liquidation, _ = _started(tmp_path)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))
    saga.on_broker_event(_event(og, leg="stop", status="Submitted", order_id=2))
    state = saga.on_broker_event(_event(og, leg="entry", status="Cancelled"))
    assert state.state == "CLOSED"
    assert breaker.signals == []
    assert liquidation.starts == []


def test_duplicate_broker_events_are_idempotent(tmp_path):
    saga, intent, state, breaker, liquidation, _ = _started(tmp_path)
    og = state.order_group_id
    ev = _event(og, leg="entry", status="Submitted", event_id="dup-1")
    s1 = saga.on_broker_event(ev)
    s2 = saga.on_broker_event(ev)
    assert s1.state == s2.state == "ENTRY_WORKING"
    assert s1.revision == s2.revision


def test_resume_restores_durable_state_after_restart(tmp_path):
    saga, intent, state, *_ = _started(tmp_path)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))
    saga.on_broker_event(_event(og, leg="stop", status="Submitted", order_id=2))
    saga.on_broker_event(_event(og, leg="take_profit", status="Submitted", order_id=3))

    # New saga instance over the same DB (restart)
    saga2, *_ = _build_saga(tmp_path)
    resumed = saga2.resume(intent.command_id)
    assert resumed is not None
    assert resumed.state == "ENTRY_WORKING"
    assert resumed.order_group_id == og
    assert resumed.protection_working is True


def test_domain_events_emitted_transactionally_with_state(tmp_path):
    saga, intent, state, *_ = _started(tmp_path)
    db = DuckDBConnection.get_instance(str(tmp_path / "saga.duckdb"))
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))

    rows = db.execute(
        "SELECT event_type, entity_type, correlation_id FROM domain_event_journal "
        "WHERE entity_type = 'automated_order_saga' ORDER BY source_cursor",
        fetch="all",
    )
    assert rows
    assert any(r[0] == "automated_order_saga.updated" for r in rows)
    assert all(r[2] == intent.command_id for r in rows)


def test_migration_30_creates_automated_order_sagas_table(tmp_path):
    from trader.automation.protective_order_saga import (
        PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION,
        apply_protective_order_saga_migration,
    )

    db = DuckDBConnection.get_instance(str(tmp_path / "mig.duckdb"))
    migrator = SchemaMigrator(db)
    assert apply_protective_order_saga_migration(migrator) is True
    assert PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION == 30
    assert apply_protective_order_saga_migration(migrator) is False  # idempotent
    cols = {
        row[1]
        for row in db.execute("PRAGMA table_info('automated_order_sagas')", fetch="all")
    }
    assert {"command_id", "order_group_id", "state", "payload", "updated_at"} <= cols


def test_execution_spec_from_plan_uses_bracket_not_market():
    from trader.automation.protective_order_saga import (
        build_bracket_plan,
        execution_spec_from_plan,
    )

    intent = make_intent()
    plan = build_bracket_plan(
        intent, quantity=Decimal("10"), limit_price=Decimal("160.08"),
        order_group_id=f"og-{intent.command_id}",
    )
    spec = execution_spec_from_plan(plan)
    assert spec["order_type"] == "LIMIT"
    assert spec["exit_type"] == "BRACKET"
    assert spec["limit_price"] == 160.08
    assert spec["stop_loss_price"] == 150.0
    assert spec["take_profit_price"] == 200.0
    assert "MARKET" not in str(spec["order_type"])
