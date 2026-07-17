"""[M1-C] Dashboard command feature flags (spec Section 10).

Both flags default false. Live commands have no permissive fallback: they
require paper commands to already be enabled, an exact account id (no
wildcards, no empties, no mode-label placeholders), and a finite positive
maximum order notional. Any inconsistent configuration raises
``CommandFlagsError`` so a bad config kills the process at startup instead
of silently running with commands partially enabled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional


class CommandFlagsError(ValueError):
    """Inconsistent dashboard command configuration; startup must fail."""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_WILDCARD_CHARS = set("*?%")
_RESERVED_ACCOUNT_LABELS = {"live", "paper"}


def _boolean(env: Mapping[str, str], name: str) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise CommandFlagsError(f"{name} must be a boolean flag, got {raw!r}")


@dataclass(frozen=True)
class CommandFlags:
    commands_enabled: bool
    live_commands_enabled: bool
    live_account_id: Optional[str]
    live_max_order_notional: Optional[float]


def load_command_flags(env: Mapping[str, str]) -> CommandFlags:
    commands = _boolean(env, "DASHBOARD_COMMANDS_ENABLED")
    live = _boolean(env, "DASHBOARD_LIVE_COMMANDS_ENABLED")

    if live and not commands:
        raise CommandFlagsError(
            "DASHBOARD_LIVE_COMMANDS_ENABLED requires DASHBOARD_COMMANDS_ENABLED "
            "to also be enabled -- live commands cannot be enabled on their own"
        )

    account_raw = str(env.get("DASHBOARD_LIVE_ACCOUNT_ID", "")).strip()
    account: Optional[str] = account_raw or None

    raw_notional = str(env.get("DASHBOARD_LIVE_MAX_ORDER_NOTIONAL", "")).strip()
    notional: Optional[float] = None
    if raw_notional:
        try:
            notional = float(raw_notional)
        except ValueError as exc:
            raise CommandFlagsError(
                f"DASHBOARD_LIVE_MAX_ORDER_NOTIONAL must be a number, got {raw_notional!r}"
            ) from exc
        if not math.isfinite(notional) or notional <= 0:
            raise CommandFlagsError(
                "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL must be finite and positive, "
                f"got {raw_notional!r}"
            )

    if live:
        if (account is None
                or _WILDCARD_CHARS & set(account)
                or account.lower() in _RESERVED_ACCOUNT_LABELS):
            raise CommandFlagsError(
                "live commands require an exact DASHBOARD_LIVE_ACCOUNT_ID; "
                "a mode label or wildcard/empty account is insufficient"
            )
        if notional is None:
            raise CommandFlagsError(
                "live commands require DASHBOARD_LIVE_MAX_ORDER_NOTIONAL; "
                "there is no permissive fallback for a missing live order limit"
            )

    return CommandFlags(
        commands_enabled=commands,
        live_commands_enabled=live,
        live_account_id=account,
        live_max_order_notional=notional,
    )
