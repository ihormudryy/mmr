"""SP2c Plan 1 Task 5: research joins only as a caller of the trader; Plan 3 makes it a service."""
from __future__ import annotations

from trader.messaging.principals import (
    CALLS, CLIENT_PRINCIPALS, KNOWN_PRINCIPALS, SERVER_ACCEPTS, SERVER_PRINCIPALS, SERVICE_PRINCIPAL, TRADER_ACL,
    rpc_files_for,
)
from trader.messaging.rpc_keys import RESTART_ON_ROTATE

BACKTEST_JUDGE_RIGHTS = {
    ("command", "claim_evaluation"): {"research"},
    ("query", "get_evaluation_claim"): {"research"},
    ("command", "update_evaluation_claim"): {"research"},
    ("query", "get_deployment_forward_evidence"): {"research"},
    ("command", "record_backtest_judgment"): {"ai_research"},
    ("query", "get_backtest_judgment"): {"research", "ai_research", "cli", "dashboard"},
}


def test_plan_1_adds_research_only_as_a_caller_of_the_trader():
    assert "research" in KNOWN_PRINCIPALS and CALLS["research"] == {"trader"}
    assert "research" in SERVER_ACCEPTS["trader"]
    assert "research" not in SERVER_PRINCIPALS and "research" not in SERVER_ACCEPTS
    assert "research" not in CLIENT_PRINCIPALS
    assert "research" not in SERVICE_PRINCIPAL and "research" not in SERVICE_PRINCIPAL.values()
    assert not [caller for caller, callees in CALLS.items() if caller != "research" and "research" in callees]
    assert RESTART_ON_ROTATE["research"] == ("trader",)              # Plan 3: ("ai", "research", "trader")
    assert "research.pub" in rpc_files_for("trader")


def test_backtest_judge_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in BACKTEST_JUDGE_RIGHTS} == BACKTEST_JUDGE_RIGHTS


def test_research_has_no_trading_policy_or_decision_right():
    rights = {key for key, allowed in TRADER_ACL.items() if "research" in allowed}
    assert rights == {key for key, allowed in BACKTEST_JUDGE_RIGHTS.items() if "research" in allowed}
