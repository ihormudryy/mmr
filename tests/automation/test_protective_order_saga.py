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
    def __init__(self, *, approved: bool = True, quantity: Decimal = Decimal("10"),
                 effective_gross_ceiling: Optional[float] = None,
                 authority_digest: Optional[str] = None):
        self.calls = 0
        self._ceiling = effective_gross_ceiling
        self._authority_digest = authority_digest
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
            effective_gross_ceiling=self._ceiling,
            authority_digest=self._authority_digest,
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


def test_risk_evaluation_exception_is_a_known_pre_dispatch_rejection(tmp_path):
    def unavailable(*args):
        raise AttributeError("incomplete session evidence")

    saga, guard, _, dispatch, _, _, _, _, _ = _build_saga(
        tmp_path, risk=SimpleNamespace(evaluate=unavailable),
    )
    intent = make_intent()
    state = saga.start(
        intent=intent, approval=make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=object(), session_state=object(), allocation=object(),
    )

    assert state.state == "CLOSED"
    assert state.error_code == "AUTOMATION_RISK_UNAVAILABLE"
    assert saga.resume(intent.command_id) == state
    assert dispatch.calls == []
    assert guard.calls == 0


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


def test_busy_liquidation_keeps_protective_failure_root_for_rescan(tmp_path):
    """Lock held elsewhere: the saga's liquidation is busy but the root survives."""
    import threading

    from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
    from trader.trading.exit_owner import ExitOwnerRegistry
    from trader.trading.liquidation_service import (
        LiquidationBusy, LiquidationRunStore, LiquidationService, apply_liquidation_migration,
    )

    position = BrokerPositionRow(
        account_id=ACCOUNT, conid=CONID, symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", quantity=10.0, average_cost=None, market_price=None,
        market_value=None, unrealized_pnl=None, realized_pnl=None, daily_pnl=None,
        deleted=False, revision=1, source_timestamp=NOW,
    )
    snapshot = BrokerRiskSnapshot(
        generation_id=1, source_cursor=1, promoted_at=NOW, account_id=ACCOUNT,
        account_mode="paper", net_liquidation=100_000.0, daily_pnl=0.0,
        positions=(position,), working_orders=(),
    )
    reduces = []
    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
    apply_liquidation_migration(SchemaMigrator(db))
    liquidation = LiquidationService(
        SimpleNamespace(capture=lambda account_id: snapshot),
        SimpleNamespace(reduce=lambda *args: reduces.append(args), cancel=lambda *args: None,
                        find_orders=lambda *args: [], get_order=lambda entity: None,
                        enumeration_complete=lambda: True, newest_generation=lambda: 1,
                        executed_quantities=lambda *a: {}, unbound_execution_since=lambda *a: False),
        store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db), now=lambda: NOW,
        lock_timeout_seconds=0.05,
    )
    saga, intent, state, breaker, _, _ = _started(tmp_path, liquidation=liquidation)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="entry", status="Submitted"))

    held, release = threading.Event(), threading.Event()

    def hold():
        with liquidation._lock:
            held.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(2.0)
    try:
        with pytest.raises(LiquidationBusy):
            saga.on_broker_event(_event(og, leg="entry", status="Filled", filled=10.0, total=10.0))
    finally:
        release.set()
        holder.join(2.0)

    assert saga.resume(intent.command_id).state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert reduces == []
    receipt = liquidation.rescan()
    assert receipt.cause_command_id == intent.command_id
    assert receipt.state == "VERIFYING"
    assert len(reduces) == 1


# --- Allocation evidence is attached by the saga's capture path (issue #47) ----

def _live_quote():
    return ExecutableQuote(
        conid=CONID, side="ask", price=160.01, market_timestamp=NOW,
        feed_type="live", session_state="continuous", bid=159.99, ask=160.01,
    )


def _saga_artifact(intent):
    return SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                           max_gross_allocation=0.06, parameters={})


def _start_saga(saga, intent, approval=None):
    return saga.start(
        intent=intent,
        approval=approval or make_approval(),
        request=FakeCommandRequest(intent.command_id),
        artifact=_saga_artifact(intent),
        session_state=SimpleNamespace(
            high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None,
        ),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )


def test_saga_attaches_allocation_evidence_before_dispatch_guard(tmp_path):
    class RecordingGuard(FakeDispatchGuard):
        def revalidate(self, approved, request, now):
            self.seen = approved
            return super().revalidate(approved, request, now)

    guard = RecordingGuard()
    risk = FakeSessionRisk(effective_gross_ceiling=0.05, authority_digest="auth-1")
    saga, *_ = _build_saga(tmp_path, guard=guard, risk=risk)
    intent = make_intent()

    _start_saga(saga, intent)

    evidence = guard.seen.allocation
    assert evidence.artifact_digest == intent.artifact_id
    assert evidence.artifact_max_gross == 0.06
    assert evidence.authority_digest == "auth-1"
    assert evidence.effective_gross_ceiling == 0.05


def _real_guard(authority):
    from trader.promotion.allocation_policy import AllocationPolicy
    from trader.trading.command_policy import CommandAuthorityPolicy
    from trader.trading.dispatch_guard import DispatchGuard

    class Broker:
        def capture(self, account_id):
            return _snapshot()

    class Quotes:
        def executable_quote(self, conid, *, side):
            return _live_quote()

    class Margin:
        def what_if_margin(self, conid, side, quantity):
            return {"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0}

    class Controls:
        def require_unpaused(self, account_id):
            return None

    class Risk:
        def check_leverage(self, margin, net_liq):
            return SimpleNamespace(approved=True, reason="")

    return DispatchGuard(
        broker=Broker(), quotes=Quotes(), margin=Margin(), controls=Controls(),
        risk_gate=Risk(),
        policy=CommandAuthorityPolicy(
            enabled=True, live_enabled=False, live_account_id=None,
            max_order_notional=25_000.0, max_drift_bps=50.0,
        ),
        account_id=ACCOUNT, account_mode="paper",
        allocation_policy=AllocationPolicy(now=lambda: NOW),
        allocation_authority_lookup=lambda account, artifact: authority,
    )


def _tight_authority(max_gross):
    return SimpleNamespace(
        account_id=ACCOUNT, account_mode="paper", artifact_digest="artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        max_gross_allocation=max_gross, authority_digest=None, stage="CANARY",
        expires_at=dt.datetime(2099, 1, 1, tzinfo=UTC),
    )


def test_saga_refuses_entry_when_ceiling_tightens_between_approval_and_dispatch(tmp_path):
    # $1,600 entry on $100k equity is 1.6%: fits 6%, does not fit 1%.
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_real_guard(_tight_authority(0.01)),
        risk=FakeSessionRisk(effective_gross_ceiling=0.06),
    )

    state = _start_saga(saga, make_intent(), make_approval(price=160.01))

    assert state.state == "CLOSED"
    assert state.error_code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"
    assert dispatch.calls == []


def test_saga_dispatches_entry_when_ceiling_is_unchanged(tmp_path):
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_real_guard(None),
        risk=FakeSessionRisk(effective_gross_ceiling=0.06),
    )

    state = _start_saga(saga, make_intent(), make_approval(price=160.01))

    assert state.state == "SUBMITTING"
    assert len(dispatch.calls) == 1


def test_saga_refuses_entry_that_only_fits_at_the_quote_not_at_the_entry_limit(tmp_path):
    # 10 x 160.01 = $1,600.10 fits the cap of $1,600.30. The BUY limit sits a few
    # bps above the ask, so the order the broker would see is above the cap.
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_real_guard(None),
        risk=FakeSessionRisk(effective_gross_ceiling=0.016003),
    )

    state = _start_saga(saga, make_intent(), make_approval(price=160.01))

    assert state.state == "CLOSED"
    assert state.error_code == "GROSS_EXPOSURE"
    assert dispatch.calls == []
# ---------------------------------------------------------------------------
# SP1 plan 1 Task 9: close ownership (CLOSE_OWNED), hand-over, release
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402


def _protected(tmp_path, **kw):
    saga, intent, state, breaker, liquidation, dispatch = _started(tmp_path, **kw)
    og = state.order_group_id
    for leg, oid in (("entry", 1), ("stop", 2), ("take_profit", 3)):
        saga.on_broker_event(_event(og, leg=leg, status="Submitted", order_id=oid))
    state = saga.on_broker_event(_event(og, leg="entry", status="Filled", filled=10.0, total=10.0))
    assert state.state == "PROTECTED"
    return saga, intent, state, breaker, liquidation


def _owned_event(order_group_id, *, leg, status, entity, filled=0.0, total=10.0):
    from trader.automation.protective_order_saga import BrokerOrderEvent
    return BrokerOrderEvent(order_group_id, leg, status, filled, total, 2,
                            f"{entity}:{status}:{filled}", NOW, order_entity_id=entity)


def _cancels(og, *entities):
    from trader.trading.liquidation_service import CancelTarget
    return tuple(CancelTarget(entity, og) for entity in entities)


def test_migration_37_adds_ownership_columns_and_groups_table(tmp_path):
    from trader.automation.protective_order_saga import PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION
    *_rest, db = _build_saga(tmp_path)
    assert PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION == 37
    cols = {r[0] for r in db.execute("DESCRIBE automated_order_sagas", fetch="all")}
    assert {"account_id", "conid", "close_root_id"} <= cols
    assert "flatten_requested" in cols
    group_cols = {r[0] for r in db.execute("DESCRIBE automated_order_saga_groups", fetch="all")}
    assert group_cols == {"order_group_id", "command_id", "protection_generation"}
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 37", fetch="one") == ("sp1_saga_close_ownership",)


def test_handover_records_exact_refs_and_generation_and_returns_prices(tmp_path):
    saga, intent, state, *_ = _protected(tmp_path)
    og = state.order_group_id
    info = saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                         cancels=_cancels(og, "og:stop", "og:tp") + _cancels("other", "x"), generation=7, now=NOW)
    owned = saga.resume(intent.command_id)
    assert (owned.state, owned.close_root_id, owned.handover_generation) == ("CLOSE_OWNED", "close-1", 7)
    assert owned.expected_cancel_ids == ("og:stop", "og:tp")
    assert (info.stop_price, info.target_price) == (150.0, 200.0)


def test_expected_cancel_under_close_owned_is_not_an_incident(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop", "og:tp"), generation=7, now=NOW)
    saga.on_broker_event(_owned_event(og, leg="stop", status="Cancelled", entity="og:stop"))
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:tp"))
    assert (state.state, state.stop_rejected) == ("CLOSE_OWNED", False)
    assert breaker.signals == [] and liquidation.starts == []


@pytest.mark.parametrize("status", ["Inactive", "Rejected"])
def test_unexpected_reject_during_handover_is_still_an_incident(tmp_path, status):
    """R14: only Cancelled/ApiCancelled of an expected ref is suppressed."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    state = saga.on_broker_event(_owned_event(og, leg="stop", status=status, entity="og:stop"))
    assert (state.state, state.error_code) == ("SAFETY_FAILED", "PROTECTION_LOST_DURING_CLOSE")
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert liquidation.starts[0][1] == intent.command_id


def test_cancel_of_a_ref_the_close_did_not_ask_for_is_an_incident(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:tp"))
    assert state.state == "SAFETY_FAILED"
    assert len(liquidation.starts) == 1


def test_late_entry_fill_event_while_owned_is_bookkeeping_only(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.on_broker_event(_owned_event(og, leg="stop", status="Cancelled", entity="og:stop"))
    state = saga.on_broker_event(_owned_event(og, leg="entry", status="Filled", entity="og:entry", filled=10.0))
    assert state.state == "CLOSE_OWNED" and breaker.signals == []


def test_unexpected_stop_cancel_without_handover_still_liquidates(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    state = saga.on_broker_event(_event(state.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert state.state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert len(liquidation.starts) == 1


def test_account_takeover_transfers_a_close_owned_saga_and_keeps_its_expected_refs(tmp_path):
    """R14: the account root takes over a saga a scoped close already owns."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.handover_account(account_id=ACCOUNT, close_root_id="kill-1",
                          cancels=_cancels(og, "og:tp"), generation=8, now=NOW)
    owned = saga.resume(intent.command_id)
    assert (owned.state, owned.close_root_id) == ("CLOSE_OWNED", "kill-1")
    assert owned.expected_cancel_ids == ("og:stop", "og:tp")
    saga.close_after_full(close_root_id="kill-1", now=NOW)
    assert saga.resume(intent.command_id).state == "CLOSED"


def test_release_after_partial_protects_the_remainder_with_the_new_legs(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop", "og:tp"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="PreSubmitted",
                               target_group="p-1-reprotect-target-265598-1", target_status="Submitted", now=NOW)
    released = saga.resume(intent.command_id)
    assert (released.state, released.close_root_id, released.protection_generation) == ("PROTECTED", None, 1)
    assert released.protection_quantity == Decimal("6")
    assert released.current_groups == ("p-1-reprotect-stop-265598-1", "p-1-reprotect-target-265598-1")
    state = saga.on_broker_event(_event("p-1-reprotect-stop-265598-1", leg="stop", status="Filled",
                                        filled=6.0, total=6.0, order_id=9))
    assert state.command_id == intent.command_id
    assert state.state in ("EXITING", "CLOSED")
    assert breaker.signals == []


def test_retired_leg_event_does_not_change_current_protection(tmp_path):
    """R14 / #25: a late cancel of the original stop after a release is ignored."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    state = saga.on_broker_event(_event(og, leg="stop", status="Cancelled", order_id=2, event_id="late-old-stop"))
    assert state.state == "PROTECTED"
    assert breaker.signals == [] and liquidation.starts == []
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-2", cancels=(), generation=9, now=NOW)
    saga.release_after_partial(close_root_id="p-2", remaining_quantity=3.0,
                               stop_group="p-2-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    state = saga.on_broker_event(_event("p-1-reprotect-stop-265598-1", leg="stop", status="Cancelled",
                                        order_id=9, event_id="late-first-replacement"))
    assert (state.state, state.protection_quantity) == ("PROTECTED", Decimal("3"))
    assert breaker.signals == []


def test_close_after_full_closes_the_saga_without_error(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    saga.close_after_full(close_root_id="close-1", now=NOW)
    closed = saga.resume(intent.command_id)
    assert (closed.state, closed.error_code) == ("CLOSED", None)
    later = saga.on_broker_event(_event(state.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert later.state == "CLOSED" and breaker.signals == []


def test_ownership_survives_a_restart(tmp_path):
    saga, intent, state, *_ = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    from trader.automation.protective_order_saga import ProtectiveOrderSagaStore
    db = DuckDBConnection.get_instance(str(tmp_path / "saga.duckdb"))
    reloaded = ProtectiveOrderSagaStore(db).load_by_close_root("close-1")
    assert [s.command_id for s in reloaded] == [intent.command_id]


def test_handover_with_no_live_saga_returns_empty_prices(tmp_path):
    saga, *_rest = _build_saga(tmp_path)
    info = saga.handover(account_id=ACCOUNT, conid=999, close_root_id="close-x", cancels=(), generation=1, now=NOW)
    assert (info.stop_price, info.target_price) == (None, None)


def test_pending_replacement_legs_are_bound_before_release_and_their_loss_is_remembered(tmp_path):
    """R2-4 / #25: events of the close's own legs reach the saga before release; a lost leg is an incident at release."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop", "og:tp"), generation=7, now=NOW)
    saga.expect_reprotect(close_root_id="p-1", groups=("p-1-reprotect-stop-265598-1",), now=NOW)
    state = saga.on_broker_event(_owned_event("p-1-reprotect-stop-265598-1", leg="stop", status="Inactive",
                                              entity="p-1-reprotect-stop-265598-1:stop"))
    assert (state.state, state.pending_protection_lost) == ("CLOSE_OWNED", True)
    assert breaker.signals == [] and liquidation.starts == []      # the close escalates on its own evidence
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    released = saga.resume(intent.command_id)
    assert (released.state, released.error_code) == ("SAFETY_FAILED", "PROTECTION_LOST_DURING_CLOSE")
    assert liquidation.starts[0][1] == intent.command_id


@pytest.mark.parametrize("stop_status", ["Cancelled", "PendingCancel", "PendingSubmit", "ApiPending"])
def test_release_with_a_stop_that_is_not_working_at_the_broker_is_an_incident(tmp_path, stop_status):
    """#25: the release takes the legs' broker status; it never assumes they work.

    #22 round 5: the stop row can turn PendingCancel after DONE was committed under the hold and
    before this release reads it. A stop that is pending cancel (or not yet accepted) is not
    protection, although live event handling counts it as working."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status=stop_status,
                               target_group=None, target_status=None, now=NOW)
    assert saga.resume(intent.command_id).state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)


def test_release_with_a_protection_problem_is_an_incident_even_with_a_submitted_stop(tmp_path):
    """#22/#25 round 7: the close found the Submitted stop unlinked or undersized under its release
    hold. The saga takes the safety-failure path instead of recording PROTECTED."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW,
                               protection_problem="p-1-reprotect-stop-265598-1 outstanding 6.0 != position 8.0")
    released = saga.resume(intent.command_id)
    assert (released.state, released.flatten_requested) == ("SAFETY_FAILED", True)
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert liquidation.starts[0][1] == intent.command_id


@pytest.mark.parametrize("taker", ["account", "upgrade"])
def test_cancelling_pending_legs_after_a_takeover_or_upgrade_is_expected(tmp_path, taker):
    """R2-4: the kill (or the upgraded close) cancels the replacement legs; that is not an incident."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.expect_reprotect(close_root_id="p-1", groups=("p-1-reprotect-stop-265598-1",), now=NOW)
    leg = _cancels("p-1-reprotect-stop-265598-1", "p-1-reprotect-stop-265598-1:stop")
    if taker == "account":
        saga.handover_account(account_id=ACCOUNT, close_root_id="kill-1", cancels=leg, generation=8, now=NOW)
    else:
        saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1", cancels=leg, generation=8, now=NOW)
    restarted, *_rest = _build_saga(tmp_path)                       # a restart in between
    state = restarted.on_broker_event(_owned_event("p-1-reprotect-stop-265598-1", leg="stop", status="Cancelled",
                                                   entity="p-1-reprotect-stop-265598-1:stop"))
    assert (state.state, state.pending_protection_lost) == ("CLOSE_OWNED", False)
    assert breaker.signals == []


def test_a_later_cancel_target_is_added_to_the_expected_set(tmp_path):
    """R27 / R2-4: an order the close finds later is handed over before its cancel; an unasked cancel still fails."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=_cancels(og, "og:tp"),
                  generation=8, now=NOW)
    assert saga.resume(intent.command_id).expected_cancel_ids == ("og:stop", "og:tp")
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:tp"))
    assert state.state == "CLOSE_OWNED"
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:other"))
    assert state.state == "SAFETY_FAILED"


def _race(saga, *, reader, writer):
    """Run ``reader`` in a thread that stops right after its read; run ``writer``; let the reader save."""
    import threading
    read_done, go, result = threading.Event(), threading.Event(), {}
    store = saga._store
    real = store.load_by_group

    def paused(group):
        found = real(group)
        if not read_done.is_set():
            read_done.set()
            go.wait(timeout=5)
        return found
    store.load_by_group = paused
    thread = threading.Thread(target=lambda: result.setdefault("state", reader()))
    thread.start()
    assert read_done.wait(timeout=5)
    writer()
    go.set()
    thread.join(timeout=10)
    store.load_by_group = real
    return result["state"]


def test_an_ingest_event_racing_the_hand_over_never_loses_the_close_owner(tmp_path):
    """R28 / R2-3: both read PROTECTED; the hand-over commits first; the event is applied again on top."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    state = _race(saga, reader=lambda: saga.on_broker_event(_event(og, leg="stop", status="PreSubmitted",
                                                                   order_id=2, event_id="ib-working-7")),
                  writer=lambda: saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                                               cancels=_cancels(og, "og:stop"), generation=7, now=NOW))
    stored = saga.resume(intent.command_id)
    assert (stored.state, stored.close_root_id, stored.expected_cancel_ids) == ("CLOSE_OWNED", "close-1", ("og:stop",))
    assert "ib-working-7" in stored.seen_event_ids


def test_an_ingest_event_racing_the_release_keeps_the_new_protection_generation(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    _race(saga, reader=lambda: saga.on_broker_event(_owned_event(og, leg="stop", status="Cancelled", entity="og:stop")),
          writer=lambda: saga.release_after_partial(
              close_root_id="p-1", remaining_quantity=6.0, stop_group="p-1-reprotect-stop-265598-1",
              stop_status="Submitted", target_group=None, target_status=None, now=NOW))
    stored = saga.resume(intent.command_id)
    assert (stored.state, stored.protection_generation, stored.current_groups) == (
        "PROTECTED", 1, ("p-1-reprotect-stop-265598-1",))
    assert breaker.signals == []                                     # the retried event is a retired leg's


def test_an_ingest_event_racing_the_final_close_never_resurrects_the_saga(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    _race(saga, reader=lambda: saga.on_broker_event(_owned_event(og, leg="entry", status="Filled",
                                                                 entity="og:entry", filled=10.0)),
          writer=lambda: saga.close_after_full(close_root_id="close-1", now=NOW))
    assert saga.resume(intent.command_id).state == "CLOSED"


def test_a_stale_save_is_refused_with_a_revision_conflict(tmp_path):
    from trader.automation.protective_order_saga import SagaRevisionConflict
    saga, intent, state, *_ = _protected(tmp_path)
    stale = saga.resume(intent.command_id)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=(), generation=7, now=NOW)
    with pytest.raises(SagaRevisionConflict):
        saga._persist(replace(stale, revision=stale.revision + 1), NOW, from_state=stale.state)


def test_only_a_failure_seen_by_this_version_asks_the_worker_for_a_flatten(tmp_path):
    """R29: a saga that was SAFETY_FAILED before the upgrade starts no flatten on the first deploy."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    historic = replace(saga.resume(intent.command_id), state="SAFETY_FAILED", revision=state.revision + 1)
    saga._persist(historic, NOW, from_state="PROTECTED")             # as migration 37 leaves it
    assert saga.unhandled_failures(ACCOUNT) == []
    fresh, intent2, state2, *_ = _protected(tmp_path / "fresh")
    fresh.on_broker_event(_event(state2.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert fresh.unhandled_failures(ACCOUNT) == [intent2.command_id]



def test_cancelling_the_rest_of_a_partly_filled_entry_keeps_protection(tmp_path):
    """D16: the session cancels the entry's unfilled rest; the filled part stays protected, no incident."""
    saga, intent, state, breaker, liquidation, _dispatch = _started(tmp_path)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="stop", status="Submitted", order_id=2))
    saga.on_broker_event(_event(og, leg="entry", status="Submitted", filled=4.0, order_id=1))
    state = saga.on_broker_event(_event(og, leg="entry", status="Cancelled", filled=4.0, order_id=1))
    assert (state.state, state.protection_quantity) == ("PARTIALLY_FILLED", Decimal("4"))
    assert breaker.signals == [] and liquidation.starts == []


def test_an_entry_event_saved_while_submit_bracket_waits_keeps_the_submitted_ids(tmp_path):
    """Round-2 verification N1: the ingest thread saves the entry's Submitted event while
    ``submit_bracket`` waits. ``start`` must not then fail its own write with a revision conflict
    (DISPATCH_AMBIGUOUS, ids lost): it re-reads and adds the ids on top of the ingest's state.
    (``_race`` pauses a read by order group; ``start`` holds a copy from before the call instead,
    so the ingest runs inside the dispatch here.)"""
    import threading

    holder = {}

    class _IngestDuringSubmit(FakeBracketDispatch):
        def submit_bracket(self, *, plan, intent, account_id):
            submitted = super().submit_bracket(plan=plan, intent=intent, account_id=account_id)
            ingest = threading.Thread(target=lambda: holder.setdefault("event", holder["saga"].on_broker_event(
                _event(plan.order_group_id, leg="entry", status="Submitted"))))
            ingest.start()
            ingest.join(timeout=5)
            return submitted

    saga, *_ = _build_saga(tmp_path, dispatch=_IngestDuringSubmit())
    holder["saga"] = saga
    intent = make_intent()
    state = saga.start(
        intent=intent, approval=make_approval(), request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )
    assert holder["event"].state == "ENTRY_WORKING"
    stored = saga.resume(intent.command_id)
    assert (state.state, state.error_code) == ("ENTRY_WORKING", None)
    assert stored.state == "ENTRY_WORKING" and stored.submitted_order_ids == state.submitted_order_ids
    assert len(stored.submitted_order_ids) == 3


def test_saga_rows_from_before_the_upgrade_survive_migration_37(tmp_path):
    """R2-5 gap: a migration-30 journal with old payloads. Migration 37 backfills the columns; an old
    PROTECTED saga still takes its broker events and a hand-over; an old SAFETY_FAILED saga asks for
    no flatten (R29)."""
    import json

    from trader.automation.protective_order_saga import PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION

    db = DuckDBConnection.get_instance(str(tmp_path / "saga.duckdb"))
    SchemaMigrator(db).apply(PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION, "before_sp1", (
        """CREATE TABLE IF NOT EXISTS automated_order_sagas (command_id VARCHAR PRIMARY KEY,
           order_group_id VARCHAR NOT NULL, state VARCHAR NOT NULL, payload VARCHAR NOT NULL,
           updated_at TIMESTAMPTZ NOT NULL)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_automated_order_sagas_group ON automated_order_sagas(order_group_id)",
        """CREATE TABLE IF NOT EXISTS automated_order_saga_events (event_id VARCHAR PRIMARY KEY,
           command_id VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)""",
    ))
    for command_id, state in (("old-1", "PROTECTED"), ("old-2", "SAFETY_FAILED")):
        payload = {  # the payload keys of master before SP1
            "command_id": command_id, "order_group_id": f"og-{command_id}", "order_ref": f"mmr:og-{command_id}",
            "state": state, "account_id": ACCOUNT, "conid": CONID, "side": "BUY", "requested_quantity": "10",
            "filled_quantity": "10", "protection_quantity": "10", "protection_working": True,
            "protection_adjusted": False, "entry_working": False, "stop_working": True, "target_working": True,
            "stop_filled": False, "target_filled": False, "entry_cancelled": False, "stop_rejected": False,
            "target_rejected": False, "submitted_order_ids": [1, 2, 3], "seen_event_ids": [], "revision": 5,
            "error_code": None, "plan_json": None}
        db.execute("INSERT INTO automated_order_sagas VALUES (?, ?, ?, ?, ?)",
                   [command_id, f"og-{command_id}", state, json.dumps(payload), NOW], fetch="none")

    saga, *_ = _build_saga(tmp_path)
    assert db.execute("SELECT account_id, conid FROM automated_order_sagas WHERE command_id = 'old-1'",
                      fetch="one") == (ACCOUNT, CONID)
    assert saga.unhandled_failures(ACCOUNT) == []
    state = saga.on_broker_event(_event("og-old-1", leg="stop", status="Submitted", order_id=2))
    assert state.state == "PROTECTED"
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels("og-old-1", "og-old-1:stop"), generation=7, now=NOW)
    owned = saga.resume("old-1")
    assert (owned.state, owned.close_root_id, owned.expected_cancel_ids) == ("CLOSE_OWNED", "close-1",
                                                                             ("og-old-1:stop",))


def test_an_old_leg_event_after_a_release_and_a_restart_changes_nothing(tmp_path):
    """#25 item 2: after the release the original bracket's legs are retired, also for a new process."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    before = saga.resume(intent.command_id)
    restarted, *_rest = _build_saga(tmp_path, breaker=breaker)
    after = restarted.on_broker_event(_owned_event(og, leg="stop", status="Inactive", entity="og:stop-late"))
    assert (after.state, after.protection_generation, after.current_groups) == (
        "PROTECTED", before.protection_generation, before.current_groups)
    assert breaker.signals == []
    assert "og:stop-late:Inactive:0.0" in restarted.resume(intent.command_id).seen_event_ids


# --- In-flight entries count toward gross (issue #49) -------------------------
#
# Budget: 6% of $100k = $6,000. A "4%" entry is 25 shares at a limit of
# 160.09 ($4,002). Two of them would be 8%.

OTHER_CONID = 272093
ENTRY_SHARES = 25


def _group_guard(snapshot, *, meet_other_thread=None):
    """A real DispatchGuard whose broker always returns ``snapshot``.

    ``meet_other_thread`` is a Barrier: the broker read waits for the other
    handler, which reproduces both threads reading the same empty snapshot.
    """
    import threading
    from trader.promotion.allocation_policy import AllocationPolicy
    from trader.trading.command_policy import CommandAuthorityPolicy
    from trader.trading.dispatch_guard import DispatchGuard

    class Broker:
        def capture(self, account_id):
            if meet_other_thread is not None:
                try:
                    meet_other_thread.wait()
                except threading.BrokenBarrierError:
                    pass
            return snapshot

    class Quotes:
        def executable_quote(self, conid, *, side):
            return ExecutableQuote(
                conid=conid, side="ask", price=160.01, market_timestamp=NOW,
                feed_type="live", session_state="continuous", bid=159.99, ask=160.01,
            )

    class Margin:
        def what_if_margin(self, conid, side, quantity):
            return {"initMarginAfter": 1000.0, "equityWithLoanAfter": 99_000.0}

    return DispatchGuard(
        broker=Broker(), quotes=Quotes(), margin=Margin(),
        controls=SimpleNamespace(require_unpaused=lambda account_id: None),
        risk_gate=SimpleNamespace(
            check_leverage=lambda margin, net_liq: SimpleNamespace(approved=True, reason=""),
        ),
        policy=CommandAuthorityPolicy(
            enabled=True, live_enabled=False, live_account_id=None,
            max_order_notional=25_000.0, max_drift_bps=50.0,
        ),
        account_id=ACCOUNT, account_mode="paper",
        allocation_policy=AllocationPolicy(now=lambda: NOW),
        allocation_authority_lookup=lambda account, artifact: None,
    )


def _entry(conid, shares=ENTRY_SHARES, signal="s"):
    return make_intent(
        conid=conid, requested_quantity=Decimal(shares), signal_id=f"{signal}-{conid}-{shares}",
    )


def _entry_approval(intent, snapshot, *, side="BUY", direction=RiskDirection.INCREASING):
    from dataclasses import replace as dc_replace

    return dc_replace(
        make_approval(quantity=float(intent.requested_quantity), price=160.01, side=side),
        conid=intent.conid, broker=snapshot, risk_direction=direction,
    )


def _start_entry(saga, intent, snapshot, **approval_kwargs):
    return _start_saga(saga, intent, _entry_approval(intent, snapshot, **approval_kwargs))


def _sized_risk(shares=ENTRY_SHARES):
    return FakeSessionRisk(quantity=Decimal(shares), effective_gross_ceiling=0.06)


def _working_entry(order_group_id, conid, shares=ENTRY_SHARES):
    from trader.data.broker_state import BrokerOrderRow

    return BrokerOrderRow(
        order_entity_id=f"order-{order_group_id}", account_id=ACCOUNT, conid=conid,
        symbol="X", order_group_id=order_group_id, leg="entry", is_external=False,
        action="BUY", order_type="LMT", total_quantity=float(shares), filled_quantity=0.0,
        avg_fill_price=None, limit_price=160.09, stop_price=None, tif="DAY",
        status="Submitted", deleted=False, revision=1, source_timestamp=NOW,
    )


def test_two_concurrent_entries_on_different_conids_cannot_both_exceed_the_budget(tmp_path):
    import threading

    snapshot = _snapshot()
    guard = _group_guard(snapshot, meet_other_thread=threading.Barrier(2, timeout=1.0))
    saga, _, _, dispatch, *_ = _build_saga(tmp_path, guard=guard, risk=_sized_risk())
    intents = [_entry(CONID), _entry(OTHER_CONID)]
    results: dict[str, Any] = {}

    def handle(intent):
        results[intent.command_id] = _start_entry(saga, intent, snapshot)

    threads = [threading.Thread(target=handle, args=(intent,)) for intent in intents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(dispatch.calls) == 1
    states = sorted(results.values(), key=lambda state: state.state)
    assert [state.state for state in states] == ["CLOSED", "SUBMITTING"]
    assert states[0].error_code == "GROSS_EXPOSURE_IN_FLIGHT"


def test_refused_entry_releases_its_reservation(tmp_path):
    snapshot = _snapshot()
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_group_guard(snapshot), risk=_sized_risk(),
    )
    dispatch.reject_with("broker said no")

    refused = _start_entry(saga, _entry(CONID), snapshot)
    assert refused.state == "CLOSED"
    assert refused.error_code == "BROKER_REJECTED"

    dispatch._reject = None
    later = _start_entry(saga, _entry(OTHER_CONID), snapshot)
    assert later.state == "SUBMITTING"
    assert len(dispatch.calls) == 1


def test_cancelled_entry_releases_its_reservation(tmp_path):
    from trader.automation.protective_order_saga import BrokerOrderEvent

    snapshot = _snapshot()
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_group_guard(snapshot), risk=_sized_risk(),
    )
    first = _start_entry(saga, _entry(CONID), snapshot)
    assert first.state == "SUBMITTING"
    assert _start_entry(saga, _entry(OTHER_CONID), snapshot).error_code == (
        "GROSS_EXPOSURE_IN_FLIGHT"
    )

    saga.on_broker_event(BrokerOrderEvent(
        order_group_id=first.order_group_id, leg="entry", status="Cancelled",
        filled_quantity=0, total_quantity=ENTRY_SHARES, order_id=5001,
        event_id="cancel-1", source_timestamp=NOW,
    ))

    later = _start_entry(saga, _entry(OTHER_CONID, signal="retry"), snapshot)
    assert later.state == "SUBMITTING"
    assert len(dispatch.calls) == 2


def test_pending_reservation_survives_restart_and_is_reconciled_from_broker(tmp_path):
    from trader.automation.protective_order_saga import BrokerOrderEvent

    empty = _snapshot()
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_group_guard(empty), risk=_sized_risk(),
    )
    dispatch.raise_on_submit(TimeoutError("socket closed mid-send"))
    unknown = _start_entry(saga, _entry(CONID), empty)
    assert unknown.state == "OUTCOME_UNKNOWN"

    # Restart: a fresh saga on the same journal still counts the unknown entry.
    restarted, _, _, dispatch2, *_ = _build_saga(
        tmp_path, guard=_group_guard(empty), risk=_sized_risk(),
    )
    refused = _start_entry(restarted, _entry(OTHER_CONID), empty)
    assert refused.error_code == "GROSS_EXPOSURE_IN_FLIGHT"
    assert dispatch2.calls == []

    # The broker now shows the order: it is counted once, by the broker.
    visible = replace_snapshot(empty, working_orders=(
        _working_entry(unknown.order_group_id, CONID),
    ))
    seen, _, _, dispatch3, *_ = _build_saga(
        tmp_path, guard=_group_guard(visible), risk=_sized_risk(6),
    )
    small = _start_entry(seen, _entry(OTHER_CONID, shares=6), visible)
    assert small.state == "SUBMITTING"  # 4% + 1% fits; double counting would refuse it

    # The broker cancels the unknown entry: its reservation is released.
    seen.on_broker_event(BrokerOrderEvent(
        order_group_id=unknown.order_group_id, leg="entry", status="Cancelled",
        filled_quantity=0, total_quantity=ENTRY_SHARES, order_id=5001,
        event_id="cancel-unknown", source_timestamp=NOW,
    ))
    seen.on_broker_event(BrokerOrderEvent(
        order_group_id=small.order_group_id, leg="entry", status="Cancelled",
        filled_quantity=0, total_quantity=6, order_id=5002,
        event_id="cancel-small", source_timestamp=NOW,
    ))
    final, _, _, dispatch4, *_ = _build_saga(
        tmp_path, guard=_group_guard(empty), risk=_sized_risk(),
    )
    assert _start_entry(final, _entry(OTHER_CONID, signal="after"), empty).state == "SUBMITTING"
    assert len(dispatch4.calls) == 1


def replace_snapshot(snapshot, **changes):
    from dataclasses import replace as dc_replace

    return dc_replace(snapshot, **changes)


def test_reduction_is_never_blocked_by_reservations(tmp_path):
    from trader.data.broker_state import BrokerPositionRow

    empty = _snapshot()
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_group_guard(empty), risk=_sized_risk(37),
    )
    # 37 shares at 160.09 = 5.9%: the budget is fully reserved.
    assert _start_entry(saga, _entry(CONID, shares=37), empty).state == "SUBMITTING"

    held = replace_snapshot(empty, positions=(BrokerPositionRow(
        account_id=ACCOUNT, conid=OTHER_CONID, symbol="X", sec_type="STK",
        exchange="SMART", currency="USD", quantity=10.0, average_cost=150.0,
        market_price=160.0, market_value=1600.0, unrealized_pnl=0.0,
        realized_pnl=0.0, daily_pnl=0.0, deleted=False, revision=1,
        source_timestamp=NOW,
    ),))
    reducer, _, _, dispatch2, *_ = _build_saga(
        tmp_path, guard=_group_guard(held), risk=_sized_risk(10),
    )

    def unreadable(account_id, exclude_command_id):
        raise RuntimeError("journal locked")

    reducer._store.in_flight_entries = unreadable  # a reduction must not need it
    sell = make_intent(
        conid=OTHER_CONID, side="SELL", requested_quantity=Decimal(10), signal_id="exit",
        stop_policy=StopPolicy(stop_price=Decimal("170"), order_type="STP"),
        target_policy=None,
    )

    state = _start_entry(
        reducer, sell, held, side="SELL", direction=RiskDirection.REDUCING,
    )

    assert state.state == "SUBMITTING"
    assert len(dispatch2.calls) == 1


def test_unreadable_reservations_refuse_the_entry(tmp_path):
    snapshot = _snapshot()
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_group_guard(snapshot), risk=_sized_risk(),
    )

    def unreadable(account_id, exclude_command_id):
        raise RuntimeError("journal locked")

    saga._store.in_flight_entries = unreadable

    state = _start_entry(saga, _entry(CONID), snapshot)

    assert state.state == "CLOSED"
    assert state.error_code == "IN_FLIGHT_STATE_UNAVAILABLE"
    assert dispatch.calls == []


# --- Reservations are released only by what the broker snapshot proves -------
#
# The entry limit is 160.09, so 25 shares reserve $4,002.25 and 5 filled
# shares reserve $800.45 against the $6,000 budget.

def _working_stop(order_group_id, conid, shares=ENTRY_SHARES):
    from dataclasses import replace as dc_replace

    return dc_replace(
        _working_entry(order_group_id, conid, shares),
        order_entity_id=f"stop-{order_group_id}", leg="stop", action="SELL",
        order_type="STP", limit_price=None, stop_price=150.0,
    )


def _held(conid, shares, *, stamped):
    from trader.data.broker_state import BrokerPositionRow

    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol="X", sec_type="STK",
        exchange="SMART", currency="USD", quantity=float(shares), average_cost=160.0,
        market_price=160.0, market_value=160.0 * shares, unrealized_pnl=0.0,
        realized_pnl=0.0, daily_pnl=0.0, deleted=False, revision=1,
        source_timestamp=stamped,
    )


def _entry_fill(order_group_id, *, status, filled, event_id):
    from trader.automation.protective_order_saga import BrokerOrderEvent

    return BrokerOrderEvent(
        order_group_id=order_group_id, leg="entry", status=status,
        filled_quantity=filled, total_quantity=ENTRY_SHARES, order_id=5001,
        event_id=event_id, source_timestamp=NOW,
    )


def _try_entry(tmp_path, snapshot, shares, signal):
    saga, _, _, dispatch, *_ = _build_saga(
        tmp_path, guard=_group_guard(snapshot), risk=_sized_risk(shares),
    )
    return _start_entry(saga, _entry(OTHER_CONID, shares=shares, signal=signal), snapshot)


def test_working_stop_of_the_group_does_not_hide_an_unseen_entry(tmp_path):
    empty = _snapshot()
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(empty), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), empty)
    assert first.state == "SUBMITTING"

    # Restart: the broker shows only the SELL stop, not the BUY parent.
    stop_only = replace_snapshot(empty, working_orders=(
        _working_stop(first.order_group_id, CONID),
    ))
    second = _try_entry(tmp_path, stop_only, ENTRY_SHARES, "after-restart")

    assert second.state == "CLOSED"
    assert second.error_code == "GROSS_EXPOSURE_IN_FLIGHT"


def test_partial_fill_counts_until_the_position_shows_it(tmp_path):
    from dataclasses import replace as dc_replace

    empty = _snapshot()
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(empty), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), empty)
    partial = saga.on_broker_event(
        _entry_fill(first.order_group_id, status="Submitted", filled=5, event_id="p5"),
    )
    assert partial.state == "PARTIALLY_FILLED"

    # The broker counts the 20 unfilled shares; the 5 filled are not in positions yet.
    lagging = replace_snapshot(empty, working_orders=(dc_replace(
        _working_entry(first.order_group_id, CONID), filled_quantity=5.0,
    ),))
    refused = _try_entry(tmp_path, lagging, 14, "lag")
    assert refused.error_code == "GROSS_EXPOSURE_IN_FLIGHT"  # 3,201.80 + 800.45 + 2,241.26

    # The position now shows the 5 shares: they are counted once, by the broker.
    shown = replace_snapshot(
        lagging, positions=(_held(CONID, 5, stamped=NOW + dt.timedelta(seconds=1)),),
    )
    assert _try_entry(tmp_path, shown, 12, "shown").state == "SUBMITTING"


def test_cancel_after_partial_fill_keeps_the_filled_shares_reserved(tmp_path):
    empty = _snapshot()
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(empty), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), empty)
    saga.on_broker_event(
        _entry_fill(first.order_group_id, status="Submitted", filled=5, event_id="p5"),
    )
    cancelled = saga.on_broker_event(
        _entry_fill(first.order_group_id, status="Cancelled", filled=5, event_id="c5"),
    )
    assert cancelled.entry_cancelled is True

    # 36 shares ($5,763.24) fit alone but not with the 5 filled shares.
    assert _try_entry(tmp_path, empty, 36, "big").error_code == "GROSS_EXPOSURE_IN_FLIGHT"
    # The 20 cancelled shares are released.
    assert _try_entry(tmp_path, empty, 25, "fits").state == "SUBMITTING"


def test_full_fill_counts_until_a_newer_position_or_generation_shows_it(tmp_path):
    empty = _snapshot()
    saga, *_, breaker, liquidation, _, _, _ = _build_saga(
        tmp_path, guard=_group_guard(empty), risk=_sized_risk(),
    )
    first = _start_entry(saga, _entry(CONID), empty)
    filled = saga.on_broker_event(
        _entry_fill(first.order_group_id, status="Filled", filled=25, event_id="f25"),
    )
    assert filled.state == "SAFETY_FAILED"
    assert filled.filled_at == NOW

    assert _try_entry(tmp_path, empty, 25, "lag").error_code == "GROSS_EXPOSURE_IN_FLIGHT"

    # The position quantity shows the 25 shares: they are counted once.
    fresh = replace_snapshot(
        empty, positions=(_held(CONID, 25, stamped=NOW + dt.timedelta(seconds=1)),),
    )
    assert _try_entry(tmp_path, fresh, 6, "fresh").state == "SUBMITTING"

    # A complete enumeration that began after the fill shows no position:
    # the shares are gone, so nothing stays reserved.
    enumerated = replace_snapshot(
        empty, generation_id=2, source_cursor=2,
        generation_started_at=NOW + dt.timedelta(seconds=1),
    )
    assert _try_entry(tmp_path, enumerated, 25, "enumerated").state == "SUBMITTING"


# --- Round 3: fills on terminal paths, quantity proof, SAFETY_FAILED entry ---

@pytest.mark.parametrize("events", [
    pytest.param((("Submitted", 5), ("Inactive", 5)), id="inactive-after-partial"),
    pytest.param((("Rejected", 5),), id="rejected-with-fill"),
])
def test_rejected_entry_keeps_its_filled_shares_reserved(tmp_path, events):
    empty = _snapshot()
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(empty), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), empty)
    for index, (status, filled) in enumerate(events):
        state = saga.on_broker_event(_entry_fill(
            first.order_group_id, status=status, filled=filled, event_id=f"e{index}",
        ))
    assert (state.state, state.filled_quantity) == ("CLOSED", Decimal(5))

    # 36 shares fit alone, not with the 5 filled shares the position does not show.
    assert _try_entry(tmp_path, empty, 36, "big").error_code == "GROSS_EXPOSURE_IN_FLIGHT"
    assert _try_entry(tmp_path, empty, 25, "fits").state == "SUBMITTING"


def test_pnl_only_position_update_does_not_prove_the_fill(tmp_path):
    from dataclasses import replace as dc_replace

    held_one = replace_snapshot(_snapshot(), positions=(_held(CONID, 1, stamped=NOW),))
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(held_one), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), held_one)
    saga.on_broker_event(
        _entry_fill(first.order_group_id, status="Submitted", filled=5, event_id="p5"),
    )
    working = (dc_replace(_working_entry(first.order_group_id, CONID), filled_quantity=5.0),)

    # A PnL update refreshed the row after the fill; its quantity is still 1.
    refreshed = replace_snapshot(
        held_one, working_orders=working,
        positions=(_held(CONID, 1, stamped=NOW + dt.timedelta(seconds=1)),),
    )
    # 160 + 3,201.80 + 800.45 + 2,241.26 = 6,403.51
    assert _try_entry(tmp_path, refreshed, 14, "pnl").error_code == "GROSS_EXPOSURE_IN_FLIGHT"

    grown = replace_snapshot(
        refreshed, positions=(_held(CONID, 6, stamped=NOW + dt.timedelta(seconds=2)),),
    )
    # 960 + 3,201.80 + 1,600.90 fits; counting the 5 shares twice would not.
    assert _try_entry(tmp_path, grown, 10, "grown").state == "SUBMITTING"


def test_safety_failed_entry_keeps_its_unfilled_part_until_cancelled(tmp_path):
    empty = _snapshot()
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(empty), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), empty)
    group = first.order_group_id
    saga.on_broker_event(_entry_fill(group, status="Submitted", filled=5, event_id="p5"))
    failed = saga.on_broker_event(_event(group, leg="stop", status="Rejected", order_id=2))
    assert failed.state == "SAFETY_FAILED"

    # 20 unfilled + 5 filled are reserved: a second 25-share entry does not fit.
    assert _try_entry(tmp_path, empty, 25, "a").error_code == "GROSS_EXPOSURE_IN_FLIGHT"

    later = saga.on_broker_event(
        _entry_fill(group, status="Submitted", filled=12, event_id="p12"),
    )
    assert (later.state, later.filled_quantity) == ("SAFETY_FAILED", Decimal(12))
    cancelled = saga.on_broker_event(
        _entry_fill(group, status="Cancelled", filled=12, event_id="c12"),
    )
    assert cancelled.entry_cancelled is True

    # Only the 12 filled shares stay reserved: 4,002.25 + 1,921.08 fits.
    assert _try_entry(tmp_path, empty, 25, "b").state == "SUBMITTING"


# --- With the merged safe close (#46): a close owns the saga ---

def test_a_close_owned_entry_stays_reserved_and_records_its_fills_and_cancel(tmp_path):
    """While a close owns the saga the entry order can still fill until its
    cancel lands: the unfilled part stays reserved and fills are recorded."""
    from dataclasses import replace as dc_replace

    empty = _snapshot()
    saga, *_ = _build_saga(tmp_path, guard=_group_guard(empty), risk=_sized_risk())
    first = _start_entry(saga, _entry(CONID), empty)
    group = first.order_group_id
    saga.on_broker_event(_entry_fill(group, status="Submitted", filled=5, event_id="p5"))
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(group, f"{group}:entry"), generation=7, now=NOW)
    assert saga.resume(first.command_id).state == "CLOSE_OWNED"

    # 20 unfilled + 5 filled are reserved: a second 25-share entry does not fit.
    assert _try_entry(tmp_path, empty, 25, "a").error_code == "GROSS_EXPOSURE_IN_FLIGHT"

    grown = saga.on_broker_event(dc_replace(
        _entry_fill(group, status="Submitted", filled=12, event_id="p12"), order_entity_id=f"{group}:entry"))
    assert (grown.state, grown.filled_quantity) == ("CLOSE_OWNED", Decimal(12))
    cancelled = saga.on_broker_event(dc_replace(
        _entry_fill(group, status="Cancelled", filled=12, event_id="c12"), order_entity_id=f"{group}:entry"))
    assert (cancelled.state, cancelled.entry_cancelled) == ("CLOSE_OWNED", True)

    # Only the 12 filled shares stay reserved: 4,002.25 + 1,921.08 fits.
    assert _try_entry(tmp_path, empty, 25, "b").state == "SUBMITTING"
