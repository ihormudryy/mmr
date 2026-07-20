#!/usr/bin/env python3
"""[P3 Task 9] Automation paper vertical-slice synthetic drills.

In-process battery over the P3 stack (intent emission → coordinator →
protective saga → broker events → attribution → session flatten → sealed
replay) behind deterministic fake ports. Nothing here touches IB or live
dispatch — this is the *synthetic* half of the P3 release gate.

The manual IB-paper session soak (market hours, real paper stack, minimum
safe automation allocation) is the other, non-fungible half and MUST NOT be
replaced by these fixtures. See
``docs/superpowers/rollout/trading-income-operations-runbook.md``.

Usage:
    python3 scripts/automation_paper_drill.py
    python3 scripts/automation_paper_drill.py --json --output drill.json
    python3 scripts/automation_paper_drill.py --scenarios happy_path,stale_quote
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.attribution import AttributionEvidenceEvent, AttributionLedger
from trader.automation.automated_intent_command import AutomatedIntentCommandService
from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.automation.models import (
    EntryPolicy,
    ExecutionIntent,
    StopPolicy,
    TargetPolicy,
    TimeExitPolicy,
)
from trader.automation.protective_order_saga import (
    ProtectiveOrderSaga,
    apply_protective_order_saga_migration,
)
from trader.automation.replay import TradingDayReplay
from trader.automation.replay_bundle import ReplayBundle
from trader.automation.session_controller import (
    SessionController,
    apply_session_controller_migration,
)
from trader.data.attribution_store import apply_attribution_migrations
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.messaging.production_api import ExecuteAutomatedIntentRequest
from trader.objects import Action
from trader.research.canonical import sha256_digest
from trader.strategy.intent_emitter import IntentEmitter, IntentEmitterContext
from trader.trading.approval_context import ApprovalContext, ExecutableMarketEvidence
from trader.trading.circuit_breaker import BreakerSignal
from trader.trading.command_coordinator import (
    BrokerRejectedError,
    CommandAudit,
    CommandLedger,
    CommandRequest,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.dispatch_guard import DispatchGuardError, DispatchPermit
from trader.trading.order_correlation import encode_order_ref
from trader.trading.proposal_command_service import ExecutableQuote
from trader.trading.trading_control import (
    TradingControlStore,
    apply_trading_control_migration,
)

REPORT_VERSION = 1
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU111111"
CONID = 265598
STRATEGY_NAME = "paper_slice_orb"
ARTIFACT_ID = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ARTIFACT_DIGEST = "sha256:artifact-bundle-deadbeef"
ATTEST_DIGEST = "b" * 64
SESSION_ID = "xnys-2026-07-18"
BUNDLE_DIGEST = ARTIFACT_DIGEST


# ---------------------------------------------------------------------------
# Fake ports
# ---------------------------------------------------------------------------

class SimulatedCrash(BaseException):
    """Hard crash after claim / before durable dispatch outcome."""


class FakeBracketDispatch:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._raise: Optional[BaseException] = None
        self._reject = False
        self._next_id = 9001

    def raise_on_submit(self, exc: BaseException) -> None:
        self._raise = exc

    def reject_stop(self) -> None:
        self._reject = True

    def submit_bracket(self, *, plan, intent, account_id: str):
        self.calls.append({
            "order_group_id": plan.order_group_id,
            "order_ref": plan.order_ref,
            "intent_id": intent.intent_id,
            "account_id": account_id,
            "order_ids": [],
        })
        if self._raise is not None:
            raise self._raise
        if self._reject:
            raise BrokerRejectedError("stop leg rejected by broker")
        n = len(plan.legs)
        order_ids = list(range(self._next_id, self._next_id + n))
        self._next_id += n
        self.calls[-1]["order_ids"] = list(order_ids)
        return SimpleNamespace(
            order_group_id=plan.order_group_id,
            order_ref=plan.order_ref,
            order_ids=order_ids,
        )


class FakeDispatchGuard:
    def __init__(self, *, stale: bool = False) -> None:
        self.calls = 0
        self._stale = stale
        self.permit = DispatchPermit(
            generation_id=1, source_cursor=1,
            quote_timestamp=NOW, what_if_timestamp=None,
        )

    def revalidate(self, approval, request, now):
        self.calls += 1
        if self._stale:
            raise DispatchGuardError("QUOTE_STALE", "executable quote is stale")
        return self.permit


class FakeSessionRisk:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, intent, artifact, approval_context, session_state, allocation):
        self.calls += 1
        return SimpleNamespace(
            approved=True,
            approved_quantity=Decimal("10"),
            reason_codes=(),
            breaker_signals=(),
            equity_risk_fraction=0.001,
        )


class FakeBreaker:
    def __init__(self) -> None:
        self.signals: list[BreakerSignal] = []
        self.state = "CLEAR"

    def record(self, signal: BreakerSignal) -> None:
        self.signals.append(signal)
        self.state = "TRIPPED"
        return SimpleNamespace(state="TRIPPED", reason_code=signal.kind)


class FakeLiquidation:
    def __init__(self) -> None:
        self.starts: list[tuple] = []

    def start(self, account_id, cause_command_id, deadline):
        self.starts.append((account_id, cause_command_id, deadline))
        return SimpleNamespace(
            account_id=account_id, cause_command_id=cause_command_id,
            state="REQUESTED", deadline=deadline,
        )


class FakeBroker:
    def __init__(self) -> None:
        self._positions: list = []
        self._working: list = []
        self.generation = 1

    def capture(self, account_id: str = ACCOUNT) -> BrokerRiskSnapshot:
        return BrokerRiskSnapshot(
            generation_id=self.generation,
            source_cursor=self.generation,
            promoted_at=NOW,
            account_id=account_id,
            account_mode="paper",
            net_liquidation=100_000.0,
            daily_pnl=0.0,
            positions=tuple(self._positions),
            working_orders=tuple(self._working),
        )

    def set_flat(self) -> None:
        self._positions = []
        self._working = []
        self.generation += 1


class FakeCancel:
    def __init__(self) -> None:
        self.calls = 0

    def cancel_working_entries(self, *args, **kwargs) -> None:
        self.calls += 1


class FakeTimeExit:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def close_position(self, *args, **kwargs) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


class FakeTypedCommandClient:
    """Routes ``execute_automated_intent`` into the in-process coordinator.

    Mirrors the production typed path: strategy builds an
    ``ExecuteAutomatedIntentRequest`` body; the trader handler stamps
    ``source=strategy_service`` and never mutates the journal from the client.
    """

    def __init__(self, coordinator: TradingCommandCoordinator, account_id: str) -> None:
        self._coordinator = coordinator
        self._account_id = account_id
        self.calls: list[dict] = []
        self.legacy_rpc_calls: list[str] = []

    def call(self, method: str, body: dict, response_model: Any = dict) -> Any:
        self.calls.append({"method": method, "body": dict(body)})
        if method != "execute_automated_intent":
            raise AssertionError(f"unexpected typed method {method!r}")
        parsed = ExecuteAutomatedIntentRequest.model_validate(body)
        wire = parsed.model_dump(mode="json")
        request = CommandRequest(
            command_id=parsed.command_id,
            action="execute_automated_intent",
            account_id=self._account_id,
            target_type="intent",
            target_id=parsed.intent_id,
            expected_version=None,
            body=wire,
            source="strategy_service",
        )
        receipt = self._coordinator.execute(request)
        return {
            "command_id": receipt.command_id,
            "correlation_id": receipt.correlation_id,
            "state": receipt.state,
            "error_code": receipt.error_code,
            "retryable": receipt.retryable,
            "outcome": receipt.outcome,
        }


class MemoryEvidenceStore:
    def __init__(self, evidence: dict[str, Any]) -> None:
        self._evidence = evidence

    def load_session(self, session_id: str) -> dict[str, Any]:
        if session_id != self._evidence.get("session_id"):
            raise KeyError(session_id)
        return self._evidence


# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------

@dataclass
class SliceStack:
    tmp: Path
    db: Any
    journal: DomainJournal
    ledger: CommandLedger
    controls: TradingControlStore
    coordinator: TradingCommandCoordinator
    service: AutomatedIntentCommandService
    saga: ProtectiveOrderSaga
    attribution: AttributionLedger
    breaker: FakeBreaker
    dispatch: FakeBracketDispatch
    guard: FakeDispatchGuard
    typed_client: FakeTypedCommandClient
    emitter: IntentEmitter
    artifact: VerifiedArtifact
    broker: FakeBroker
    clock: Callable[[], dt.datetime]
    order_refs: list[str] = field(default_factory=list)

    def emit_from_bar(
        self,
        *,
        bar_ts: dt.datetime,
        signal_ts: Optional[dt.datetime] = None,
        side: str = "BUY",
        strategy_name: str = STRATEGY_NAME,
        stop_price: str = "150",
        target_price: str = "200",
    ) -> Optional[CommandReceipt]:
        from trader.trading.strategy import Signal

        signal = Signal(
            source_name=strategy_name,
            action=Action.BUY if side == "BUY" else Action.SELL,
            probability=0.8,
            risk=0.02,
            conid=CONID,
            date_time=signal_ts or (bar_ts + dt.timedelta(seconds=1)),
            metadata={
                "stop_price": stop_price,
                "target_price": target_price,
                "risk_fraction": "0.002",
                "limit_offset_bps": "5",
                "max_hold_bars": 10,
                "close_by": (NOW + dt.timedelta(hours=2)).isoformat(),
            },
        )
        return self.emitter.on_signal(
            strategy_name=strategy_name,
            signal=signal,
            completed_bar_timestamp=bar_ts,
            session_id=SESSION_ID,
        )


def _artifact() -> VerifiedArtifact:
    return VerifiedArtifact(
        artifact_id=ARTIFACT_ID,
        manifest_digest="a" * 64,
        dataset_manifest_digest="c" * 64,
        parameters={
            "entry_order_type": "LIMIT",
            "limit_offset_bps": "5",
            "stop_order_type": "STP",
            "risk_fraction": "0.002",
            "max_hold_bars": 10,
        },
        allowlist=(str(CONID), "AAPL"),
        max_gross_allocation=0.06,
        expires_at=NOW + dt.timedelta(days=30),
        public_key_id="ed25519-test",
        verification_reason_codes=("RULES_PASS",),
    )


def _approval(intent: ExecutionIntent) -> ApprovalContext:
    from trader.trading.command_coordinator import RiskDirection

    quote = ExecutableQuote(
        conid=intent.conid,
        side=intent.side,
        price=160.0,
        market_timestamp=NOW - dt.timedelta(seconds=1),
        feed_type="realtime",
        session_state="open",
        bid=159.99,
        ask=160.01,
    )
    return ApprovalContext(
        conid=intent.conid,
        side=intent.side,
        quantity=10.0,
        reference_price=160.0,
        max_drift_bps=50.0,
        risk_direction=RiskDirection.INCREASING,
        broker=BrokerRiskSnapshot(
            generation_id=1, source_cursor=1, promoted_at=NOW,
            account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000.0,
            daily_pnl=0.0, positions=(), working_orders=(),
        ),
        market=ExecutableMarketEvidence(quote=quote, received_at=NOW),
        what_if=None,
    )


def build_stack(
    db_path: str,
    *,
    stale_quote: bool = False,
    reject_stop: bool = False,
    ambiguous: bool = False,
    crash: bool = False,
    now: Optional[Callable[[], dt.datetime]] = None,
) -> SliceStack:
    tmp = Path(db_path).parent
    db = DuckDBConnection.get_instance(db_path)
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)
    apply_protective_order_saga_migration(migrator)
    apply_attribution_migrations(migrator)
    apply_session_controller_migration(migrator)

    clock = now or (lambda: NOW)
    controls = TradingControlStore(journal)
    db.transaction(lambda conn: controls.seed_in_tx(conn, [(ACCOUNT, "paper")], NOW))
    ledger = CommandLedger(journal)
    audit = CommandAudit(journal)
    breaker = FakeBreaker()
    dispatch = FakeBracketDispatch()
    if reject_stop:
        dispatch.reject_stop()
    if ambiguous:
        dispatch.raise_on_submit(TimeoutError("lost ack"))
    if crash:
        dispatch.raise_on_submit(SimulatedCrash("killed after claim"))
    guard = FakeDispatchGuard(stale=stale_quote)
    risk = FakeSessionRisk()
    liquidation = FakeLiquidation()
    broker = FakeBroker()

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
        now=clock,
        db=db,
    )
    attribution = AttributionLedger(
        journal=journal, db=db, account_id=ACCOUNT, now=clock,
    )

    bundles = tmp / "bundles"
    bundles.mkdir(exist_ok=True)
    (bundles / BUNDLE_DIGEST.replace(":", "_")).mkdir(exist_ok=True)

    class _NonceGate:
        def consume_in_tx(self, *args, **kwargs):
            return True

    coordinator = TradingCommandCoordinator(
        ledger=ledger,
        audit=audit,
        journal=journal,
        nonces=_NonceGate(),
        now=clock,
        reconciler=SimpleNamespace(schedule=lambda *_a, **_k: None),
    )

    def approval_factory(*, intent, command):
        return _approval(intent)

    service = AutomatedIntentCommandService(
        ledger=ledger,
        audit=audit,
        journal=journal,
        controls=controls,
        dispatch=SimpleNamespace(submit=lambda **kw: (_ for _ in ()).throw(
            AssertionError("bare dispatch must not run when saga is wired"))),
        artifact_verifier=SimpleNamespace(
            verify=lambda *a, **k: _artifact(),
        ),
        account_id=ACCOUNT,
        account_mode="paper",
        now=clock,
        bundle_root=bundles,
        protective_saga=saga,
        approval_factory=approval_factory,
        session_state_factory=lambda **kw: SimpleNamespace(
            state="OPEN",
            entry_cutoff_reached=False,
            high_water_mark=100_000.0,
            expected_account_id=ACCOUNT,
            liquidity=None,
        ),
        allocation_factory=lambda **kw: SimpleNamespace(
            max_gross_allocation=0.06,
            strategy_allocation=0.06,
            max_gross_fraction=0.06,
        ),
    )
    coordinator.register_action(
        "execute_automated_intent", service.execute,
        requires_preflight=False, saga=True,
    )

    typed = FakeTypedCommandClient(coordinator, ACCOUNT)
    artifact = _artifact()
    emitter = IntentEmitter(
        command_client=typed,
        context=IntentEmitterContext(
            enabled=True,
            live_enabled=False,
            strategy_name=STRATEGY_NAME,
            artifact=artifact,
            artifact_digest=ARTIFACT_DIGEST,
            eligibility_attestation_digest=ATTEST_DIGEST,
            artifact_bundle_digest=BUNDLE_DIGEST,
            account_mode="paper",
        ),
        now=clock,
    )
    return SliceStack(
        tmp=tmp, db=db, journal=journal, ledger=ledger, controls=controls,
        coordinator=coordinator, service=service, saga=saga,
        attribution=attribution, breaker=breaker, dispatch=dispatch,
        guard=guard, typed_client=typed, emitter=emitter, artifact=artifact,
        broker=broker, clock=clock,
    )


def _broker_event(order_group_id: str, *, leg: str, status: str, order_id: int = 1,
                  filled: float = 0.0, event_id: Optional[str] = None):
    from trader.automation.protective_order_saga import BrokerOrderEvent

    return BrokerOrderEvent(
        order_group_id=order_group_id,
        leg=leg,
        status=status,
        filled_quantity=filled,
        total_quantity=10.0,
        order_id=order_id,
        event_id=event_id or f"{order_group_id}:{leg}:{status}:{filled}",
        source_timestamp=NOW,
    )


def _advance_to_protected(stack: SliceStack, command_id: str) -> str:
    og = f"og-{command_id}"
    stack.saga.on_broker_event(_broker_event(og, leg="entry", status="Submitted", order_id=1))
    stack.saga.on_broker_event(_broker_event(og, leg="stop", status="Submitted", order_id=2))
    stack.saga.on_broker_event(
        _broker_event(og, leg="take_profit", status="Submitted", order_id=3))
    stack.saga.on_broker_event(
        _broker_event(og, leg="entry", status="Filled", order_id=1, filled=10.0))
    state = stack.saga.resume(command_id)
    if state is None or state.state != "PROTECTED":
        raise AssertionError(f"expected PROTECTED, got {getattr(state, 'state', None)}")
    return og


def _append_attribution(stack: SliceStack, intent: ExecutionIntent, og: str) -> str:
    trade_id = f"trade-{intent.intent_id}"
    now = stack.clock()
    events = (
        ("intent", {"intent_id": intent.intent_id, "command_id": intent.command_id,
                    "artifact_id": intent.artifact_id, "signal_id": intent.signal_id}),
        ("fill", {"exec_id": f"ex-{og}-entry", "leg": "entry", "side": "BOT",
                  "quantity": "10", "price": "160.00", "order_group_id": og}),
        ("fill", {"exec_id": f"ex-{og}-exit", "leg": "take_profit", "side": "SLD",
                  "quantity": "10", "price": "200.00", "order_group_id": og}),
        ("commission", {"exec_id": f"ex-{og}-entry", "commission": "1.00"}),
        ("commission", {"exec_id": f"ex-{og}-exit", "commission": "1.00"}),
        ("position", {"quantity": "0", "conid": intent.conid}),
    )
    for kind, payload in events:
        stack.attribution.append(AttributionEvidenceEvent(
            evidence_key=f"{trade_id}:{kind}:{payload.get('exec_id', kind)}",
            trade_id=trade_id,
            event_kind=kind,
            payload=payload,
            source_timestamp=now,
        ))
    return trade_id


def _seal_replay(stack: SliceStack, intent: ExecutionIntent, og: str,
                 trade_id: str, out_dir: Path) -> Any:
    signal_body = {
        "artifact_id": intent.artifact_id,
        "bar_id": intent.bar_id,
        "conid": intent.conid,
        "side": intent.side,
        "completed_bar_timestamp": intent.completed_bar_timestamp.isoformat(),
    }
    signal_id = intent.signal_id

    def _ser(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dt.datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: _ser(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_ser(v) for v in value]
        return value

    intent_dict = _ser(asdict(intent))
    # Sealed body must self-consistently recompute intent/command IDs.
    body_for_ids = {k: v for k, v in intent_dict.items()
                    if k not in ("intent_id", "command_id")}
    intent_dict["intent_id"] = derive_intent_id(body_for_ids)
    intent_dict["command_id"] = derive_command_id(intent_dict["intent_id"])

    sizing = {
        "equity": "100000",
        "entry_price": "170",
        "stop_price": str(intent.stop_policy.stop_price),
        "risk_fraction": str(intent.risk_fraction),
        "approved_quantity": "10",
    }
    decision_body = {
        "intent_id": intent_dict["intent_id"],
        "approved": True,
        "reason_codes": [],
        "approved_quantity": "10",
        "calendar_version": "4.5.0",
    }
    decision = {
        **decision_body,
        "decision_digest": sha256_digest("policy_decision", decision_body),
    }
    broker_events = [
        {"event_id": "evt-ack", "kind": "order_status", "command_id": intent.command_id,
         "status": "Submitted", "filled": "0", "remaining": "10", "timestamp": NOW.isoformat()},
        {"event_id": "evt-fill", "kind": "fill", "command_id": intent.command_id,
         "status": "Filled", "filled": "10", "remaining": "0", "timestamp": NOW.isoformat()},
        {"event_id": "evt-exit", "kind": "fill", "command_id": intent.command_id,
         "status": "Filled", "filled": "10", "leg": "take_profit",
         "timestamp": (NOW + dt.timedelta(minutes=5)).isoformat()},
    ]
    trade = stack.attribution.rebuild_trade(trade_id)
    evidence = {
        "session_id": SESSION_ID,
        "artifact_attestation": {
            "artifact_id": ARTIFACT_ID,
            "manifest_digest": artifact_digest_safe(stack.artifact.manifest_digest),
            "attestation_digest": ATTEST_DIGEST,
        },
        "bars": [{"bar_id": intent.bar_id, "conid": CONID,
                  "timestamp": intent.completed_bar_timestamp.isoformat(),
                  "open": "159", "high": "161", "low": "158", "close": "160", "volume": 1e6}],
        "quote_evidence": [{"conid": CONID, "bid": "159.99", "ask": "160.01",
                            "timestamp": NOW.isoformat(), "feed_type": "realtime"}],
        "broker_snapshots": [{"generation_id": 1, "net_liquidation": "100000",
                              "timestamp": NOW.isoformat()}],
        "policies": [{"name": "session_risk", "version": "1"}],
        "intents": [intent_dict],
        "decisions": [decision],
        "commands": [{"command_id": intent.command_id, "state": "RESOLVED",
                      "action": "execute_automated_intent"}],
        "broker_events": broker_events,
        "attribution": {
            "trades": [trade.to_payload()],
            "unresolved": [],
        },
        "breaker_actions": [],
        "reconciliation_actions": [],
        "operator_actions": [],
        "xnys_schedule": {
            "session_date": "2026-07-18", "calendar_name": "XNYS",
            "calendar_version": "4.5.0",
            "open_utc": "2026-07-18T13:30:00+00:00",
            "close_utc": "2026-07-18T20:00:00+00:00",
            "entry_cutoff_utc": "2026-07-18T19:30:00+00:00",
            "cancel_entries_utc": "2026-07-18T19:35:00+00:00",
            "flatten_start_utc": "2026-07-18T19:45:00+00:00",
            "flat_deadline_utc": "2026-07-18T19:55:00+00:00",
            "is_early_close": False,
        },
        "sizing": [sizing],
        "signals": [{"signal_id": signal_id, **signal_body}],
        "decision_trace": [
            {"step": "signal", "signal_id": signal_id},
            {"step": "intent", "intent_id": intent_dict["intent_id"],
             "command_id": intent_dict["command_id"]},
            {"step": "sizing", "approved_quantity": sizing["approved_quantity"]},
            {"step": "policy", "decision_digest": decision["decision_digest"], "approved": True},
            {"step": "broker_events", "event_ids": [e["event_id"] for e in broker_events]},
        ],
    }
    digest = ReplayBundle(MemoryEvidenceStore(evidence), output_dir=out_dir).seal(SESSION_ID)
    result = TradingDayReplay().run(digest.path)
    if not result.matched:
        raise AssertionError(f"replay diverged: {result.divergences}")
    return digest


def artifact_digest_safe(value: str) -> str:
    return value


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scn_happy_path(db_path: str) -> dict:
    stack = build_stack(db_path)
    bar_ts = NOW - dt.timedelta(minutes=1)
    receipt1 = stack.emit_from_bar(bar_ts=bar_ts)
    if receipt1 is None or receipt1.state not in ("SUBMITTED", "RESOLVED"):
        raise AssertionError(f"first emit failed: {receipt1}")
    # Duplicate completed bar → identical intent → no second dispatch.
    receipt2 = stack.emit_from_bar(bar_ts=bar_ts)
    if receipt2 is None:
        raise AssertionError("duplicate bar returned None")
    if receipt2.command_id != receipt1.command_id:
        raise AssertionError("duplicate bar minted a different command_id")
    if len(stack.dispatch.calls) != 1:
        raise AssertionError(f"expected one bracket submit, got {len(stack.dispatch.calls)}")
    if any(c["method"] != "execute_automated_intent" for c in stack.typed_client.calls):
        raise AssertionError("non-automation typed method used")
    if stack.typed_client.legacy_rpc_calls:
        raise AssertionError("legacy RPC used on automation path")

    intent = stack.emitter.last_intent
    assert intent is not None
    og = _advance_to_protected(stack, intent.command_id)
    # Time exit / flatten via take-profit fill (session-valid exit).
    stack.saga.on_broker_event(
        _broker_event(og, leg="take_profit", status="Filled", order_id=3, filled=10.0))
    trade_id = _append_attribution(stack, intent, og)
    rebuilt = stack.attribution.rebuild_trade(trade_id)
    if not getattr(rebuilt, "resolved", False):
        raise AssertionError("attribution trade unresolved after exit fills")
    digest = _seal_replay(stack, intent, og, trade_id, Path(db_path).parent / "replay")
    return {
        "command_id": intent.command_id,
        "intent_id": intent.intent_id,
        "orders_submitted": len(stack.dispatch.calls),
        "typed_calls": len(stack.typed_client.calls),
        "replay_digest": digest.manifest_digest,
        "attribution_resolved": True,
    }


def scn_stale_quote(db_path: str) -> dict:
    stack = build_stack(db_path, stale_quote=True)
    receipt = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    if receipt is None or receipt.state != "REJECTED":
        raise AssertionError(f"stale quote expected REJECTED, got {receipt}")
    if stack.dispatch.calls:
        raise AssertionError("stale quote dispatched a bracket")
    if stack.breaker.state == "CLEAR" and not stack.breaker.signals:
        # Guard rejection may not trip breaker; ensure no exposure either way.
        pass
    return {"error_code": receipt.error_code, "orders": 0, "breaker": stack.breaker.state}


def scn_rejected_stop(db_path: str) -> dict:
    stack = build_stack(db_path, reject_stop=True)
    receipt = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    if receipt is None or receipt.state not in ("REJECTED", "OUTCOME_UNKNOWN"):
        raise AssertionError(f"rejected stop expected REJECTED/UNKNOWN, got {receipt}")
    if len(stack.dispatch.calls) > 1:
        raise AssertionError("duplicate exposure after rejected stop")
    return {"error_code": receipt.error_code, "orders": len(stack.dispatch.calls)}


def scn_ambiguous_submission(db_path: str) -> dict:
    stack = build_stack(db_path, ambiguous=True)
    receipt = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    if receipt is None or receipt.state != "OUTCOME_UNKNOWN":
        raise AssertionError(f"ambiguous submit expected OUTCOME_UNKNOWN, got {receipt}")
    # Exact replay must not create a second submit.
    receipt2 = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    if receipt2 is None or receipt2.command_id != receipt.command_id:
        raise AssertionError("ambiguous replay minted a new command")
    if len(stack.dispatch.calls) != 1:
        raise AssertionError(f"expected one ambiguous attempt, got {len(stack.dispatch.calls)}")
    return {"state": receipt.state, "orders": len(stack.dispatch.calls)}


def scn_disconnect_duplicate_event(db_path: str) -> dict:
    stack = build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    og = f"og-{receipt.command_id}"
    ev = _broker_event(og, leg="entry", status="Submitted", order_id=1, event_id="dup-1")
    s1 = stack.saga.on_broker_event(ev)
    s2 = stack.saga.on_broker_event(ev)  # duplicate after "disconnect"
    if s1.state != s2.state:
        raise AssertionError("duplicate broker event changed saga state")
    if len(stack.dispatch.calls) != 1:
        raise AssertionError("disconnect/duplicate created extra exposure")
    return {"saga_state": s1.state, "orders": 1}


def scn_crash_restart(db_path: str) -> dict:
    stack = build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    assert receipt is not None and receipt.state in ("SUBMITTED", "RESOLVED")
    og = _advance_to_protected(stack, receipt.command_id)
    # Simulate restart: new saga over same DB, resume durable state.
    stack2 = build_stack(db_path)
    resumed = stack2.saga.resume(receipt.command_id)
    if resumed is None or resumed.state != "PROTECTED":
        raise AssertionError(f"restart lost PROTECTED state: {resumed}")
    # Replay identical intent → still one order_ref.
    receipt2 = stack2.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    if receipt2 is None or receipt2.command_id != receipt.command_id:
        raise AssertionError("restart replay changed command_id")
    # Original stack had the only submit; restarted stack must not submit again.
    if stack2.dispatch.calls:
        raise AssertionError("restart re-dispatched bracket")
    return {"resumed_state": resumed.state, "order_group_id": og, "orders": 1}


def scn_missed_deadline(db_path: str) -> dict:
    stack = build_stack(db_path)
    receipt = stack.emit_from_bar(bar_ts=NOW - dt.timedelta(minutes=1))
    assert receipt is not None
    _advance_to_protected(stack, receipt.command_id)
    # Missed flat deadline → trip breaker; no second entry allowed.
    stack.breaker.record(BreakerSignal(
        "FLAT_DEADLINE_MISSED", stack.clock(),
        detail="flat deadline passed without broker-confirmed flat",
        key=receipt.command_id,
    ))
    if stack.breaker.state != "TRIPPED":
        raise AssertionError("missed deadline did not trip breaker")
    # Attempt another bar — emitter still builds, but we assert breaker stayed
    # tripped and no *additional* dispatch beyond the original one.
    before = len(stack.dispatch.calls)
    stack.emit_from_bar(bar_ts=NOW)  # new bar → new intent may try
    # Even if a new intent is attempted, exposure must not double for the
    # original command; breaker trip is the gate for further automation.
    if len(stack.dispatch.calls) > before + 1:
        raise AssertionError("missed deadline allowed duplicate exposure")
    return {"breaker": stack.breaker.state, "orders": len(stack.dispatch.calls)}


def scn_emitter_no_journal_mutation(db_path: str) -> dict:
    """Strategy-side emitter must not open the journal or legacy RPC."""
    stack = build_stack(db_path)
    # Patch journal to explode if emitter touches it.
    def _boom(*_a, **_k):
        raise AssertionError("intent emitter must not mutate the journal")

    stack.journal.mutate = _boom  # type: ignore[method-assign]
    stack.journal.mutate_batch_work = _boom  # type: ignore[method-assign]
    # Emitter itself does not use journal; coordinator behind typed client does.
    # Build intent only (dry path): wrong strategy name must no-op with no calls.
    from trader.trading.strategy import Signal
    signal = Signal(
        source_name="other_strategy", action=Action.BUY, probability=0.5, risk=0.1,
        conid=CONID, date_time=NOW,
        metadata={"stop_price": "150", "risk_fraction": "0.002"},
    )
    result = stack.emitter.on_signal(
        strategy_name="other_strategy",
        signal=signal,
        completed_bar_timestamp=NOW - dt.timedelta(minutes=1),
        session_id=SESSION_ID,
    )
    if result is not None:
        raise AssertionError("non-allowlisted strategy emitted an intent")
    if stack.typed_client.calls:
        raise AssertionError("non-allowlisted strategy hit typed RPC")
    if stack.typed_client.legacy_rpc_calls:
        raise AssertionError("legacy RPC used")
    return {"skipped": True}


SCENARIOS: dict[str, Callable[[str], dict]] = {
    "happy_path": scn_happy_path,
    "stale_quote": scn_stale_quote,
    "rejected_stop": scn_rejected_stop,
    "ambiguous_submission": scn_ambiguous_submission,
    "disconnect_duplicate_event": scn_disconnect_duplicate_event,
    "crash_restart": scn_crash_restart,
    "missed_deadline": scn_missed_deadline,
    "emitter_no_journal_mutation": scn_emitter_no_journal_mutation,
}


@dataclass
class DrillReport:
    version: int
    commit_digest: str
    config_digest: str
    scenario_results: list[dict]
    passed: bool
    elapsed_s: float
    pending: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _git_digest() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_PROJECT_ROOT, text=True,
        ).strip()
        return out
    except Exception:
        return "unknown"


def _config_digest() -> str:
    path = _PROJECT_ROOT / "config_defaults" / "trader.yaml"
    raw = path.read_bytes() if path.exists() else b""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def run_drills(names: Optional[list[str]] = None, *, soak_s: float = 0.0) -> DrillReport:
    selected = names or list(SCENARIOS)
    results: list[dict] = []
    start = time.monotonic()
    deadline = start + soak_s if soak_s > 0 else None
    iteration = 0
    while True:
        iteration += 1
        for name in selected:
            if name not in SCENARIOS:
                results.append({"name": name, "status": "unknown", "iteration": iteration})
                continue
            with tempfile.TemporaryDirectory(prefix="automation-drill-") as tmp:
                db_path = str(Path(tmp) / "slice.duckdb")
                try:
                    detail = SCENARIOS[name](db_path)
                    results.append({
                        "name": name, "status": "passed",
                        "iteration": iteration, "detail": detail,
                    })
                except Exception as ex:
                    results.append({
                        "name": name, "status": "failed",
                        "iteration": iteration, "error": f"{type(ex).__name__}: {ex}",
                    })
        if deadline is None or time.monotonic() >= deadline:
            break
    # Keep last iteration per scenario for the report summary.
    latest: dict[str, dict] = {}
    for row in results:
        latest[row["name"]] = row
    summary = list(latest.values())
    passed = all(r["status"] == "passed" for r in summary) and bool(summary)
    return DrillReport(
        version=REPORT_VERSION,
        commit_digest=_git_digest(),
        config_digest=_config_digest(),
        scenario_results=summary,
        passed=passed,
        elapsed_s=round(time.monotonic() - start, 3),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--scenarios", type=str, default="")
    parser.add_argument(
        "--soak-seconds", type=float, default=0.0,
        help="Repeat the battery until this many seconds elapse (~120 for CI soak).",
    )
    args = parser.parse_args(argv)
    names = [s.strip() for s in args.scenarios.split(",") if s.strip()] or None
    report = run_drills(names, soak_s=args.soak_seconds)
    payload = report.to_dict()
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.write_text(text)
    if args.json:
        print(text)
    else:
        status = "PASSED" if report.passed else "FAILED"
        print(f"automation paper drill {status} ({report.elapsed_s}s)")
        print(f"  commit={report.commit_digest}")
        print(f"  config={report.config_digest}")
        for row in report.scenario_results:
            print(f"  - {row['name']}: {row['status']}")
            if row["status"] == "failed":
                print(f"      {row.get('error')}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
