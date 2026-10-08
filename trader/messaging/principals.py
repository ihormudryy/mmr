"""RPC principals, the trust matrix and the per-method allow-list.

This module is code, reviewed in git, never user config (spec 5.3). A
principal is the name a caller signs as; its public key comes only from the
server's keyring on disk (``trader.messaging.rpc_keys``).
"""

from __future__ import annotations

import re
from typing import Mapping

KNOWN_PRINCIPALS: frozenset[str] = frozenset({
    "trader", "strategy", "cli", "dashboard", "ai_supervisor", "ai_research", "research",
})
SERVER_PRINCIPALS: frozenset[str] = frozenset({"trader", "strategy", "research"})
# Principals the SDK may sign as (``MMR_RPC_PRINCIPAL``).
CLIENT_PRINCIPALS: frozenset[str] = frozenset({"cli", "ai_supervisor", "ai_research"})
# Reserved names: no key, no allow-list entry. telegram_bridge arrives in SP2;
# scheduler has no trading RPC rights (owner answer 3).
RESERVED_PRINCIPALS: frozenset[str] = frozenset({"telegram_bridge", "scheduler"})
# The one principal that may carry a controller epoch in the envelope (SP2 spec 5.1).
CONTROLLER_PRINCIPAL = "ai_supervisor"

SERVER_ACCEPTS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"cli", "dashboard", "strategy", "ai_supervisor", "ai_research", "research"}),
    "strategy": frozenset({"cli", "dashboard", "trader"}),
    # SP2c spec 5.1: like strategy, research is a server that also calls the trader.
    "research": frozenset({"ai_research", "cli"}),
}

CALLS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"strategy"}),
    "strategy": frozenset({"trader"}),
    "cli": frozenset({"trader", "strategy", "research"}),
    "dashboard": frozenset({"trader", "strategy"}),
    "ai_supervisor": frozenset({"trader"}),
    "ai_research": frozenset({"trader", "research"}),
    "research": frozenset({"trader"}),
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


def rpc_files_for(principal: str | None) -> frozenset[str]:
    """Key files a service signing as ``principal`` must see: its own pair and its peers' ``.pub``."""
    if principal is None:
        return frozenset()
    return frozenset({f"{principal}.key", f"{principal}.pub"}
                     | {f"{peer}.pub" for peer in peers_for(principal)})


# ---------------------------------------------------------------------------
# Per-method allow-list, keyed by (socket role, method). A production
# registry refuses to register a method that has no entry here; an empty
# set means "nobody". Later plans add their own methods' entries.
# ---------------------------------------------------------------------------

HUMAN = frozenset({"cli", "dashboard"})
ACCOUNT_READERS = HUMAN | {"ai_supervisor"}
MARKET_READERS = HUMAN | {"ai_supervisor", "ai_research"}

_TRADER_ACCOUNT_READS = (
    "get_status", "get_account_values", "get_portfolio_summary", "get_positions",
    "get_open_orders", "get_trades", "get_risk_limits", "get_ib_account", "get_fx_rates",
    "get_account_cash_by_currency", "get_command", "get_proposal",
    "get_paper_automation_status", "diagnose_portfolio_feed", "snapshot_with_cursor",
)
_TRADER_MARKET_READS = (
    "get_snapshot", "get_snapshots_batch", "get_market_depth", "get_published_contracts",
    "list_universes", "get_universe", "scanner_locations", "scan_ideas",
)
_TRADER_HUMAN_COMMANDS = (
    "approve_proposal", "reject_proposal", "cancel_order", "cancel_orders", "resume_trading",
    "preflight_command", "liquidate_account", "enable_strategy", "disable_strategy",
    "update_strategy_params", "deactivate_live_canary", "suspend_allocation",
    "activate_paper_automation", "deactivate_paper_automation", "create_universe",
    "delete_universe", "add_universe_symbols", "remove_universe_symbol", "import_universe_csv",
)

TRADER_ACL: Mapping[tuple[str, str], frozenset[str]] = {
    **{("query", m): ACCOUNT_READERS for m in _TRADER_ACCOUNT_READS},
    **{("query", m): MARKET_READERS for m in _TRADER_MARKET_READS},
    ("query", "resolve_instrument"): MARKET_READERS | {"strategy"},
    ("query", "discover_instrument"): MARKET_READERS | {"strategy"},
    ("query", "publish_instrument"): HUMAN | {"strategy"},
    ("query", "get_trading_control"): ACCOUNT_READERS | {"strategy"},
    ("query", "list_proposals"): ACCOUNT_READERS | {"strategy"},
    ("query", "reconcile_with_broker"): HUMAN,
    ("feed", "read_domain_events"): HUMAN | {"strategy"},
    ("command", "create_proposal"): HUMAN | {"strategy"},
    ("command", "execute_automated_intent"): frozenset({"strategy"}),
    ("command", "record_state_acknowledged"): frozenset({"strategy"}),
    ("command", "pause_trading"): HUMAN | {"ai_supervisor"},
    # Owner answer 5 (ruling 18): live activation is cli only. dashboard keeps
    # the risk-reducing deactivate_live_canary / suspend_allocation.
    ("command", "activate_live_canary"): frozenset({"cli"}),
    ("command", "activate_allocation"): frozenset({"cli"}),
    **{("command", m): HUMAN for m in _TRADER_HUMAN_COMMANDS},
    # SP1 ai_paper (Plan 3 R23, owner answer 6): explicit sets per method, never a
    # group alias; reads and mutations are separate entries.
    # SP2 Plan 1 (spec 6.7): the operator publishes the initial policy; SP2a/b code never calls it as ai_supervisor.
    ("command", "publish_ai_risk_policy"): frozenset({"ai_supervisor", "cli"}),
    ("command", "submit_ai_paper_decision"): frozenset({"ai_supervisor"}),
    ("command", "register_ai_deployment"): frozenset({"ai_research"}),
    # SP2 Plan 3 (spec 6.6): only the operator registers a discretionary deployment; the bot cannot.
    ("command", "register_discretionary_deployment"): frozenset({"cli"}),
    ("query", "get_ai_risk_policy"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    ("query", "get_ai_deployment"): frozenset({"cli", "dashboard", "ai_supervisor", "ai_research"}),
    # SP2c Plan 2 (spec 5.1 table): explicit sets per method. The strategy service reads the active set.
    ("query", "get_ai_deployment_version"): frozenset({"cli", "dashboard", "ai_supervisor", "ai_research"}),
    ("command", "withdraw_ai_deployment"): frozenset({"cli", "dashboard"}),
    ("query", "get_active_ai_deployments"): frozenset({"strategy"}),
    # SP2 Plan 1: the controller epoch (spec 6.2). Explicit sets per method.
    ("command", "grant_ai_controller_epoch"): frozenset({"ai_supervisor"}),
    ("query", "get_ai_paper_decision"): frozenset({"ai_supervisor"}),
    ("query", "read_ai_signals"): frozenset({"ai_supervisor"}),
    # SP2 Plan 3: the trader-owned discovery read and entry quote (read only; spec 6.5, ruling 18).
    ("query", "discover_ai_candidates"): frozenset({"ai_supervisor"}),
    ("query", "get_ai_entry_quote"): frozenset({"ai_supervisor"}),
    # SP2c Plan 1 (spec 5.1 table): evaluation claims and judgments. Explicit sets per method.
    ("command", "claim_evaluation"): frozenset({"research"}),
    ("query", "get_evaluation_claim"): frozenset({"research"}),
    ("command", "update_evaluation_claim"): frozenset({"research"}),
    ("query", "get_deployment_forward_evidence"): frozenset({"research"}),
    ("command", "record_backtest_judgment"): frozenset({"ai_research"}),
    ("query", "get_backtest_judgment"): frozenset({"research", "ai_research", "cli", "dashboard"}),
    # SP2c Plan 3 (spec 7): only the research service records shadow rows.
    ("command", "record_shadow_result"): frozenset({"research"}),
    # SP1 experiments (Plan 4 K14): explicit sets per method. ai_supervisor may pause (risk-reducing)
    # and read; only operators start, resume and stop.
    ("command", "start_experiment"): frozenset({"cli", "dashboard"}),
    ("command", "pause_experiment"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    ("command", "resume_experiment"): frozenset({"cli", "dashboard"}),
    ("command", "stop_experiment"): frozenset({"cli", "dashboard"}),
    ("query", "get_experiment"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    # SP1 scoreboard (Plan 5): reads. verify is for humans. SP2 Plan 2 adds the two ingestion commands below (ai_supervisor only).
    ("query", "get_scoreboard"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    ("query", "verify_scoreboard"): frozenset({"cli", "dashboard"}),
    ("query", "get_experiment_trips"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    ("command", "record_ai_cost"): frozenset({"ai_supervisor"}),
    ("command", "record_simulated_decision"): frozenset({"ai_supervisor"}),
    # SP2 Plan 2: the owner's model cap from trader.yaml; read only, no principal writes it
    ("query", "get_ai_model_budget"): frozenset({"ai_supervisor"}),
    # SP1 acceptance (Plan 6): reads for the harness and the operator. Explicit sets per method.
    ("query", "get_acceptance_preflight"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    ("query", "get_broker_order_evidence"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    # The live OCA shrink probe (Plan 6 ruling 23): the operator only, never an AI or the dashboard.
    ("command", "acceptance_mark_start"): frozenset({"cli"}),
    ("command", "acceptance_shrink_probe"): frozenset({"cli"}),
}

STRATEGY_ACL: Mapping[tuple[str, str], frozenset[str]] = {
    **{("command", m): frozenset({"trader"}) for m in (
        "enable_strategy", "disable_strategy", "update_strategy_params",
        "arm_paper_automation", "disarm_paper_automation")},
    ("query", "get_strategy_receipt"): frozenset({"trader"}),
    ("query", "get_paper_automation_arm"): frozenset({"trader"}),
    ("query", "list_strategies"): HUMAN | {"trader"},
    ("command", "reload_strategies"): HUMAN | {"trader"},
    ("command", "enable_strategy_by_name"): HUMAN,
    ("command", "disable_strategy_by_name"): HUMAN,
}

# SP2c spec 5.1: the research server's methods. Each handler re-checks its caller.
RESEARCH_ACL: Mapping[tuple[str, str], frozenset[str]] = {
    ("command", "submit_evaluation"): frozenset({"ai_research"}),
    ("query", "get_evaluation"): frozenset({"ai_research", "cli"}),
    ("command", "attest_from_judgment"): frozenset({"ai_research"}),
}


# Compose service -> the principal it signs as (None: no key, tmpfs only).
SERVICE_PRINCIPAL: Mapping[str, str | None] = {
    "trader": "trader", "strategy": "strategy", "dashboard": "dashboard", "cli": "cli",
    "scheduler": None, "data": None, "ai": "ai_supervisor", "research": "research",
}

# A service that signs as more than one principal. SP2 spec 4: the ai service holds the
# ai_supervisor and ai_research keys (accepted limit: two keys in one process are not isolation).
SERVICE_EXTRA_PRINCIPALS: Mapping[str, tuple[str, ...]] = {"ai": ("ai_research",)}


def service_principals(service: str) -> tuple[str, ...]:
    own = SERVICE_PRINCIPAL[service]
    return () if own is None else (own, *SERVICE_EXTRA_PRINCIPALS.get(service, ()))


def service_rpc_files(service: str) -> frozenset[str]:
    """Key files a compose service must see: each own pair and every peer's .pub."""
    files: frozenset[str] = frozenset()
    for principal in service_principals(service):
        files |= rpc_files_for(principal)
    return files
