"""Trader-owned command-authority policy + capability matrix.

Design: docs/superpowers/specs/2026-07-18-command-plane-activation-design.md
(constraints C2 and C4; sequence step 1).

The dashboard's ``DASHBOARD_COMMANDS_ENABLED`` flag is a UI kill switch — it must
never be the enforcement source. Risk-increasing commands are gated by THIS
policy, owned by the trader (``command_authority:`` in trader.yaml) and validated
fail-closed at startup: a contradictory policy (live enforcement without a
matching account, a live authority with no notional ceiling, limits that bound
nothing) refuses to start rather than silently under-enforcing.

The capability matrix maps each command to the adapter ports it needs, so the
command manifest is DERIVED from the trader's actually-constructed dependencies
rather than a static desired list — commands whose ports are still stubs (order
cancel, order-state lookup; see TradingRuntimeOrderDispatch) are never
advertised or registered.

This module is pure (no IB, no ZMQ, no DuckDB): just the policy value object,
its validation, and the matrix. Later phases construct the ports and call
``available_commands`` to decide what to register.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

_TRUE_STRINGS = ("1", "true", "yes", "on")
_FALSE_STRINGS = ("0", "false", "no", "off")

_KNOWN_KEYS = frozenset(
    {"enabled", "live_enabled", "live_account_id", "max_order_notional", "max_drift_bps"})

DEFAULT_MAX_DRIFT_BPS = 50.0


class CommandPolicyError(ValueError):
    """A command-authority policy that is malformed or internally contradictory.

    Raised at parse/validate time so a misconfiguration fails the trader loudly
    at startup instead of silently under-enforcing at command time.
    """


def _as_bool(value: object, key: str) -> bool:
    """Parse a strict boolean. Accepts real bools and the canonical string
    forms (mirrors trader.config's env-var hardening); anything else raises,
    so ``enabled: "maybe"`` can't quietly become True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_STRINGS:
            return True
        if low in _FALSE_STRINGS:
            return False
    raise CommandPolicyError(
        f"command_authority.{key} must be a boolean, got {value!r}")


def _as_optional_float(value: object, key: str) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise CommandPolicyError(
            f"command_authority.{key} must be a number, got {value!r}")


@dataclass(frozen=True)
class CommandAuthorityPolicy:
    """Trader-owned command-authority policy.

    - ``enabled``: master switch — build/register the authority at all.
    - ``live_enabled``: allow risk-increasing commands on a LIVE account.
    - ``live_account_id``: must equal the trader's configured IB account.
    - ``max_order_notional``: hard ceiling on a single order's notional.
    - ``max_drift_bps``: server-side price-drift ceiling (caller values clamp to
      it; ignored entirely in live mode per the design).
    """

    enabled: bool = False
    live_enabled: bool = False
    live_account_id: Optional[str] = None
    max_order_notional: Optional[float] = None
    max_drift_bps: float = DEFAULT_MAX_DRIFT_BPS

    @staticmethod
    def from_config(raw: Optional[dict]) -> "CommandAuthorityPolicy":
        raw = raw or {}
        if not isinstance(raw, dict):
            raise CommandPolicyError(
                f"command_authority config must be a mapping, got {type(raw).__name__}")
        unknown = set(raw) - _KNOWN_KEYS
        if unknown:
            raise CommandPolicyError(
                f"unknown command_authority keys: {sorted(unknown)} "
                f"(known: {sorted(_KNOWN_KEYS)})")
        account = raw.get("live_account_id")
        return CommandAuthorityPolicy(
            enabled=_as_bool(raw.get("enabled", False), "enabled"),
            live_enabled=_as_bool(raw.get("live_enabled", False), "live_enabled"),
            live_account_id=(str(account) if account else None),
            max_order_notional=_as_optional_float(
                raw.get("max_order_notional"), "max_order_notional"),
            max_drift_bps=_as_optional_float(
                raw.get("max_drift_bps", DEFAULT_MAX_DRIFT_BPS), "max_drift_bps"),
        )


def validate_command_policy(
    policy: CommandAuthorityPolicy, *, trader_account_id: Optional[str],
    paper_trading: bool,
) -> None:
    """Fail-closed startup validation. Raises ``CommandPolicyError`` on any
    contradiction. A disabled policy is always valid (nothing is registered)."""
    if not policy.enabled:
        return

    if policy.max_drift_bps is None or not math.isfinite(policy.max_drift_bps) \
            or policy.max_drift_bps <= 0:
        raise CommandPolicyError(
            f"command_authority.max_drift_bps must be finite and > 0, "
            f"got {policy.max_drift_bps!r}")

    if policy.max_order_notional is not None and (
            not math.isfinite(policy.max_order_notional)
            or policy.max_order_notional <= 0):
        raise CommandPolicyError(
            f"command_authority.max_order_notional must be finite and > 0, "
            f"got {policy.max_order_notional!r}")

    if policy.live_enabled:
        if paper_trading:
            raise CommandPolicyError(
                "command_authority.live_enabled=true requires a live trader "
                "account, but the trader is running paper")
        if not policy.live_account_id:
            raise CommandPolicyError(
                "command_authority.live_enabled=true requires live_account_id")
        if policy.live_account_id != trader_account_id:
            raise CommandPolicyError(
                f"command_authority.live_account_id {policy.live_account_id!r} "
                f"does not match the trader account {trader_account_id!r}")
        if policy.max_order_notional is None:
            raise CommandPolicyError(
                "command_authority.live_enabled=true requires max_order_notional "
                "(a live authority must bound single-order notional)")


def load_and_validate_command_policy(
    raw: Optional[dict], *, trader_account_id: Optional[str], paper_trading: bool,
) -> CommandAuthorityPolicy:
    """Parse + validate in one step — the startup entry point."""
    policy = CommandAuthorityPolicy.from_config(raw)
    validate_command_policy(
        policy, trader_account_id=trader_account_id, paper_trading=paper_trading)
    return policy


# --- Capability matrix (design C4) -----------------------------------------
# Adapter ports the command authority depends on. A command is "available" only
# when EVERY port it requires is ready (constructed + non-stub). Registration is
# driven off actual dependencies via available_commands(), never a static list.
PORT_ORDER_SUBMIT = "order_submit"
PORT_ORDER_CANCEL = "order_cancel"
PORT_ORDER_STATE = "order_state"      # find_by_order_ref: approval reconciliation
PORT_QUOTES = "quotes"
PORT_POSITIONS = "positions"
PORT_BROKER = "broker"
PORT_RISK_GATE = "risk_gate"
PORT_STRATEGY = "strategy"
PORT_ALERTS = "alerts"
PORT_CONTROLS = "controls"
PORT_NONCES = "nonces"

COMMAND_PORT_REQUIREMENTS: dict[str, frozenset[str]] = {
    "preflight_command": frozenset({PORT_NONCES}),
    "create_proposal": frozenset({PORT_QUOTES, PORT_POSITIONS, PORT_RISK_GATE}),
    "approve_proposal": frozenset({
        PORT_ORDER_SUBMIT, PORT_ORDER_STATE, PORT_QUOTES, PORT_POSITIONS,
        PORT_BROKER, PORT_RISK_GATE, PORT_ALERTS, PORT_NONCES}),
    "reject_proposal": frozenset(),
    "cancel_order": frozenset({PORT_ORDER_CANCEL, PORT_ORDER_STATE}),
    "cancel_all": frozenset({PORT_ORDER_CANCEL, PORT_ORDER_STATE}),
    "pause_trading": frozenset({PORT_CONTROLS}),
    "resume_trading": frozenset({PORT_CONTROLS, PORT_NONCES}),
    "enable_strategy": frozenset({PORT_STRATEGY}),
    "disable_strategy": frozenset({PORT_STRATEGY}),
    "update_strategy_params": frozenset({PORT_STRATEGY, PORT_NONCES}),
}

ALL_PORTS: frozenset[str] = frozenset().union(*COMMAND_PORT_REQUIREMENTS.values())

# Every command-authority port now has a production adapter: order dispatch
# (submit, step 2b/M1-F3), cancel + order-state (step 2c), quotes / positions /
# broker (step 2b, command_ports.py), strategy control (existing), the preflight
# nonce gate (step 5), and the critical-alert adapter (command_alerts.py). None
# is a stub. Adapter EXISTENCE is separate from being CONSTRUCTED at startup --
# available_commands() is driven by the ports the trader actually builds in the
# wiring phase, not this set.
KNOWN_STUB_PORTS: frozenset[str] = frozenset()


def available_commands(ready_ports: Iterable[str]) -> tuple[str, ...]:
    """Commands whose every required port is ready — the manifest derivation
    (design C4). ``ready_ports`` is the set of adapter ports the trader actually
    constructed as real (non-stub) at startup."""
    ready = frozenset(ready_ports)
    return tuple(sorted(
        cmd for cmd, req in COMMAND_PORT_REQUIREMENTS.items() if req <= ready))
