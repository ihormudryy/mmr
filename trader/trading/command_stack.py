"""Production composition root for the trader-owned command authority."""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.data.proposal_repository import (
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.data.circuit_breaker_store import (
    CircuitBreakerStore,
    apply_circuit_breaker_migration,
)
from trader.data.universe import Universe
from trader.trading.command_coordinator import (
    ApprovalCommandService,
    CancelCommandService,
    CommandAudit,
    CommandLedger,
    OutcomeReconciler,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.command_alerts import LoggingCriticalAlertPort
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.command_ports import (
    TraderBrokerAuthority,
    TraderBrokerRiskSnapshotAuthority,
    TraderPositionAuthority,
    TraderQuoteAuthority,
)
from trader.trading.preflight_nonce import (
    PreflightNonceGate,
    apply_preflight_nonce_migration,
)
from trader.trading.proposal_command_service import ProposalCommandService
from trader.trading.risk_producer import RiskProducer
from trader.trading.dispatch_guard import DispatchGuard
from trader.trading.circuit_breaker import CircuitBreaker
from trader.trading.circuit_breaker import BreakerSignal
from trader.trading.liquidation_service import LiquidationService, LiquidationRunStore, apply_liquidation_migration
from trader.trading.order_correlation import encode_order_ref
from trader.trading.semantic_readiness import (
    SemanticReadiness,
    xnys_session_key,
    xnys_session_open,
)
from trader.trading.trading_control import (
    TradingControlStore,
    apply_trading_control_migration,
)


class CommandStackConfigurationError(RuntimeError):
    """A required production adapter is absent while authority is enabled."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class _UnavailableStrategyControl:
    """Fail-safe reconciler seam while strategy mutations remain unregistered."""

    def forward(self, request):
        raise RuntimeError("strategy control authority is not configured")

    def get_receipt(self, command_id: str):
        return None


class _BrokerStoreOrderView:
    def __init__(self, store, journal):
        self._store = store
        self._journal = journal

    def get_order(self, order_entity_id: str):
        return self._store.get_order_in_tx(self._journal.connect(), order_entity_id)


class _UniverseAuthority:
    def __init__(self, accessor):
        self._accessor = accessor

    def resolve_conid(self, conid: int):
        rows = self._accessor.resolve_symbol(conid, first_only=True)
        return rows[0] if rows else None


class _LiquidationDispatch:
    """Adapter which keeps emergency flattening on the existing IB boundary."""
    def __init__(self, dispatch):
        self._dispatch = dispatch

    def cancel(self, order, command_id: str) -> None:
        self._dispatch.cancel(order.order_entity_id, encode_order_ref(command_id))

    def reduce(self, position, side: str, quantity: float, command_id: str) -> None:
        self._dispatch.reduce_position(position, side, quantity, encode_order_ref(command_id))


class _LiquidationBreaker:
    def __init__(self, breaker: CircuitBreaker, now):
        self._breaker = breaker
        self._now = now

    def trip_liquidation(self, cause_command_id: str, detail: str) -> None:
        self._breaker.record(BreakerSignal(
            "LIQUIDATION_FAILED", self._now(), detail=detail, key=cause_command_id,
        ))


def _load_canary_public_keys(key_ring_path: str) -> list:
    """Load every ``*.pem`` Ed25519 public key from ``key_ring_path``.

    Mirrors ``StrategyRuntime._get_artifact_verifier``'s exact key-ring
    loading convention (P3 Task 2) for consistency across services. Returns
    an empty list (never raises) on any I/O/parse problem so a misconfigured
    ring degrades to "canary activation stays dormant" rather than crashing
    trader_service startup.
    """
    import glob as _glob
    import logging
    import os as _os

    from trader.research import signing

    keys: list = []
    try:
        abs_path = _os.path.abspath(_os.path.expanduser(key_ring_path))
        pem_files = sorted(_glob.glob(_os.path.join(abs_path, "*.pem")))
        for pem_path in pem_files:
            try:
                keys.append(signing.load_verify_key(pem_path))
            except Exception as exc:
                logging.error("failed to load canary public key %s: %s", pem_path, exc)
    except Exception as exc:
        logging.error("failed to enumerate canary key ring %s: %s", key_ring_path, exc)
    return keys


def _build_canary_service(
    trader: Any,
    stage_machine: Any,
    evidence_store: Any,
    authority_store: Any,
    *,
    account_id: str,
    account_mode: str,
    semantic_readiness: SemanticReadiness,
    broker_flat_reconciled: Callable[[], bool],
    breaker_clear: Callable[[], bool],
    now: Callable[[], dt.datetime],
) -> Optional[Any]:
    """Build ``CanaryActivationService`` iff a canary signing-key ring AND an
    artifact bundle to re-verify against are BOTH configured on ``trader``.

    Absent either (the default -- no such attributes exist on ``Trader``
    until ops config adds them), returns ``None`` and the two commands are
    never registered (dormant), never partially wired.
    """
    key_ring_path = getattr(trader, "canary_public_key_ring_path", None)
    bundle_path_str = getattr(trader, "canary_artifact_bundle_path", None)
    expected_artifact_id = getattr(trader, "canary_expected_artifact_id", None)
    if not key_ring_path or not bundle_path_str or not expected_artifact_id:
        return None

    public_keys = _load_canary_public_keys(key_ring_path)
    if not public_keys:
        return None

    import os as _os
    from pathlib import Path as _Path

    from trader.automation.artifact_verifier import ArtifactVerifier
    from trader.promotion.canary_attestation import CanaryAuthorityVerifier, ExpectedCanaryBindings
    from trader.promotion.controller import CanaryActivationService

    canary_verifier = CanaryAuthorityVerifier(trusted_public_keys=public_keys)
    artifact_verifier = ArtifactVerifier(trusted_public_keys=public_keys)
    bundle_path = _Path(_os.path.abspath(_os.path.expanduser(bundle_path_str)))

    def expected_bindings():
        # Re-run the FULL artifact chain (bundle integrity, signature,
        # state/mode gate, expiry, revocation, read-only mount in live
        # mode) fresh on every activation attempt -- never a cached copy.
        verified = artifact_verifier.verify(bundle_path, "live", expected_artifact_id, now())
        return ExpectedCanaryBindings(
            account_id=account_id, artifact_digest=verified.artifact_id,
            allowlist_digest=verified.allowlist_digest, ruleset_digest=verified.ruleset_digest,
        )

    return CanaryActivationService(
        stage_machine=stage_machine, evidence_store=evidence_store, authority_store=authority_store,
        verifier=canary_verifier, expected_bindings=expected_bindings,
        semantic_readiness_ready=lambda: semantic_readiness.evaluate(now()).ready,
        broker_flat_reconciled=broker_flat_reconciled, breaker_clear=breaker_clear, now=now,
    )


def _build_allocation_service(
    trader: Any,
    authority_store: Any,
    *,
    account_id: str,
    account_mode: str,
    semantic_readiness: SemanticReadiness,
    broker_flat_reconciled: Callable[[], bool],
    breaker_clear: Callable[[], bool],
    now: Callable[[], dt.datetime],
) -> Optional[Any]:
    """Build ``AllocationActivationService`` when scaling keys + artifact are configured."""
    key_ring_path = (
        getattr(trader, "allocation_public_key_ring_path", None)
        or getattr(trader, "canary_public_key_ring_path", None)
    )
    bundle_path_str = (
        getattr(trader, "allocation_artifact_bundle_path", None)
        or getattr(trader, "canary_artifact_bundle_path", None)
    )
    expected_artifact_id = (
        getattr(trader, "allocation_expected_artifact_id", None)
        or getattr(trader, "canary_expected_artifact_id", None)
    )
    if not key_ring_path or not bundle_path_str or not expected_artifact_id:
        return None

    public_keys = _load_canary_public_keys(key_ring_path)
    if not public_keys:
        return None

    import os as _os
    from pathlib import Path as _Path

    from trader.automation.artifact_verifier import ArtifactVerifier
    from trader.promotion.allocation_attestation import AllocationAttestationVerifier, ExpectedAllocationBindings
    from trader.promotion.controller import AllocationActivationService

    keys_by_id = {}
    for key in public_keys:
        from trader.research.signing import public_key_id
        keys_by_id[public_key_id(key)] = key

    allocation_verifier = AllocationAttestationVerifier(trusted_public_keys=keys_by_id)
    artifact_verifier = ArtifactVerifier(trusted_public_keys=public_keys)
    bundle_path = _Path(_os.path.abspath(_os.path.expanduser(bundle_path_str)))

    def expected_bindings():
        verified = artifact_verifier.verify(bundle_path, account_mode, expected_artifact_id, now())
        return ExpectedAllocationBindings(
            account_id=account_id,
            account_mode=account_mode,
            artifact_digest=verified.artifact_id,
            allowlist_digest=verified.allowlist_digest,
            ruleset_digest=verified.ruleset_digest,
            strategy_id=getattr(trader, "allocation_strategy_id", "") or "default",
        )

    return AllocationActivationService(
        authority_store=authority_store,
        verifier=allocation_verifier,
        expected_bindings=expected_bindings,
        semantic_readiness_ready=lambda: semantic_readiness.evaluate(now()).ready,
        broker_flat_reconciled=broker_flat_reconciled,
        breaker_clear=breaker_clear,
        now=now,
    )


@dataclass(frozen=True)
class CommandStack:
    journal: Any
    repository: ProposalRepository
    ledger: CommandLedger
    controls: TradingControlStore
    nonces: PreflightNonceGate
    coordinator: TradingCommandCoordinator
    reconciler: OutcomeReconciler
    proposal_service: ProposalCommandService
    approval_service: ApprovalCommandService
    cancel_service: CancelCommandService
    account_mode: str
    resume_ready: Callable[[], bool]
    reconciliation_complete: Callable[[str], bool]
    circuit_breaker: CircuitBreaker
    semantic_readiness: SemanticReadiness
    liquidation_service: LiquidationService
    session_risk: Any = None  # SessionRiskController when automation stack is active
    protective_order_saga: Any = None  # ProtectiveOrderSaga (P3 Task 5)
    session_controller: Any = None  # SessionController (P3 Task 6)
    attribution_ledger: Any = None  # AttributionLedger (P3 Task 7)
    dispatch_guard: Any = None
    promotion_stage_machine: Any = None  # PromotionStageMachine (P4 Task 1)
    promotion_evidence_store: Any = None  # EvidenceStore (P4 Task 1)
    canary_authority_store: Any = None  # LiveActivationAuthorityStore (P4 Task 5)
    canary_service: Any = None  # CanaryActivationService (P4 Task 5) -- None until a
    # canary public-key ring is configured (dormant by default; see build_command_stack)
    allocation_service: Any = None  # AllocationActivationService (P5 Task 3)
    automated_intent_service: Any = None  # AutomatedIntentCommandService (paper automation)


_REQUIRED_TRADER_PORTS = (
    ("domain_journal", "MISSING_DOMAIN_JOURNAL"),
    ("journal_db", "MISSING_JOURNAL_DB"),
    ("broker_state_store", "MISSING_BROKER_STATE"),
    ("broker_ingest", "MISSING_BROKER_INGEST"),
    ("risk_gate", "MISSING_RISK_GATE"),
    ("universe_accessor", "MISSING_UNIVERSE"),
    ("portfolio", "MISSING_POSITIONS"),
    ("book", "MISSING_ORDER_BOOK"),
    ("client", "MISSING_BROKER_CLIENT"),
)


def _require_trader_ports(trader: Any) -> None:
    for attribute, code in _REQUIRED_TRADER_PORTS:
        if getattr(trader, attribute, None) is None:
            raise CommandStackConfigurationError(
                code, f"trader.{attribute} is required when command authority is enabled",
            )
    if not getattr(trader, "ib_account", None):
        raise CommandStackConfigurationError(
            "MISSING_ACCOUNT", "trader.ib_account must be pinned",
        )


def _run_on_trader_loop(trader: Any, coroutine: Any, timeout: float = 5.0):
    loop = getattr(trader, "_main_loop", None)
    if loop is None or not loop.is_running():
        close = getattr(coroutine, "close", None)
        if close is not None:
            close()
        raise RuntimeError("trader event loop is unavailable")
    return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=timeout)


def _resolve_security(trader: Any, conid: int):
    rows = trader.universe_accessor.resolve_symbol(conid, first_only=True)
    return rows[0] if rows else None


def _resolve_contract(trader: Any, conid: int):
    security = _resolve_security(trader, conid)
    return None if security is None else Universe.to_contract(security)


def _broker_ready(trader: Any) -> bool:
    if not trader.is_ib_connected():
        return False
    readiness = trader.broker_ingest.is_ready
    return bool(readiness() if callable(readiness) else readiness)


def _build_automated_intent_service(
    trader: Any,
    *,
    ledger: CommandLedger,
    audit: CommandAudit,
    journal: Any,
    controls: TradingControlStore,
    dispatch: Any,
    protective_order_saga: Any,
    account_id: str,
    account_mode: str,
    now: Callable[[], dt.datetime],
    schedule_reconcile: Optional[Callable[[str], None]],
) -> Optional[Any]:
    """Build ``AutomatedIntentCommandService`` for paper automation only.

    Requires ``trader.automation_enabled`` and configured artifact path +
    public key ring + expected artifact id. Live automation stays refused.
    Returns ``None`` when dormant so ``execute_automated_intent`` is never
    registered.
    """
    from types import SimpleNamespace
    import os as _os
    from pathlib import Path as _Path

    if not getattr(trader, "automation_enabled", False):
        return None
    if getattr(trader, "automation_live_enabled", False):
        raise CommandStackConfigurationError(
            "AUTOMATION_LIVE_REFUSED",
            "automation_live_enabled=true is refused (hybrid design R3)",
        )
    if account_mode != "paper":
        return None
    key_ring = getattr(trader, "automation_public_key_ring_path", "") or ""
    bundle_path = getattr(trader, "automation_artifact_bundle_path", "") or ""
    expected_id = getattr(trader, "automation_expected_artifact_id", "") or ""
    if not key_ring or not bundle_path or not expected_id:
        raise CommandStackConfigurationError(
            "AUTOMATION_CONFIG_INCOMPLETE",
            "automation_enabled requires artifact_bundle_path, "
            "public_key_ring_path, and expected_artifact_id",
        )

    from trader.automation.artifact_verifier import ArtifactVerifier
    from trader.automation.automated_intent_command import AutomatedIntentCommandService

    public_keys = _load_canary_public_keys(key_ring)
    if not public_keys:
        raise CommandStackConfigurationError(
            "AUTOMATION_KEY_RING_EMPTY",
            f"no usable *.pem verify keys under {key_ring!r}",
        )
    verifier = ArtifactVerifier(trusted_public_keys=public_keys)
    root = _Path(_os.path.abspath(_os.path.expanduser(bundle_path)))
    bundle_root = root.parent if root.name.startswith("artifact-") else root

    def approval_factory(*, intent, command):
        from trader.data.broker_state import BrokerRiskSnapshot
        from trader.trading.approval_context import (
            ApprovalContext, ExecutableMarketEvidence,
        )
        from trader.trading.command_coordinator import RiskDirection
        from trader.trading.proposal_command_service import ExecutableQuote

        quote = ExecutableQuote(
            conid=intent.conid, side=intent.side, price=0.0,
            market_timestamp=now(), feed_type="realtime", session_state="open",
            bid=0.0, ask=0.0,
        )
        snap = BrokerRiskSnapshot(
            generation_id=0, source_cursor=0, promoted_at=now(),
            account_id=account_id, account_mode=account_mode,
            net_liquidation=0.0, daily_pnl=0.0, positions=(), working_orders=(),
        )
        return ApprovalContext(
            conid=intent.conid, side=intent.side, quantity=0.0,
            reference_price=0.0, max_drift_bps=50.0,
            risk_direction=RiskDirection.INCREASING,
            broker=snap,
            market=ExecutableMarketEvidence(quote=quote, received_at=now()),
            what_if=None,
        )

    return AutomatedIntentCommandService(
        ledger=ledger,
        audit=audit,
        journal=journal,
        controls=controls,
        dispatch=dispatch,
        artifact_verifier=verifier,
        account_id=account_id,
        account_mode=account_mode,
        now=now,
        bundle_root=bundle_root,
        schedule_reconcile=schedule_reconcile,
        protective_saga=protective_order_saga,
        approval_factory=approval_factory,
        session_state_factory=lambda **_kw: SimpleNamespace(
            state="OPEN",
            entry_cutoff_reached=False,
            high_water_mark=None,
            expected_account_id=account_id,
            liquidity=None,
        ),
        allocation_factory=lambda **_kw: SimpleNamespace(
            max_gross_allocation=0.06,
            strategy_allocation=0.06,
            max_gross_fraction=0.06,
        ),
    )


def build_command_stack(
    trader: Any,
    policy: CommandAuthorityPolicy,
    now: Callable[[], dt.datetime],
) -> Optional[CommandStack]:
    """Compose every currently production-ready command adapter.

    Disabled policy is deliberately dormant. Enabled policy is fail-loud: no
    partial registry is returned when a required adapter is absent.
    """
    if not policy.enabled:
        return None
    _require_trader_ports(trader)

    journal = trader.domain_journal
    migrator = SchemaMigrator(trader.journal_db)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)
    apply_preflight_nonce_migration(migrator)
    apply_circuit_breaker_migration(migrator)
    apply_liquidation_migration(migrator)
    from trader.promotion.evidence_store import apply_evidence_migrations
    from trader.promotion.stage import apply_stage_migration
    from trader.promotion.controller import apply_live_activation_authority_migration

    apply_stage_migration(migrator)
    apply_evidence_migrations(migrator)
    apply_live_activation_authority_migration(migrator)
    from trader.promotion.canary_risk import apply_canary_risk_migration
    from trader.operations.session_checklist import apply_session_checklist_migration

    apply_canary_risk_migration(migrator)
    apply_session_checklist_migration(migrator)
    from trader.data.allocation_authority_store import apply_allocation_authority_migrations

    apply_allocation_authority_migrations(migrator)
    from trader.data.allocation_authority_store import AllocationAuthorityStore
    from trader.data.portfolio_risk_authority_store import (
        PortfolioRiskAuthorityStore,
        apply_portfolio_risk_authority_migrations,
    )
    from trader.promotion.allocation_policy import AllocationPolicy
    from trader.promotion.degradation_reaction import react_to_breaker_trip

    apply_portfolio_risk_authority_migrations(migrator)
    allocation_authority_store = AllocationAuthorityStore(
        journal=journal, db=trader.journal_db, now=now,
    )
    portfolio_risk_authority_store = PortfolioRiskAuthorityStore(
        journal=journal, db=trader.journal_db, now=now,
    )
    allocation_policy = AllocationPolicy(now=now)
    from trader.automation.protective_order_saga import (
        ProtectiveBracketDispatch,
        ProtectiveOrderSaga,
        apply_protective_order_saga_migration,
    )
    apply_protective_order_saga_migration(migrator)

    repository = ProposalRepository(journal)
    ledger = CommandLedger(journal)
    controls = TradingControlStore(journal)
    account_mode = "paper" if trader.paper_trading else "live"
    trader.journal_db.transaction(
        lambda conn: controls.seed_in_tx(conn, [(trader.ib_account, account_mode)], now())
    )
    nonces = PreflightNonceGate(journal, now=now)
    positions = TraderPositionAuthority(trader)
    run_coro = lambda coro: _run_on_trader_loop(trader, coro)
    resolve_contract = lambda conid: _resolve_contract(trader, conid)
    quotes = TraderQuoteAuthority(
        trader, run_coro=run_coro, resolve_contract=resolve_contract, delayed=False,
    )
    broker_snapshot = TraderBrokerRiskSnapshotAuthority(
        db=trader.journal_db,
        store=trader.broker_state_store,
        account_id=trader.ib_account,
        account_mode=account_mode,
        ready=lambda: _broker_ready(trader),
    )

    def resume_ready() -> bool:
        """Require current, fenced broker evidence immediately before resume."""
        try:
            broker_snapshot.capture(trader.ib_account)
        except Exception:
            return False
        return True

    breaker_store = CircuitBreakerStore(journal, trader.ib_account)
    breaker_store.seed(now())

    def journal_writable() -> bool:
        def probe(conn):
            conn.execute(
                "UPDATE automation_circuit_breaker SET revision=revision WHERE account_id=?",
                [trader.ib_account],
            )
            return True
        return bool(trader.journal_db.transaction(probe))

    def control_readable() -> bool:
        try:
            controls.get(trader.ib_account)
        except Exception:
            return False
        return True

    def quotes_ready() -> bool:
        instruments = tuple(getattr(trader, "automated_instruments", ()) or ())
        if not instruments:
            return True
        probe = getattr(trader, "automation_quotes_ready", None)
        return bool(probe(instruments)) if callable(probe) else False
    margin = TraderBrokerAuthority(
        trader, run_coro=run_coro, resolve_contract=resolve_contract,
    )
    dispatch_guard = DispatchGuard(
        broker=broker_snapshot, quotes=quotes, margin=margin,
        controls=controls, risk_gate=trader.risk_gate, policy=policy,
        account_id=trader.ib_account, account_mode=account_mode,
        allocation_policy=allocation_policy,
        allocation_authority_lookup=lambda account_id, artifact_digest: (
            allocation_authority_store.active_for(account_id, artifact_digest)
        ),
    )

    def compute_risk_projection():
        snapshot = broker_snapshot.capture(trader.ib_account)
        return {
            "net_liquidation": snapshot.net_liquidation,
            "daily_pnl": snapshot.daily_pnl,
            "open_order_count": snapshot.open_order_count,
        }

    risk_producer = RiskProducer(
        trader.journal_db,
        journal,
        trader.ib_account,
        compute_projection=compute_risk_projection,
        clock=now,
    )
    risk_producer.migrate(migrator)

    from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

    dispatch = TradingRuntimeOrderDispatch(trader, policy=policy)
    orders_view = _BrokerStoreOrderView(trader.broker_state_store, journal)
    alerts = LoggingCriticalAlertPort(now=now)
    strategy = _UnavailableStrategyControl()
    reconciler = OutcomeReconciler(
        journal=journal,
        ledger=ledger,
        orders=dispatch,
        strategy=strategy,
        alerts=alerts,
        repo=repository,
        orders_view=orders_view,
        now=now,
    )
    coordinator = TradingCommandCoordinator(
        journal=journal,
        ledger=ledger,
        audit=CommandAudit(journal),
        nonces=nonces,
        now=now,
        reconciler=reconciler,
    )

    def reconciliation_complete(command_id: str) -> bool:
        return not ledger.unresolved_for_account(
            trader.ib_account, exclude_command_id=command_id,
        )

    def reconciliation_safe() -> bool:
        return not ledger.unresolved_for_account(trader.ib_account)

    semantic_readiness = SemanticReadiness(
        ib_connected=lambda: bool(trader.is_ib_connected()),
        account_pinned=lambda: bool(trader.ib_account),
        broker_current=resume_ready,
        journal_writable=journal_writable,
        reconciliation_safe=reconciliation_safe,
        control_readable=control_readable,
        breaker_clear=lambda: breaker_store.get().state == "CLEAR",
        session_open=xnys_session_open,
        command_stack_active=lambda: getattr(trader, "command_stack", None) is not None,
        quotes_ready=quotes_ready,
    )

    def reset_ready() -> bool:
        # Breaker state itself is deliberately excluded: reset is the action
        # that changes it. Reconciliation is checked by CircuitBreaker as a
        # separate, explicit precondition.
        return all((
            bool(trader.is_ib_connected()), bool(trader.ib_account), resume_ready(),
            journal_writable(), control_readable(), xnys_session_open(now()), quotes_ready(),
        ))

    circuit_breaker = CircuitBreaker(
        breaker_store,
        now=now,
        reset_ready=reset_ready,
        reconciliation_complete=reconciliation_safe,
        session_key=xnys_session_key,
        on_trip=lambda state: react_to_breaker_trip(
            allocation_authority_store,
            account_id=trader.ib_account,
            breaker_state=state,
            now=now(),
        ),
    )
    liquidation_service = LiquidationService(
        broker_snapshot, _LiquidationDispatch(dispatch),
        breaker=_LiquidationBreaker(circuit_breaker, now), now=now,
        store=LiquidationRunStore(trader.journal_db),
        journal=journal, ledger=ledger,
    )
    proposal_service = ProposalCommandService(
        repository=repository,
        journal=journal,
        risk_gate=trader.risk_gate,
        quotes=quotes,
        universe=_UniverseAuthority(trader.universe_accessor),
        account_id=trader.ib_account,
        account_mode=account_mode,
        now=now,
        controls=controls,
        positions=positions,
    )
    approval_service = ApprovalCommandService(
        journal=journal,
        ledger=ledger,
        repo=repository,
        controls=controls,
        orders=dispatch,
        quotes=quotes,
        risk_gate=trader.risk_gate,
        risk_producer=risk_producer,
        reconciler=reconciler,
        broker=broker_snapshot,
        account_id=trader.ib_account,
        account_mode=account_mode,
        now=now,
        dispatch_guard=dispatch_guard,
    )
    cancel_service = CancelCommandService(
        journal=journal,
        ledger=ledger,
        orders_view=orders_view,
        dispatch=dispatch,
        nonces=nonces,
        risk_producer=risk_producer,
        reconciler=reconciler,
        coordinator=coordinator,
        now=now,
    )
    # P3 Task 4 — trader-owned session/liquidity risk (evaluate-only; saga uses it in Task 5).
    from trader.automation.calendar_policy import XNYSCalendarPolicy
    from trader.automation.session_risk import SessionRiskController

    session_risk = SessionRiskController(
        calendar=XNYSCalendarPolicy(),
        breaker=circuit_breaker,
        allocation_policy=allocation_policy,
        portfolio_authority_present=lambda account_id: (
            portfolio_risk_authority_store.active_for(account_id) is not None
        ),
        strategy_count=lambda: allocation_authority_store.active_strategy_count(
            trader.ib_account,
        ),
        now=now,
    )
    # P3 Task 5 — protective entry saga over existing expressive-order path.
    protective_dispatch = ProtectiveBracketDispatch(dispatch)
    protective_order_saga = ProtectiveOrderSaga(
        journal=journal,
        ledger=ledger,
        dispatch=protective_dispatch,
        dispatch_guard=dispatch_guard,
        session_risk=session_risk,
        breaker=circuit_breaker,
        liquidation=liquidation_service,
        account_id=trader.ib_account,
        account_mode=account_mode,
        now=now,
        db=trader.journal_db,
    )
    # P3 Task 6 — exchange-aware session deadlines / flatten scheduler.
    from trader.automation.session_controller import (
        SessionCancelAdapter,
        SessionController,
        SessionTimeExitAdapter,
        apply_session_controller_migration,
    )
    apply_session_controller_migration(migrator)
    session_controller = SessionController(
        journal=journal,
        db=trader.journal_db,
        calendar=XNYSCalendarPolicy(),
        broker=broker_snapshot,
        cancel=SessionCancelAdapter(_LiquidationDispatch(dispatch)),
        liquidation=liquidation_service,
        breaker=circuit_breaker,
        time_exit=SessionTimeExitAdapter(_LiquidationDispatch(dispatch)),
        account_id=trader.ib_account,
        now=now,
    )
    # P3 Task 7 — authoritative attribution ledger; broker_ingest appends evidence.
    from trader.automation.attribution import AttributionLedger
    from trader.data.attribution_store import apply_attribution_migrations

    apply_attribution_migrations(migrator)
    attribution_ledger = AttributionLedger(
        journal=journal,
        db=trader.journal_db,
        account_id=trader.ib_account,
        now=now,
    )
    ingest = getattr(trader, "broker_ingest", None)
    if ingest is not None:
        ingest.attribution_ledger = attribution_ledger
        ingest.protective_order_saga = protective_order_saga

    # P4 Task 5 — promotion stage machine + evidence store + the
    # append-only canary-authority ledger are always constructed (cheap,
    # no external dependency) so `mmr strategies inspect`-style reporting
    # and PromotionController.prepare_canary (which runs entirely offline,
    # never through this stack) have a durable stage to read once a
    # strategy starts accumulating paper evidence. The authenticated
    # `activate_live_canary`/`deactivate_live_canary` COMMANDS, however,
    # stay dormant (never registered -- see production_api.py's
    # `canary_service is not None` guard) until a canary signing-key ring
    # is actually configured: `trader.canary_public_key_ring_path`,
    # `trader.canary_artifact_bundle_path`, and
    # `trader.canary_expected_artifact_id` are ops config this task
    # deliberately does not invent defaults for -- an unconfigured trader
    # must never expose a live-canary activation surface.
    from trader.promotion.controller import CanaryActivationService, LiveActivationAuthorityStore
    from trader.promotion.evidence_store import EvidenceStore
    from trader.promotion.stage import PromotionStageMachine

    promotion_stage_machine = PromotionStageMachine(journal=journal, db=trader.journal_db, now=now)
    promotion_evidence_store = EvidenceStore(journal=journal, db=trader.journal_db, now=now)
    canary_authority_store = LiveActivationAuthorityStore(journal=journal, db=trader.journal_db, now=now)

    def broker_flat_reconciled() -> bool:
        # "Flat" (zero open positions, zero working orders) AND "reconciled"
        # (no unresolved commands the ledger is still waiting on) -- both
        # halves of the brief's "flat/reconciled broker state" gate.
        snapshot = broker_snapshot.capture(trader.ib_account)
        return (
            len(snapshot.positions) == 0
            and snapshot.open_order_count == 0
            and reconciliation_safe()
        )

    canary_service = _build_canary_service(
        trader, promotion_stage_machine, promotion_evidence_store, canary_authority_store,
        account_id=trader.ib_account, account_mode=account_mode,
        semantic_readiness=semantic_readiness, broker_flat_reconciled=broker_flat_reconciled,
        breaker_clear=lambda: breaker_store.get().state == "CLEAR", now=now,
    )
    allocation_service = _build_allocation_service(
        trader, allocation_authority_store,
        account_id=trader.ib_account, account_mode=account_mode,
        semantic_readiness=semantic_readiness, broker_flat_reconciled=broker_flat_reconciled,
        breaker_clear=lambda: breaker_store.get().state == "CLEAR", now=now,
    )

    automated_intent_service = _build_automated_intent_service(
        trader,
        ledger=ledger,
        audit=CommandAudit(journal),
        journal=journal,
        controls=controls,
        dispatch=dispatch,
        protective_order_saga=protective_order_saga,
        account_id=trader.ib_account,
        account_mode=account_mode,
        now=now,
        schedule_reconcile=lambda command_id: reconciler.schedule(
            command_id, now(),
        ),
    )

    stack = CommandStack(
        journal=journal,
        repository=repository,
        ledger=ledger,
        controls=controls,
        nonces=nonces,
        coordinator=coordinator,
        reconciler=reconciler,
        proposal_service=proposal_service,
        approval_service=approval_service,
        cancel_service=cancel_service,
        account_mode=account_mode,
        resume_ready=resume_ready,
        reconciliation_complete=reconciliation_complete,
        circuit_breaker=circuit_breaker,
        semantic_readiness=semantic_readiness,
        liquidation_service=liquidation_service,
        session_risk=session_risk,
        protective_order_saga=protective_order_saga,
        session_controller=session_controller,
        attribution_ledger=attribution_ledger,
        dispatch_guard=dispatch_guard,
        promotion_stage_machine=promotion_stage_machine,
        promotion_evidence_store=promotion_evidence_store,
        canary_authority_store=canary_authority_store,
        canary_service=canary_service,
        allocation_service=allocation_service,
        automated_intent_service=automated_intent_service,
    )
    trader.command_ledger = ledger
    trader.command_reconciler = reconciler
    trader.command_stack = stack
    trader.trading_control_store = controls
    trader.automation_circuit_breaker = circuit_breaker
    trader.semantic_readiness = semantic_readiness
    trader.liquidation_service = liquidation_service
    trader.session_risk = session_risk
    trader.protective_order_saga = protective_order_saga
    trader.session_controller = session_controller
    trader.attribution_ledger = attribution_ledger
    return stack
