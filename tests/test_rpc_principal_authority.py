"""Command authority comes from the authenticated RPC principal, never the body."""
import dataclasses
import uuid

import pytest

from tests.automation.test_automated_command_boundary import intent_to_wire, make_intent
from tests.rpc_identity_fixtures import (
    RecordingCoordinator, ServedStack, build_full_production_registry, make_identities,
)
from trader.messaging.production_api import attribution_label
from trader.messaging.typed_rpc import TypedRpcRemoteError, _DispatchProblem
from trader.trading.command_coordinator import CommandRequest, canonical_request_hash

PROPOSAL_BODY = {"conid": 265598, "action": "BUY", "quantity": 10}
APPROVE_BODY = {"proposal_id": 7, "expected_version": 1}


@pytest.fixture
def trader_served():
    ids = make_identities()
    coordinator = RecordingCoordinator()
    registry = build_full_production_registry(ids["trader"], coordinator=coordinator)
    stack = ServedStack({("trader", role): registry for role in ("query", "command", "feed")}, ids)
    stack.coordinator = coordinator
    yield stack
    stack.close()


def _cmd_id():
    return f"c-{uuid.uuid4().hex}"


def _code(stack, principal, role, method, body=None):
    return stack.raw_code(stack.signed(principal, "trader", role, method, body or {}))


def test_execute_automated_intent_source_and_principal_come_from_the_key(trader_served):
    body = intent_to_wire(make_intent())
    trader_served.client("strategy", "trader", "command").call("execute_automated_intent", body, dict)
    cmd = trader_served.coordinator.last_request
    assert (cmd.principal, cmd.source) == ("strategy", "strategy")


def test_approve_records_the_principal_as_source(trader_served):
    trader_served.client("cli", "trader", "command").call(
        "approve_proposal", {**APPROVE_BODY, "command_id": _cmd_id()}, dict)
    cmd = trader_served.coordinator.last_request
    assert (cmd.principal, cmd.source) == ("cli", "cli")


def test_approve_body_source_is_rejected(trader_served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        trader_served.client("cli", "trader", "command").call(
            "approve_proposal", {**APPROVE_BODY, "command_id": _cmd_id(), "source": "dashboard"}, dict)
    assert exc.value.code == "VALIDATION_ERROR"
    assert trader_served.coordinator.requests == []


@pytest.mark.parametrize("principal,label,expected", [
    ("strategy", "strategy:momentum", "strategy:momentum"),
    ("cli", "", "cli"), ("cli", "llm", "llm"), ("dashboard", "", "dashboard"),
])
def test_create_proposal_attribution(trader_served, principal, label, expected):
    trader_served.client(principal, "trader", "command").call(
        "create_proposal", {**PROPOSAL_BODY, "command_id": _cmd_id(), "source": label}, dict)
    cmd = trader_served.coordinator.last_request
    assert (cmd.principal, cmd.source, cmd.body["source"]) == (principal, expected, expected)


@pytest.mark.parametrize("principal,label", [("cli", "strategy:momentum"), ("strategy", ""),
                                             ("strategy", "llm"), ("dashboard", "strategy:x"),
                                             ("strategy", "strategy:a b")])
def test_create_proposal_label_cannot_impersonate(trader_served, principal, label):
    with pytest.raises(TypedRpcRemoteError) as exc:
        trader_served.client(principal, "trader", "command").call(
            "create_proposal", {**PROPOSAL_BODY, "command_id": _cmd_id(), "source": label}, dict)
    assert exc.value.code == "PERMISSION_DENIED"
    assert trader_served.coordinator.requests == []


def test_attribution_label_is_a_pure_check():
    assert attribution_label("ai_supervisor", "") == "ai_supervisor"
    with pytest.raises(_DispatchProblem):
        attribution_label("strategy", "strategy:")


def test_strategy_control_forward_records_the_real_caller(trader_served):
    trader_served.client("cli", "trader", "command").call(
        "enable_strategy",
        {"command_id": _cmd_id(), "strategy_name": "s", "expected_control_revision": 1}, dict)
    cmd = trader_served.coordinator.last_request
    assert cmd.principal == "cli" and cmd.source == "dashboard"


def test_principal_is_not_part_of_the_request_hash():
    base = CommandRequest(command_id="c1", action="approve_proposal", account_id="DU1",
                          target_type="proposal", target_id="7", expected_version=1,
                          body={"proposal_id": 7}, source="cli")
    assert canonical_request_hash(base) == canonical_request_hash(
        dataclasses.replace(base, principal="dashboard"))


AI_FORBIDDEN = ["approve_proposal", "execute_automated_intent", "liquidate_account", "resume_trading",
                "activate_live_canary", "activate_allocation", "activate_paper_automation",
                "create_proposal", "cancel_order", "record_state_acknowledged"]
NOT_ON_TYPED_SURFACE = ["place_standalone_order", "set_risk_limits", "buy", "sell"]


@pytest.mark.parametrize("principal", ["ai_supervisor", "ai_research"])
def test_ai_principals_cannot_trade_or_set_limits(trader_served, principal):
    for method in AI_FORBIDDEN:
        assert _code(trader_served, principal, "command", method) == "PERMISSION_DENIED", method
    for method in NOT_ON_TYPED_SURFACE:
        assert _code(trader_served, principal, "command", method) == "METHOD_NOT_ALLOWED", method
    assert trader_served.coordinator.requests == []


def test_ai_supervisor_may_pause_and_read(trader_served):
    assert _code(trader_served, "ai_supervisor", "command", "pause_trading") != "PERMISSION_DENIED"
    assert _code(trader_served, "ai_supervisor", "query", "get_account_values") != "PERMISSION_DENIED"
    assert _code(trader_served, "ai_research", "query", "get_account_values") == "PERMISSION_DENIED"
    assert _code(trader_served, "ai_research", "query", "get_snapshot") != "PERMISSION_DENIED"


def test_cli_activation_reaches_the_handler_but_dashboard_does_not(trader_served):
    assert _code(trader_served, "dashboard", "command", "activate_live_canary") == "PERMISSION_DENIED"
    # cli passes the allow-list; the body and the existing attestation/preflight
    # checks then apply unchanged (an empty body fails validation here).
    assert _code(trader_served, "cli", "command", "activate_live_canary") == "VALIDATION_ERROR"
