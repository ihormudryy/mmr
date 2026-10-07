import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.rpc_identity_fixtures import (
    ServedStack, build_full_production_registry, make_identities,
)
from trader.messaging import principals
from trader.messaging.principals import HUMAN, STRATEGY_ACL, TRADER_ACL
from trader.messaging.production_api import TypedStrategyControlPort
from trader.messaging.typed_rpc import RpcCaller, TypedRpcRegistry, TypedRpcRemoteError
from trader.trading.command_coordinator import CommandRequest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def served():
    ids = make_identities()
    registry = TypedRpcRegistry()
    stack = ServedStack({("trader", "query"): registry}, ids)
    stack.registry = registry
    yield stack
    stack.close()


@pytest.fixture
def served_with_acl():
    ids = make_identities()
    acl = {**TRADER_ACL, ("query", "whoami"): HUMAN}
    query = TypedRpcRegistry(acl=acl)
    command = TypedRpcRegistry(acl=acl)
    for (role, method) in TRADER_ACL:
        target = query if role == "query" else command if role == "command" else None
        if target is not None:
            target.register(role, method, dict, dict, lambda body: {"reached": True})
    stack = ServedStack({("trader", "query"): query, ("trader", "command"): command}, ids)
    stack.registry = query
    yield stack
    stack.close()


def _code(stack, principal, role, method, body=None):
    return stack.raw_code(stack.signed(principal, "trader", role, method, body))


def test_registry_with_acl_refuses_a_method_without_an_entry():
    reg = TypedRpcRegistry(acl={("query", "a"): frozenset({"cli"})})
    with pytest.raises(ValueError, match="allow-list"):
        reg.register("query", "b", dict, dict, lambda b: {})


def test_registry_without_acl_denies_everyone(served):
    served.registry.register("query", "ping", dict, dict, lambda b: {"ok": 1})
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("cli").call("ping", {}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


def test_empty_allow_list_entry_means_nobody():
    ids = make_identities()
    registry = TypedRpcRegistry(acl={("query", "nobody"): frozenset()})
    registry.register("query", "nobody", dict, dict, lambda b: {})
    stack = ServedStack({("trader", "query"): registry}, ids)
    try:
        for principal in principals.SERVER_ACCEPTS["trader"]:
            assert _code(stack, principal, "query", "nobody") == "PERMISSION_DENIED"
    finally:
        stack.close()


def test_principal_outside_the_allow_list_is_denied_and_logged(served_with_acl, caplog, monkeypatch):
    logger = logging.getLogger("trader.messaging.typed_rpc")
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "disabled", False)
    with caplog.at_level(logging.WARNING, logger="trader.messaging.typed_rpc"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served_with_acl.client("ai_research").call("get_positions", {}, dict)
    assert exc.value.code == "PERMISSION_DENIED"
    assert "ai_research" in caplog.text and "get_positions" in caplog.text


def test_unregistered_method_is_method_not_allowed_not_permission_denied(served_with_acl):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served_with_acl.client("cli").call("no_such_method", {}, dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"


def test_denied_request_still_burns_its_nonce(served_with_acl):
    req = served_with_acl.signed("ai_research", "trader", "query", "get_positions")
    assert served_with_acl.send_raw(req).problem.code == "PERMISSION_DENIED"
    assert served_with_acl.send_raw(req).problem.code == "REPLAY_ERROR"


def test_permission_is_checked_before_body_validation():
    ids = make_identities()
    from pydantic import BaseModel

    class Strict(BaseModel):
        needed: int

    registry = TypedRpcRegistry(acl={("query", "strict"): frozenset({"cli"})})
    registry.register("query", "strict", Strict, dict, lambda b: {})
    stack = ServedStack({("trader", "query"): registry}, ids)
    try:
        assert _code(stack, "dashboard", "query", "strict") == "PERMISSION_DENIED"
        assert _code(stack, "cli", "query", "strict") == "VALIDATION_ERROR"
    finally:
        stack.close()


@pytest.mark.parametrize("execution", ["inline", "thread"])
def test_with_caller_handler_receives_the_authenticated_caller(served_with_acl, execution):
    seen = []
    served_with_acl.registry.register("query", "whoami", dict, dict,
                                      lambda b, caller: seen.append(caller) or {},
                                      with_caller=True, execution=execution)
    served_with_acl.client("dashboard").call("whoami", {}, dict)
    assert seen == [RpcCaller("dashboard", None)]


def test_scheduler_is_not_a_principal_and_appears_in_no_acl():
    assert "scheduler" not in principals.KNOWN_PRINCIPALS and "scheduler" in principals.RESERVED_PRINCIPALS
    for table in (TRADER_ACL, STRATEGY_ACL):
        assert all("scheduler" not in allowed for allowed in table.values())
        assert all(allowed <= principals.KNOWN_PRINCIPALS for allowed in table.values())


def test_strategy_acl_names_only_principals_the_strategy_server_accepts():
    assert all(allowed <= principals.SERVER_ACCEPTS["strategy"] for allowed in STRATEGY_ACL.values())
    assert all(allowed <= principals.SERVER_ACCEPTS["trader"] for allowed in TRADER_ACL.values())


@pytest.mark.parametrize("method", ["activate_live_canary", "activate_allocation"])
def test_activation_is_cli_only(served_with_acl, method):
    assert TRADER_ACL[("command", method)] == frozenset({"cli"})
    for principal in ("dashboard", "strategy", "ai_supervisor", "ai_research"):
        assert _code(served_with_acl, principal, "command", method) == "PERMISSION_DENIED", principal
    assert _code(served_with_acl, "cli", "command", method) == "OK"


@pytest.mark.parametrize("method", ["deactivate_live_canary", "suspend_allocation"])
def test_dashboard_may_still_deactivate_and_suspend(served_with_acl, method):
    assert _code(served_with_acl, "dashboard", "command", method) == "OK"


def test_dashboard_may_read_status(served_with_acl):
    served_with_acl.client("dashboard").call("get_paper_automation_status", {}, dict)


def test_every_production_trader_method_has_an_entry():
    reg = build_full_production_registry()
    registered = {(r.socket_role, r.method) for r in reg.registrations()}
    assert registered <= set(TRADER_ACL)
    assert {("command", "activate_live_canary"), ("command", "execute_automated_intent"),
            ("feed", "read_domain_events")} <= registered


def test_every_strategy_method_has_an_entry():
    from trader.strategy.strategy_runtime import register_strategy_control_authority
    cmd, qry = TypedRpcRegistry(acl=STRATEGY_ACL), TypedRpcRegistry(acl=STRATEGY_ACL)
    register_strategy_control_authority(cmd, qry, MagicMock())
    registered = {(r.socket_role, r.method) for r in [*cmd.registrations(), *qry.registrations()]}
    assert registered == set(STRATEGY_ACL)


def test_every_registration_in_the_source_has_an_entry():
    pattern = re.compile(r"\.register\(\s*['\"](query|command|feed)['\"],\s*['\"]([a-z_]+)['\"]")
    found = set()
    for path in [*ROOT.joinpath("trader").rglob("*.py"), *ROOT.joinpath("web").rglob("*.py")]:
        found |= set(pattern.findall(path.read_text()))
    assert found, "registration scan found nothing"
    missing = found - set(TRADER_ACL) - set(STRATEGY_ACL)
    assert not missing, missing


def test_forwarded_call_is_authorized_as_trader_not_on_behalf_of():
    ids = make_identities()
    seen = []
    command = TypedRpcRegistry(acl=STRATEGY_ACL)
    query = TypedRpcRegistry(acl=STRATEGY_ACL)
    command.register("command", "enable_strategy", dict, dict,
                     lambda body, caller: seen.append(caller) or {
                         "command_id": body["command_id"], "status": "APPLIED"},
                     with_caller=True)
    stack = ServedStack({("strategy", "command"): command, ("strategy", "query"): query}, ids)
    try:
        body = {"command_id": "c1", "strategy_name": "s", "expected_control_revision": 1}
        with pytest.raises(TypedRpcRemoteError) as exc:
            stack.client("dashboard", "strategy", "command").call("enable_strategy", body, dict)
        assert exc.value.code == "PERMISSION_DENIED"
        assert seen == []

        port = TypedStrategyControlPort(stack.client("trader", "strategy", "command"),
                                        stack.client("trader", "strategy", "query"))
        request = CommandRequest(
            command_id="c2", action="enable_strategy", account_id="DU1", target_type="strategy",
            target_id="s", expected_version=1, body={"strategy_name": "s"}, source="operator",
            principal="dashboard", preflight_nonce=None)
        try:
            port.forward(request)
        except Exception:
            pass  # the stub receipt is not a full StrategyCommandReceipt; only the caller matters
        assert seen == [RpcCaller("trader", "dashboard")]
    finally:
        stack.close()


def test_ai_principals_get_the_ai_paper_rights_and_no_trading_rights():
    def rights(principal):
        return {key for key, allowed in TRADER_ACL.items() if principal in allowed}
    assert ("command", "register_ai_deployment") in rights("ai_research")
    assert {("command", "publish_ai_risk_policy"), ("command", "submit_ai_paper_decision")} <= rights("ai_supervisor")
    for principal in ("ai_supervisor", "ai_research"):     # inclusion/exclusion only: Plan 5 adds scoreboard reads
        assert not {("command", m) for m in ("approve_proposal", "execute_automated_intent", "liquidate_account",
                                             "resume_trading", "create_proposal")} & rights(principal)
    assert ("command", "register_ai_deployment") not in rights("ai_supervisor")
    assert not {("command", "publish_ai_risk_policy"), ("command", "submit_ai_paper_decision")} & rights("ai_research")


AI_PAPER_FAMILY = {                                                    # R23, owner answer 6: exact, method by method
    ("command", "publish_ai_risk_policy"): {"ai_supervisor", "cli"},
    ("command", "submit_ai_paper_decision"): {"ai_supervisor"},
    ("command", "register_ai_deployment"): {"ai_research"},
    ("query", "get_ai_risk_policy"): {"cli", "dashboard", "ai_supervisor"},
    ("query", "get_ai_deployment"): {"cli", "dashboard", "ai_supervisor", "ai_research"},
}


def test_ai_paper_family_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in AI_PAPER_FAMILY} == AI_PAPER_FAMILY
    assert {key for key, allowed in TRADER_ACL.items() if "ai_research" in allowed and key in AI_PAPER_FAMILY} == {
        ("command", "register_ai_deployment"), ("query", "get_ai_deployment")}
    assert not [key for key in AI_PAPER_FAMILY if {"strategy", "scheduler", "trader"} & set(TRADER_ACL[key])]


def test_the_full_registry_registers_the_ai_paper_family():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert set(AI_PAPER_FAMILY) <= registered


def test_experiment_rights():                                            # SP1 Plan 4 K14
    from trader.messaging.principals import TRADER_ACL

    def rights(principal):
        return {key for key, allowed in TRADER_ACL.items() if principal in allowed}
    for principal in ("cli", "dashboard"):
        assert {("command", f"{a}_experiment") for a in ("start", "pause", "resume", "stop")} <= rights(principal)
        assert ("query", "get_experiment") in rights(principal)
    assert {("command", "pause_experiment"), ("query", "get_experiment")} <= rights("ai_supervisor")
    assert not {("command", "start_experiment"), ("command", "resume_experiment"),
                ("command", "stop_experiment")} & rights("ai_supervisor")
    assert not {k for k in rights("ai_research") if "experiment" in k[1]}
    assert not {k for k in rights("strategy") if "experiment" in k[1]}


def test_acceptance_rights_are_exact():                                   # SP1 Plan 6 rulings 14 and 23
    for method in ("get_acceptance_preflight", "get_broker_order_evidence"):
        assert TRADER_ACL[("query", method)] == frozenset({"cli", "dashboard", "ai_supervisor"})
    for method in ("acceptance_mark_start", "acceptance_shrink_probe"):
        assert TRADER_ACL[("command", method)] == frozenset({"cli"})      # never an AI, never the dashboard
