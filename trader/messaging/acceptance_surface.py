"""Acceptance reads over typed RPC (SP1 Plan 6 Task 2, rulings 3, 4 and 14).

``get_acceptance_preflight`` is the clean-account gate's input: trader-owned
state only; a capture failure is returned as ``{"capture_error": code}``, never
raised. ``get_broker_order_evidence`` is the generation-fenced order read with
the OCA fields and the recorded status events (``get_open_orders`` and
``mmr orders`` drop the OCA columns and stay as they are).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class GetAcceptancePreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetBrokerOrderEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    conid: Optional[int] = None


def _code(exc: Exception) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)


def evidence_source(trader: Any) -> str:
    """``ib`` only when trader_service set it after connecting to a real IB; a composed test stack never does."""
    return "ib" if getattr(trader, "broker_evidence_source", None) == "ib" else "synthetic"


def broker_order_evidence(trader: Any, conid: Optional[int]) -> Dict[str, Any]:
    from trader.data.broker_order_events import broker_order_evidence_in_tx
    store, account_id = trader.broker_state_store, trader.ib_account
    return trader.journal_db.transaction(
        lambda conn: broker_order_evidence_in_tx(conn, store, account_id, conid, source=evidence_source(trader)))


def acceptance_preflight(trader: Any, command_stack: Any) -> Dict[str, Any]:
    from trader.automation.experiment_service import start_fx_from_cash

    experiments = command_stack.experiments
    ports = experiments.service.ports
    account_id = trader.ib_account
    try:
        snapshot = ports.broker.capture(account_id)
    except Exception as exc:
        return {"capture_error": _code(exc)}
    try:
        account = trader.journal_db.transaction(
            lambda conn: trader.broker_state_store.get_account_in_tx(conn, account_id))
        owner = command_stack.exit_owner_registry.account_owner(account_id)
        roots = list(ports.liquidation_roots())
        unresolved = [row.command_id for row in command_stack.ledger.unresolved_for_account(account_id)]
        breaker = command_stack.circuit_breaker.store.get().state
        record = experiments.store.active()
    except Exception as exc:
        logger.exception("acceptance preflight: trader state unreadable")
        return {"capture_error": _code(exc)}
    try:
        fx = start_fx_from_cash(ports.account_cash())
        base_currency, usd_per_base = fx.base_currency, fx.usd_per_base
    except Exception:
        base_currency, usd_per_base = None, None
    return {
        "account_id": snapshot.account_id, "account_mode": snapshot.account_mode,
        "generation_id": snapshot.generation_id, "net_liquidation": snapshot.net_liquidation,
        "nlv_as_of": None if account is None else account.source_timestamp.isoformat(),
        "daily_pnl": snapshot.daily_pnl, "base_currency": base_currency, "usd_per_base": usd_per_base,
        "positions": [{"conid": int(p.conid), "quantity": float(p.quantity)}
                      for p in snapshot.positions if p.quantity != 0],
        "working_orders": [{"order_entity_id": o.order_entity_id, "conid": int(o.conid), "leg": o.leg,
                            "is_external": bool(o.is_external), "status": o.status}
                           for o in snapshot.working_orders],
        "unresolved_commands": unresolved,
        "open_liquidation_roots": [str(getattr(root, "root_id", root)) for root in roots],
        "exit_owner": None if owner is None else str(getattr(owner, "root_id", owner)),
        "breaker_tripped": breaker != "CLEAR",
        "experiment_state": None if record is None else record.state,
    }


def register_acceptance_surface(registry: Any, trader: Any, command_stack: Any) -> None:
    """Only on a paper stack with experiment services (the gate is for the AI paper experiment)."""
    if command_stack is None or getattr(command_stack, "experiments", None) is None:
        return

    def get_acceptance_preflight(_parsed: GetAcceptancePreflightRequest) -> Dict[str, Any]:
        return acceptance_preflight(trader, command_stack)

    def get_broker_order_evidence(parsed: GetBrokerOrderEvidenceRequest) -> Dict[str, Any]:
        return broker_order_evidence(trader, parsed.conid)

    registry.register("query", "get_acceptance_preflight", GetAcceptancePreflightRequest, dict,
                      get_acceptance_preflight)
    registry.register("query", "get_broker_order_evidence", GetBrokerOrderEvidenceRequest, dict,
                      get_broker_order_evidence)
