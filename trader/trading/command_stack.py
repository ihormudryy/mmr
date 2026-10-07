"""Production composition root for the trader-owned command authority."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
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
    BrokerStateOrphanEvidence,
    TraderBrokerRiskSnapshotAuthority,
    TraderPositionAuthority,
    TraderQuoteAuthority,
    ingest_ready,
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
from trader.trading.exit_owner import ExitOwnerRegistry
from trader.trading.liquidation_service import LiquidationService, LiquidationRunStore, apply_liquidation_migration
from trader.trading.liquidation_worker import LiquidationWorker, SerializedLiquidation
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


logger = logging.getLogger(__name__)

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


class _JournalOrLiveStrategySnapshot:
    """``StrategySnapshotPort``: journal first, then strategy_service list."""

    def __init__(self, journal, query_provider):
        self._journal = journal
        # Either a TypedRpcClient or a lazy port exposing ``query_client``.
        self._query_provider = query_provider

    def _query_client(self):
        provider = self._query_provider
        if provider is None:
            return None
        if hasattr(provider, "query_client"):
            return provider.query_client
        return provider

    def exists(self, strategy_name: str) -> bool:
        if self._journal.get_entity("strategy", strategy_name) is not None:
            return True
        client = self._query_client()
        if client is None:
            return False
        try:
            response = client.call("list_strategies", {}, dict)
        except Exception:
            return False
        for row in response.get("strategies") or []:
            name = row.get("name") or row.get("strategy_name")
            if name == strategy_name:
                return True
        return False


def _strategy_control_credentials_ready(trader: Any) -> bool:
    """True when enable/disable can be registered (the trader's RPC identity is loaded)."""
    return getattr(trader, "rpc_identity", None) is not None


class _LazyTypedStrategyControlPort:
    """Connects to strategy typed sockets on first forward/get_receipt.

    Eager connect at ``build_command_stack`` time left open ZMQ contexts that
    hung pytest teardown (``Context.term``) and also failed when strategy
    wasn't up yet during trader boot.
    """

    def __init__(self, trader: Any):
        self._trader = trader
        self._port = None
        self._query = None

    def _ensure(self):
        if self._port is not None:
            return self._port
        port, query = _connect_strategy_control_port(self._trader)
        if port is None:
            raise RuntimeError("strategy control authority is not configured")
        self._port = port
        self._query = query
        return self._port

    @property
    def query_client(self):
        self._ensure()
        return self._query

    def forward(self, request):
        return self._ensure().forward(request)

    def get_receipt(self, command_id: str):
        return self._ensure().get_receipt(command_id)


def _connect_strategy_control_port(trader: Any):
    """Typed one-way trader → strategy_service control port, or ``(None, None)``.

    Signs as the trader's own identity. No identity means no port; there is
    no fallback key.
    """
    from trader.messaging.production_api import TypedStrategyControlPort
    from trader.messaging.typed_rpc import TypedRpcClient

    identity = getattr(trader, "rpc_identity", None)
    if identity is None:
        return None, None

    address = (getattr(trader, "strategy_typed_address", None) or "").strip()
    if not address:
        address = "tcp://127.0.0.1"
    cmd_port = int(getattr(trader, "strategy_typed_command_port", 42104) or 42104)
    qry_port = int(getattr(trader, "strategy_typed_query_port", 42105) or 42105)
    command_client = TypedRpcClient(
        "command", identity, server="strategy", address=address, port=cmd_port, timeout=30.0,
    )
    query_client = TypedRpcClient(
        "query", identity, server="strategy", address=address, port=qry_port, timeout=30.0,
    )
    command_client.connect()
    query_client.connect()
    return TypedStrategyControlPort(command_client, query_client), query_client


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
    """Keeps every exit on the one IB boundary; child ids become order refs."""
    def __init__(self, dispatch, orders_view):
        self._dispatch = dispatch
        self._orders_view = orders_view

    def cancel(self, order, child_id: str) -> None:
        self._dispatch.cancel_on_loop(order.order_entity_id, encode_order_ref(child_id))

    def reduce(self, position, side: str, quantity: float, child_id: str) -> None:
        self._dispatch.reduce_position(position, side, quantity, encode_order_ref(child_id))

    def reduce_partial(self, position, side: str, quantity: float, child_id: str) -> None:
        self._dispatch.reduce_partial(position, side, quantity, encode_order_ref(child_id))

    def place_exit_leg(self, position, *, leg: str, quantity: float, price: float,
                       oca_group: str, child_id: str, sibling_child_id: Optional[str] = None) -> None:
        sibling_ref = None if sibling_child_id is None else encode_order_ref(sibling_child_id)
        self._dispatch.place_exit_leg(position, leg=leg, quantity=quantity, price=price,
                                      oca_group=oca_group, order_ref=encode_order_ref(child_id),
                                      oca_sibling_ref=sibling_ref)

    def find_orders(self, account_id: str, child_id: str) -> list:
        return self._dispatch.find_by_order_ref(account_id, encode_order_ref(child_id))

    def find_orders_with_prefix(self, account_id: str, prefix: str) -> list:
        return self._dispatch.find_legacy_reduces(account_id, prefix)

    def get_order(self, order_entity_id: str):
        return self._orders_view.get_order(order_entity_id)

    def executed_quantities(self, account_id: str, order_entity_ids: tuple) -> dict:
        return self._dispatch.executed_quantities(account_id, order_entity_ids)

    def unbound_execution_since(self, account_id: str, conid, generation_id: int) -> bool:
        return self._dispatch.unbound_execution_since(account_id, conid, generation_id)

    def enumeration_complete(self) -> bool:
        return self._dispatch.enumeration_complete()

    def newest_generation(self) -> int:
        return self._dispatch.newest_generation()

    def hold_broker_changes(self):
        return self._dispatch.hold_broker_changes()


class _BrokerGenerationRefresh:
    """Ask for a newer complete broker generation without waiting for it.

    A promoted generation only changes when ``run_broker_sync`` runs, which
    today is only at (re)connect. The close needs newer generations to see
    absence and fresh positions (R4, R5), so it asks for one while it waits.
    """
    def __init__(self, trader, *, min_interval_seconds: float = 5.0,
                 clock: Optional[Callable[[], float]] = None):
        self._trader = trader
        self._min_interval = min_interval_seconds
        self._clock = clock or time.monotonic
        self._last: Optional[float] = None

    def request_refresh(self, account_id: str) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self._min_interval:
            return
        loop = getattr(self._trader, "_main_loop", None)
        sync = getattr(getattr(self._trader, "broker_ingest", None), "run_broker_sync", None)
        if loop is None or not loop.is_running() or sync is None:
            return
        self._last = now
        future = asyncio.run_coroutine_threadsafe(sync(self._trader.client), loop)
        future.add_done_callback(_log_refresh_failure)


def _log_refresh_failure(future) -> None:
    import logging
    if not future.cancelled() and future.exception() is not None:
        logging.getLogger(__name__).warning("broker generation refresh failed: %s", future.exception())


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
    liquidation_service: Any  # SerializedLiquidation: every entry point on one worker (R12)
    session_risk: Any = None  # SessionRiskController when automation stack is active
    liquidation_worker: Any = None  # LiquidationWorker behind liquidation_service (R12)
    exit_owner_registry: Any = None  # ExitOwnerRegistry (SP1 safe close)
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
    paper_automation_service: Any = None  # Paper activation authority (Phase 1+2)
    paper_hot_arm: Any = None  # ProductionPaperHotArmPorts when paper mode
    strategy_control_service: Any = None  # StrategyControlCommandService when wired
    ai_paper: Any = None  # AiPaperServices when ai_paper.enabled (SP1 Plan 3)
    experiments: Any = None  # ExperimentServices on paper (SP1 Plan 4, K15)
    mode_conflict: Optional[str] = None  # "BOTH_MODES_ARMED" (SP1 Plan 4 K17)


@dataclass(frozen=True)
class AiPaperServices:
    config: Any
    policy: Any            # AiRiskPolicyService
    deployments: Any       # AiDeploymentStore
    decisions: Any         # AiPaperDecisionService
    decision_store: Any    # AiPaperDecisionStore (Plan 5 reads links_for_order_ref)
    actions: Any           # AiPaperActions: publish, register and the two reads
    entry_filter: Any


@dataclass(frozen=True)
class _AiPaperParts:
    """What the dispatch guard needs before the saga exists."""
    config: Any
    policy: Any
    entry_filter: Any


def _ai_paper_config(trader: Any, account_mode: str) -> Optional[Any]:
    """The enabled ai_paper config, or None. A live account refuses to build (spec 5.4)."""
    config = getattr(trader, "ai_paper_config", None)
    if config is None or not config.enabled:
        return None
    if account_mode != "paper":
        raise CommandStackConfigurationError(
            "AI_PAPER_LIVE_REFUSED", "ai_paper.enabled is refused on a live account",
        )
    return config


def _build_ai_paper_parts(trader: Any, config: Any, now: Callable[[], dt.datetime]) -> _AiPaperParts:
    from trader.automation.ai_paper_filter import AiEntryFilter
    from trader.automation.ai_risk_policy import AiRiskPolicyService
    from trader.automation.calendar_policy import XNYSCalendarPolicy

    policy = AiRiskPolicyService(
        db=trader.journal_db, account_id=trader.ib_account, ceiling=config.limits_ceiling,
        calendar=XNYSCalendarPolicy(), now=now,
    )
    return _AiPaperParts(config=config, policy=policy,
                         entry_filter=AiEntryFilter(universe=trader.universe_accessor))


def _ai_paper_guard_options(parts: Optional[_AiPaperParts]) -> dict:
    """R25: the AI gate, strict margin and the limits router for submit_ai_paper_decision only."""
    if parts is None:
        return {}
    from trader.automation.ai_paper_decision import AI_PAPER_ACTION
    from trader.automation.ai_paper_evidence import ai_entry_gate
    from trader.automation.risk_limits import PAPER_LIMITS

    def current_limits(request: Any):
        if getattr(request, "action", None) == AI_PAPER_ACTION:
            return parts.policy.effective_limits()
        return PAPER_LIMITS

    return {
        "current_limits": current_limits,
        "ai_entry_gate": ai_entry_gate(entry_filter=parts.entry_filter),
        "strict_margin_actions": frozenset({AI_PAPER_ACTION}),
    }


def _build_ai_entry_cutoff(parts: Optional[_AiPaperParts], *, broker: Any, cancel: Any, liquidation: Any,
                           account_id: str, now: Callable[[], dt.datetime]) -> Optional[Any]:
    """R26: AI entries are cancelled at the entry cutoff, and a partial fill is re-protected."""
    if parts is None:
        return None
    from trader.automation.ai_entry_cutoff import AiEntryCutoff
    return AiEntryCutoff(broker=broker, cancel=cancel, liquidation=liquidation, policy=parts.policy,
                         account_id=account_id, now=now)


def _build_ai_paper_services(
    trader: Any, parts: Optional[_AiPaperParts], *, ledger: CommandLedger, journal: Any,
    controls: TradingControlStore, broker: Any, quotes: Any, margin: Any, policy: CommandAuthorityPolicy,
    saga: Any, liquidation: Any, exit_owners: Any, account_mode: str, now: Callable[[], dt.datetime],
    schedule_reconcile: Callable[[str], None], experiments: Any = None,
) -> Optional[AiPaperServices]:
    if parts is None:
        return None
    from trader.automation.ai_deployments import AiDeploymentStore
    from trader.automation.ai_paper_actions import AiPaperActions
    from trader.automation.ai_paper_decision import AiPaperDecisionService, AiPaperDecisionStore
    from trader.automation.ai_paper_evidence import AI_ENTRY_POLICY, AiPaperEvidence
    from trader.automation.ai_paper_experiment import NoExperiment

    deployments = AiDeploymentStore(trader.journal_db, now=now)
    decision_store = AiPaperDecisionStore(journal)
    evidence = AiPaperEvidence(
        broker=broker, quotes=quotes, margin=margin, history=getattr(trader, "data", None),
        journal=journal, account_id=trader.ib_account, account_mode=account_mode, now=now,
        max_drift_bps=policy.max_drift_bps, entry_offset_bps=AI_ENTRY_POLICY.limit_offset_bps,
        entry_filter=parts.entry_filter,
    )
    decisions = AiPaperDecisionService(
        ledger=ledger, journal=journal, controls=controls, policy=parts.policy, deployments=deployments,
        evidence=evidence, saga=saga, experiments=experiments if experiments is not None else NoExperiment(),
        exit_owners=exit_owners,
        liquidation=liquidation, broker=broker, config=parts.config, account_id=trader.ib_account,
        now=now, schedule_reconcile=schedule_reconcile, decisions=decision_store,
    )
    actions = AiPaperActions(
        policy=parts.policy, deployments=deployments, broker=broker, config=parts.config,
        account_id=trader.ib_account, ledger=ledger, journal=journal, controls=controls, now=now,
    )
    return AiPaperServices(config=parts.config, policy=parts.policy, deployments=deployments,
                           decisions=decisions, decision_store=decision_store, actions=actions,
                           entry_filter=parts.entry_filter)


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
    return ingest_ready(trader.broker_ingest)


def _bundle_root_for(bundle_path: str) -> Path:
    """A bundle directory's parent is the root order dispatch resolves digests under."""
    root = Path(os.path.abspath(os.path.expanduser(bundle_path)))
    return root.parent if root.name.startswith(("artifact-", "sha256_")) else root


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
    broker: Any,
    quotes: Any,
    margin: Any,
    policy: CommandAuthorityPolicy,
    liquidation: Any = None,
    entry_block: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[Any]:
    """Build ``AutomatedIntentCommandService`` for paper automation only.

    Requires ``trader.automation_enabled`` and configured artifact path +
    public key ring + expected artifact id. Live automation stays refused.
    Returns ``None`` when dormant so ``execute_automated_intent`` is never
    registered.
    """
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
    from trader.automation.production_evidence import ProductionAutomationEvidence
    from trader.automation.paper_materials import require_qualified_research_evidence

    public_keys = _load_canary_public_keys(key_ring)
    if not public_keys:
        raise CommandStackConfigurationError(
            "AUTOMATION_KEY_RING_EMPTY",
            f"no usable *.pem verify keys under {key_ring!r}",
        )
    verifier = ArtifactVerifier(trusted_public_keys=public_keys)
    root = Path(os.path.abspath(os.path.expanduser(bundle_path)))
    evidence = ProductionAutomationEvidence(
        broker=broker, quotes=quotes, margin=margin,
        history=getattr(trader, "data", None), journal=journal,
        account_id=account_id, account_mode=account_mode,
        strategy_id=getattr(trader, "automation_strategy_name", None),
        max_drift_bps=policy.max_drift_bps, now=now,
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
        bundle_root=_bundle_root_for(bundle_path),
        configured_bundle_path=root,
        expected_artifact_id=expected_id,
        bundle_evidence_validator=require_qualified_research_evidence,
        schedule_reconcile=schedule_reconcile,
        protective_saga=protective_order_saga,
        approval_factory=evidence.approval_factory,
        session_state_factory=evidence.session_state_factory,
        allocation_factory=evidence.allocation_factory,
        liquidation=liquidation,
        broker=broker,
        entry_block=entry_block,
    )


def _load_position_sizer() -> Any:
    """Load ``PositionSizer`` from ``~/.config/mmr/position_sizing.yaml``.

    Required for dashboard/CLI proposals that leave quantity and amount blank
    (auto-size from confidence). Without this, ``ProposalCommandService``
    raises ``SIZING_BLOCKED: no position sizer is configured``.
    """
    from trader.trading.position_sizing import PositionSizingConfig, PositionSizer

    return PositionSizer(PositionSizingConfig.load())


@dataclass(frozen=True)
class _ExperimentParts:
    """What exists before the stack: the store and the arming lock both directions share."""
    store: Any
    arming_lock: Any
    experiment_lock: Any


def _build_experiment_parts(trader: Any, account_mode: str,
                            now: Callable[[], dt.datetime]) -> Optional[_ExperimentParts]:
    """K15: experiments exist on every paper stack, ai_paper enabled or not; never on live."""
    if account_mode != "paper":
        return None
    from trader.automation.experiment_service import ArmingLock, ExperimentLock
    from trader.automation.experiments import ExperimentStore

    store = ExperimentStore(trader.journal_db, trader.ib_account, now)
    lock = ArmingLock()
    return _ExperimentParts(store=store, arming_lock=lock, experiment_lock=ExperimentLock(store, lock))


@dataclass(frozen=True)
class ExperimentServices:
    store: Any       # ExperimentStore
    service: Any     # ExperimentService: start / pause / resume / stop
    monitor: Any     # KillLineMonitor
    reader: Any      # ExperimentStateReader (Plan 3's ExperimentStatePort)
    lock: Any        # ArmingLock shared with the one-strategy activation
    config_path: Any = None  # trader.yaml, re-read only to show a pending kill-line edit (K9)


def _build_experiment_services(
    trader: Any, parts: Optional[_ExperimentParts], *, journal: Any, broker: Any, liquidation: Any,
    liquidation_store: Any, exit_owners: Any, session_controller: Any, breaker_store: Any,
    resume_ready: Callable[[], bool], reconciliation_safe: Callable[[], bool],
    reconciliation_complete: Callable[[str], bool], late: dict,
    mode_conflict: Callable[[], Optional[str]], account_mode: str, now: Callable[[], dt.datetime],
) -> Optional[ExperimentServices]:
    """K15: built on every paper stack, also with ``ai_paper.enabled: false``."""
    if parts is None:
        return None
    from trader.automation.ai_paper_config import AiPaperConfig
    from trader.automation.ai_paper_experiment import ExperimentStateReader
    from trader.automation.experiment_service import ArmingPorts, ExperimentService
    from trader.automation.kill_monitor import KillLineMonitor
    from trader.messaging.trader_service_api import TraderServiceApi

    config = getattr(trader, "ai_paper_config", None) or AiPaperConfig()
    monitor = KillLineMonitor(
        store=parts.store, broker=broker, session=session_controller, liquidation=liquidation, config=config,
        account_id=trader.ib_account, now=now, journal=journal, reconciliation_safe=reconciliation_safe)
    ports = ArmingPorts(
        broker=broker,
        account_cash=TraderServiceApi(trader).get_account_cash_by_currency,
        resume_ready=resume_ready,
        reconciliation_safe=lambda exclude: (reconciliation_safe() if exclude is None
                                             else reconciliation_complete(exclude)),
        breaker_clear=lambda: breaker_store.get().state == "CLEAR",
        exit_owners=exit_owners,
        liquidation_roots=lambda: liquidation_store.transaction(liquidation_store.roots_to_advance_in_tx),
        old_path_armed=lambda: _one_strategy_armed(late["paper_automation"]),
        ai_paper_built=lambda: late["ai_paper"] is not None,
    )
    service = ExperimentService(store=parts.store, ports=ports, lock=parts.arming_lock, config=config,
                                account_id=trader.ib_account, account_mode=account_mode, now=now)
    reader = ExperimentStateReader(parts.store, monitor, mode_conflict=mode_conflict)
    return ExperimentServices(store=parts.store, service=service, monitor=monitor, reader=reader,
                              lock=parts.arming_lock, config_path=_trader_yaml_path())


def _trader_yaml_path() -> Path:
    return Path(os.environ.get("TRADER_CONFIG", "~/.config/mmr/trader.yaml")).expanduser()


def _experiment_gate(reader: Any, account_id: str) -> Callable[[Any], Optional[str]]:
    """K20: only ai_paper entries reach the gate; the one-strategy path is untouched."""
    from trader.automation.ai_paper_decision import AI_PAPER_ACTION
    from trader.automation.ai_paper_experiment import experiment_entry_refusal

    def gate(request: Any) -> Optional[str]:
        if getattr(request, "action", None) != AI_PAPER_ACTION:
            return None
        return experiment_entry_refusal(reader, account_id)
    return gate


def _log_active_kill_line(experiments: ExperimentServices) -> None:
    """K9: the kill line the running process enforces; an edit to trader.yaml waits for a restart."""
    from trader.automation.kill_line import effective_kill_line
    try:
        record = experiments.store.active()
    except Exception:
        logger.exception("experiment state unreadable at startup")
        return
    if record is None:
        return
    line = effective_kill_line(record, experiments.service.config)
    if line is None:
        logger.warning("experiment %s: no kill line is active", record.experiment_id)
    else:
        logger.warning("experiment kill line active: %s%% (%s)", line.pct, line.basis)
    configured_basis = getattr(experiments.service.config, "experiment_kill_basis", record.kill_basis)
    if configured_basis != record.kill_basis:
        logger.warning("experiment %s keeps its frozen kill basis %s; the configured basis %s applies to "
                       "the next experiment", record.experiment_id, record.kill_basis, configured_basis)


def _one_strategy_armed(paper_automation_service: Any) -> Optional[str]:
    """Armed means any lifecycle but ``disabled`` (configured alone is not armed, K17)."""
    lifecycle = paper_automation_service.status().lifecycle
    return None if lifecycle == "disabled" else "ONE_STRATEGY_ARMED"


def _detect_mode_conflict(parts: Optional[_ExperimentParts], paper_automation_service: Any, *, journal: Any,
                          account_id: str, now: Callable[[], dt.datetime]) -> Optional[str]:
    """K17: both modes armed. The trader still starts; every entry on both paths is refused."""
    if parts is None:
        return None
    record = parts.store.active()
    if record is None:
        return None
    try:
        armed = _one_strategy_armed(paper_automation_service)
    except Exception:
        logger.exception("one-strategy automation state unreadable; treated as armed (fail closed)")
        armed = "ONE_STRATEGY_STATE_UNREADABLE"
    if armed is None:
        return None
    detail = (f"BOTH_MODES_ARMED: the one-strategy automation is armed and experiment "
              f"{record.experiment_id} is {record.state}. Every entry is refused; exits keep working. "
              "Deactivate the one-strategy automation, or stop the experiment once it is flat.")
    logger.error(detail)
    try:
        from trader.automation.kill_monitor import record_incident
        at = now()
        record_incident(journal, kind="automation.mode_conflict", account_id=account_id, detail=detail,
                        event_id=f"automation.mode_conflict:{account_id}:{at.isoformat()}", now=at)
    except Exception:
        logger.exception("automation.mode_conflict incident not written")
    return "BOTH_MODES_ARMED"


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
    apply_liquidation_migration(migrator)          # also applies 35 (exit owners) before 36
    exit_owner_registry = ExitOwnerRegistry(trader.journal_db)
    liquidation_store = LiquidationRunStore(trader.journal_db)
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
    from trader.automation.ai_deployments import apply_ai_deployment_migration
    from trader.automation.ai_paper_decision import apply_ai_paper_decision_migration
    from trader.automation.ai_risk_policy import apply_ai_risk_policy_migration

    apply_ai_risk_policy_migration(migrator)          # 54
    apply_ai_deployment_migration(migrator)           # 55
    apply_ai_paper_decision_migration(migrator)       # 56
    from trader.automation.experiments import apply_experiment_migration

    apply_experiment_migration(migrator)              # 70 (SP1 Plan 4)

    repository = ProposalRepository(journal)
    ledger = CommandLedger(journal)
    controls = TradingControlStore(journal)
    account_mode = "paper" if trader.paper_trading else "live"
    trader.journal_db.transaction(
        lambda conn: controls.seed_in_tx(conn, [(trader.ib_account, account_mode)], now())
    )
    experiment_parts = _build_experiment_parts(trader, account_mode, now)
    mode_conflict_slot: dict[str, Optional[str]] = {"code": None}
    # Late-bound: the gate needs the kill monitor, which needs the session controller built below.
    experiment_gate_slot: dict[str, Callable[[Any], Optional[str]]] = {"gate": lambda request: None}
    late: dict[str, Any] = {"paper_automation": None, "ai_paper": None}
    ai_paper_config = _ai_paper_config(trader, account_mode)
    ai_paper_parts = (None if ai_paper_config is None
                      else _build_ai_paper_parts(trader, ai_paper_config, now))
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
            allocation_authority_store.authority_for_dispatch(account_id, artifact_digest)
        ),
        **_ai_paper_guard_options(ai_paper_parts),
        experiment_gate=lambda request: experiment_gate_slot["gate"](request),
    )

    def compute_risk_projection():
        snapshot = broker_snapshot.capture(trader.ib_account)
        from trader.trading.portfolio_risk import enrich_risk_projection

        return enrich_risk_projection(
            snapshot,
            duckdb_path=trader.duckdb_path,
            history_duckdb_path=getattr(trader, 'history_duckdb_path', '') or '',
        )

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
    strategy_port = None
    if _strategy_control_credentials_ready(trader):
        strategy_port = _LazyTypedStrategyControlPort(trader)
    strategy = strategy_port if strategy_port is not None else _UnavailableStrategyControl()
    reconciler = OutcomeReconciler(
        journal=journal,
        ledger=ledger,
        orders=dispatch,
        strategy=strategy,
        alerts=alerts,
        repo=repository,
        orders_view=orders_view,
        now=now,
        closes=liquidation_store,
    )
    strategy_control_service = None
    if strategy_port is not None:
        from trader.trading.command_coordinator import StrategyControlCommandService

        strategy_control_service = StrategyControlCommandService(
            journal=journal,
            ledger=ledger,
            port=strategy_port,
            snapshot=_JournalOrLiveStrategySnapshot(journal, strategy_port),
            reconciler=reconciler,
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
    liquidation_worker = LiquidationWorker()
    liquidation_service = SerializedLiquidation(
        LiquidationService(
            broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
            store=liquidation_store, registry=exit_owner_registry, now=now,
            breaker=_LiquidationBreaker(circuit_breaker, now),
            journal=journal, ledger=ledger,
            schedule_reconcile=lambda command_id: reconciler.schedule(command_id, now()),
            refresh=_BrokerGenerationRefresh(trader),
        ),
        liquidation_worker, account_id=trader.ib_account, now=now,
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
        sizer=_load_position_sizer(),
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
        paper_max_quote_age_seconds=policy.max_paper_quote_age_seconds,
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
        # The ingest thread reports protection failures; it must queue the flatten, not wait (R12).
        liquidation=liquidation_service.nonblocking(),
        account_id=trader.ib_account,
        account_mode=account_mode,
        now=now,
        db=trader.journal_db,
        orphan_evidence=BrokerStateOrphanEvidence(
            db=trader.journal_db, store=trader.broker_state_store, snapshots=broker_snapshot,
        ),
    )
    # The saga is the protection port of every close and the source of unhandled failures.
    liquidation_service.attach_protection(protective_order_saga)
    # P3 Task 6 — exchange-aware session deadlines / flatten scheduler.
    from trader.automation.session_controller import (
        SessionCancelAdapter,
        SessionController,
        SessionTimeExitAdapter,
        apply_session_controller_migration,
    )
    apply_session_controller_migration(migrator)
    ai_entry_cutoff = _build_ai_entry_cutoff(
        ai_paper_parts, broker=broker_snapshot, cancel=_LiquidationDispatch(dispatch, orders_view),
        liquidation=liquidation_service, account_id=trader.ib_account, now=now,
    )
    session_controller = SessionController(
        journal=journal,
        db=trader.journal_db,
        calendar=XNYSCalendarPolicy(),
        broker=broker_snapshot,
        cancel=SessionCancelAdapter(_LiquidationDispatch(dispatch, orders_view)),
        liquidation=liquidation_service,
        breaker=circuit_breaker,
        time_exit=SessionTimeExitAdapter(liquidation_service, account_id=trader.ib_account, now=now),
        account_id=trader.ib_account,
        now=now,
        on_entry_cutoff=None if ai_entry_cutoff is None else ai_entry_cutoff.on_entry_cutoff,
    )
    experiments = _build_experiment_services(
        trader, experiment_parts, journal=journal, broker=broker_snapshot, liquidation=liquidation_service,
        liquidation_store=liquidation_store, exit_owners=exit_owner_registry, session_controller=session_controller,
        breaker_store=breaker_store, resume_ready=resume_ready, reconciliation_safe=reconciliation_safe,
        reconciliation_complete=reconciliation_complete, late=late, mode_conflict=lambda: mode_conflict_slot["code"], account_mode=account_mode, now=now)
    if experiments is not None:
        experiment_gate_slot["gate"] = _experiment_gate(experiments.reader, trader.ib_account)
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
        broker=broker_snapshot, quotes=quotes, margin=margin, policy=policy,
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
        liquidation=liquidation_service,
        entry_block=lambda: mode_conflict_slot["code"],
    )
    ai_paper = _build_ai_paper_services(
        trader, ai_paper_parts, ledger=ledger, journal=journal, controls=controls,
        broker=broker_snapshot, quotes=quotes, margin=margin, policy=policy,
        saga=protective_order_saga, liquidation=liquidation_service, exit_owners=exit_owner_registry,
        account_mode=account_mode, now=now,
        schedule_reconcile=lambda command_id: reconciler.schedule(command_id, now()),
        experiments=None if experiments is None else experiments.reader,
    )
    late["ai_paper"] = ai_paper
    from trader.automation.paper_activation import PaperAutomationActivationService
    from trader.automation.paper_hot_arm import ProductionPaperHotArmPorts

    trader_yaml_path = _trader_yaml_path()
    strategy_yaml_path = Path(
        getattr(trader, "strategy_config_file", None)
        or "~/.config/mmr/strategy_runtime.yaml"
    ).expanduser()

    paper_automation_service = PaperAutomationActivationService(
        trader_yaml_path=trader_yaml_path,
        strategy_yaml_path=strategy_yaml_path,
        config_dir=Path("~/.config/mmr").expanduser(),
        share_dir=Path("~/.local/share/mmr").expanduser(),
        account_mode=account_mode,
        command_authority_enabled=policy.enabled,
        now=now,
        hot_arm=None,
        experiment_lock=None if experiment_parts is None else experiment_parts.experiment_lock,
    )

    late["paper_automation"] = paper_automation_service
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
        liquidation_worker=liquidation_worker,
        exit_owner_registry=exit_owner_registry,
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
        paper_automation_service=paper_automation_service,
        strategy_control_service=strategy_control_service,
        ai_paper=ai_paper,
        experiments=experiments,
    )

    def _build_intent_for_hot_arm(trader_obj: Any):
        return _build_automated_intent_service(
            trader_obj,
            broker=broker_snapshot, quotes=quotes, margin=margin, policy=policy,
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
            liquidation=liquidation_service,
            entry_block=lambda: mode_conflict_slot["code"],
        )

    if account_mode == "paper":
        hot_arm = ProductionPaperHotArmPorts(
            trader=trader,
            stack=stack,
            account_id=trader.ib_account,
            account_mode=account_mode,
            now=now,
            build_intent_service=_build_intent_for_hot_arm,
            experiment_lock=None if experiment_parts is None else experiment_parts.experiment_lock,
        )
        paper_automation_service._hot_arm = hot_arm
        object.__setattr__(stack, "paper_hot_arm", hot_arm)

    if automated_intent_service is not None:
        paper_automation_service.mark_runtime_armed(
            strategy_name=str(
                getattr(trader, "automation_strategy_name", "") or ""
            ),
            artifact_id=str(
                getattr(trader, "automation_expected_artifact_id", "") or ""
            ),
            artifact_bundle_path=str(
                getattr(trader, "automation_artifact_bundle_path", "") or ""
            ),
            public_key_ring_path=str(
                getattr(trader, "automation_public_key_ring_path", "") or ""
            ),
        )

    mode_conflict = _detect_mode_conflict(
        experiment_parts, paper_automation_service, journal=journal, account_id=trader.ib_account, now=now)
    mode_conflict_slot["code"] = mode_conflict
    object.__setattr__(stack, "mode_conflict", mode_conflict)

    trader.command_ledger = ledger
    trader.command_reconciler = reconciler
    trader.command_stack = stack
    trader.trading_control_store = controls
    trader.automation_circuit_breaker = circuit_breaker
    trader.semantic_readiness = semantic_readiness
    trader.liquidation_service = liquidation_service
    trader.liquidation_worker = liquidation_worker
    trader.exit_owner_registry = exit_owner_registry
    trader.session_risk = session_risk
    trader.protective_order_saga = protective_order_saga
    trader.session_controller = session_controller
    trader.attribution_ledger = attribution_ledger
    if ai_paper is not None:
        trader.ai_paper_attribution = ai_paper.decision_store  # Plan 5 reads links_for_order_ref here
    if experiments is not None:
        trader.experiment_store = experiments.store            # Plan 5's A1 reader
        trader.kill_line_monitor = experiments.monitor
        _log_active_kill_line(experiments)
    return stack
