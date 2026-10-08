"""SP2c Plan 3 Task 1: research is a typed RPC server principal that also calls the trader."""
from __future__ import annotations

from tests.rpc_identity_fixtures import make_identities
from trader.messaging.principals import (
    CALLS, CLIENT_PRINCIPALS, KNOWN_PRINCIPALS, RESEARCH_ACL, SERVER_ACCEPTS, SERVER_PRINCIPALS, SERVICE_PRINCIPAL,
    TRADER_ACL, service_rpc_files,
)
from trader.messaging.rpc_keys import RESTART_ON_ROTATE
from trader.messaging.typed_rpc import TypedRpcClient, TypedRpcRegistry, TypedRpcServer

BACKTEST_JUDGE_RIGHTS = {
    ("command", "claim_evaluation"): {"research"},
    ("query", "get_evaluation_claim"): {"research"},
    ("command", "update_evaluation_claim"): {"research"},
    ("query", "get_deployment_forward_evidence"): {"research"},
    ("command", "record_backtest_judgment"): {"ai_research"},
    ("query", "get_backtest_judgment"): {"research", "ai_research", "cli", "dashboard"},
}


def test_research_is_a_server_that_calls_the_trader_and_not_an_sdk_signer():
    assert "research" in KNOWN_PRINCIPALS and "research" in SERVER_PRINCIPALS
    assert "research" not in CLIENT_PRINCIPALS
    assert SERVER_ACCEPTS["research"] == {"ai_research", "cli"}
    assert CALLS["research"] == {"trader"}
    assert {"research", "trader"} <= CALLS["ai_research"] and "research" in CALLS["cli"]
    assert "research" in SERVER_ACCEPTS["trader"]


def test_research_acl_is_the_spec_table():
    assert {key: set(value) for key, value in RESEARCH_ACL.items()} == {
        ("command", "submit_evaluation"): {"ai_research"},
        ("query", "get_evaluation"): {"ai_research", "cli"},
        ("command", "attest_from_judgment"): {"ai_research"}}
    assert all(allowed <= SERVER_ACCEPTS["research"] for allowed in RESEARCH_ACL.values())


def test_backtest_judge_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in BACKTEST_JUDGE_RIGHTS} == BACKTEST_JUDGE_RIGHTS


def test_research_has_no_trading_policy_or_decision_right():                       # review focus 5
    rights = {key for key, allowed in TRADER_ACL.items() if "research" in allowed}
    expected = {key for key, allowed in BACKTEST_JUDGE_RIGHTS.items() if "research" in allowed}
    assert rights == expected | {("command", "record_shadow_result")}
    assert TRADER_ACL[("command", "record_shadow_result")] == {"research"}


def test_service_identity_and_key_files():
    assert SERVICE_PRINCIPAL["research"] == "research"
    assert service_rpc_files("research") == {
        "research.key", "research.pub", "trader.pub", "ai_research.pub", "cli.pub"}
    for service in ("ai", "trader", "cli"):
        assert "research.pub" in service_rpc_files(service)


def test_rotation_restarts_follow_the_new_peers():
    assert RESTART_ON_ROTATE["research"] == ("ai", "research", "trader")
    assert RESTART_ON_ROTATE["cli"] == ("research", "strategy", "trader")
    assert RESTART_ON_ROTATE["ai_research"] == ("ai", "research", "trader")
    assert RESTART_ON_ROTATE["trader"] == ("ai", "dashboard", "research", "strategy", "trader")


def test_typed_server_and_client_accept_research():
    identities = make_identities()
    server = TypedRpcServer("query", TypedRpcRegistry(acl=RESEARCH_ACL), identities["research"], port=0)
    client = TypedRpcClient("query", identities["ai_research"], server="research", port=1)
    assert server.identity.principal == "research" and client.server == "research"
