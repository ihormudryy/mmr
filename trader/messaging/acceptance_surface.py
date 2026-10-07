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

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


class GetAcceptancePreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetBrokerOrderEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    conid: Optional[int] = None


_EXPERIMENT_ID = re.compile(r"^exp-[0-9a-f]{20}$")
_RUN_ID = re.compile(r"^acc-[0-9]{8}-[0-9a-f]{6}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class AcceptanceMarkStartRequest(BaseModel):
    """acceptance_mark_start (ruling 23): the four fields of the durable mark."""
    model_config = ConfigDict(extra="forbid", strict=True)

    command_id: str
    experiment_id: str
    run_id: str
    conid: int = Field(gt=0)
    decision_id: str

    @field_validator("command_id", "decision_id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not _COMMAND_ID.fullmatch(value):
            raise ValueError("ids are 8-80 characters of letters, digits, '-' and '_'")
        return value

    @field_validator("experiment_id")
    @classmethod
    def _experiment_shape(cls, value: str) -> str:
        if not _EXPERIMENT_ID.fullmatch(value):
            raise ValueError("experiment_id must match exp-<20 hex>")
        return value

    @field_validator("run_id")
    @classmethod
    def _run_shape(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id must match acc-YYYYMMDD-<6 hex>")
        return value


class AcceptanceShrinkProbeRequest(AcceptanceMarkStartRequest):
    display_size: int


def _code(exc: Exception) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)


def broker_order_evidence(trader: Any, conid: Optional[int]) -> Dict[str, Any]:
    from trader.trading.acceptance_probe import read_broker_order_evidence
    return read_broker_order_evidence(trader, conid)


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
        "acceptance_probe": getattr(getattr(trader, "ai_paper_config", None), "acceptance_probe", False) is True,
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
    probe = getattr(command_stack, "acceptance_probe", None)
    if probe is not None:
        _register_probe_commands(registry, command_stack.coordinator, probe, getattr(trader, "ib_account", None))


def _register_probe_commands(registry: Any, coordinator: Any, probe: Any, account_id: Optional[str]) -> None:
    """Ruling 23: operator-only commands through the coordinator (ledger, idempotency, receipts)."""
    from trader.trading.acceptance_probe import MARK_ACTION, PROBE_ACTION

    coordinator.register_action(MARK_ACTION, probe.mark_start, requires_preflight=False)
    coordinator.register_action(PROBE_ACTION, probe.probe, requires_preflight=False)

    def handler(action: str):
        def _handle(parsed: AcceptanceMarkStartRequest, caller: Any) -> Dict[str, Any]:
            from trader.messaging.production_api import _receipt_to_dict
            from trader.trading.command_coordinator import CommandRequest
            body = parsed.model_dump()
            command_id = body.pop("command_id")
            request = CommandRequest(
                command_id=command_id, action=action, account_id=account_id, target_type="experiment",
                target_id=parsed.experiment_id, expected_version=None, body=body,
                source=caller.principal, principal=caller.principal)
            return _receipt_to_dict(coordinator.execute(request))
        return _handle

    registry.register("command", MARK_ACTION, AcceptanceMarkStartRequest, dict, handler(MARK_ACTION),
                      with_caller=True)
    registry.register("command", PROBE_ACTION, AcceptanceShrinkProbeRequest, dict, handler(PROBE_ACTION),
                      with_caller=True)
