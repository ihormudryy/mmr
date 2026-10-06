"""RPC principals, the trust matrix and the per-method allow-list.

This module is code, reviewed in git, never user config (spec 5.3). A
principal is the name a caller signs as; its public key comes only from the
server's keyring on disk (``trader.messaging.rpc_keys``).
"""

from __future__ import annotations

import re
from typing import Mapping

KNOWN_PRINCIPALS: frozenset[str] = frozenset({
    "trader", "strategy", "cli", "dashboard", "ai_supervisor", "ai_research",
})
SERVER_PRINCIPALS: frozenset[str] = frozenset({"trader", "strategy"})
# Principals the SDK may sign as (``MMR_RPC_PRINCIPAL``).
CLIENT_PRINCIPALS: frozenset[str] = frozenset({"cli", "ai_supervisor", "ai_research"})
# Reserved names: no key, no allow-list entry. telegram_bridge arrives in SP2;
# scheduler has no trading RPC rights (owner answer 3).
RESERVED_PRINCIPALS: frozenset[str] = frozenset({"telegram_bridge", "scheduler"})

SERVER_ACCEPTS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"cli", "dashboard", "strategy", "ai_supervisor", "ai_research"}),
    "strategy": frozenset({"cli", "dashboard", "trader"}),
}

CALLS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"strategy"}),
    "strategy": frozenset({"trader"}),
    "cli": frozenset({"trader", "strategy"}),
    "dashboard": frozenset({"trader", "strategy"}),
    "ai_supervisor": frozenset({"trader"}),
    "ai_research": frozenset({"trader"}),
}

_PRINCIPAL_NAME = re.compile(r"[a-z][a-z_]{1,31}")


def is_valid_principal_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and _PRINCIPAL_NAME.fullmatch(name) is not None
        and name in KNOWN_PRINCIPALS
    )


def peers_for(principal: str) -> frozenset[str]:
    """Principals whose public keys ``principal`` needs: callers it accepts plus servers it calls."""
    if not is_valid_principal_name(principal):
        raise ValueError(f"unknown principal {principal!r}")
    return SERVER_ACCEPTS.get(principal, frozenset()) | CALLS[principal]
