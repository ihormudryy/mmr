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
    TraderPositionAuthority,
    TraderQuoteAuthority,
)
from trader.trading.preflight_nonce import (
    PreflightNonceGate,
    apply_preflight_nonce_migration,
)
from trader.trading.proposal_command_service import ProposalCommandService
from trader.trading.risk_producer import RiskProducer
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
    if not getattr(trader, "paper_trading", False):
        raise CommandStackConfigurationError(
            "LIVE_GUARDS_INCOMPLETE",
            "live command authority remains disabled until P1 Task 4 adds "
            "trader-owned notional and immediate dispatch revalidation",
        )
    _require_trader_ports(trader)

    journal = trader.domain_journal
    migrator = SchemaMigrator(trader.journal_db)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)
    apply_preflight_nonce_migration(migrator)

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
    broker = TraderBrokerAuthority(
        trader, run_coro=run_coro, resolve_contract=resolve_contract,
    )

    risk_producer = RiskProducer(
        trader.journal_db,
        journal,
        trader.ib_account,
        compute_projection=lambda: {
            "net_liquidation": broker.net_liquidation(),
            "daily_pnl": broker.daily_pnl(),
            "open_order_count": broker.open_order_count(),
        },
        clock=now,
    )
    risk_producer.migrate(migrator)

    from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

    dispatch = TradingRuntimeOrderDispatch(trader)
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
        positions=positions,
        quotes=quotes,
        risk_gate=trader.risk_gate,
        risk_producer=risk_producer,
        reconciler=reconciler,
        broker=broker,
        account_id=trader.ib_account,
        account_mode=account_mode,
        now=now,
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
    )
    trader.command_ledger = ledger
    trader.command_reconciler = reconciler
    trader.command_stack = stack
    trader.trading_control_store = controls
    return stack
