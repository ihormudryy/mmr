"""SP2 Plan 5 Task 2: two method-restricted clients over signed typed RPC (spec 4, 12 Security)."""
import pytest

from tests.ai.runtime.fakes import FakeSocket
from tests.rpc_identity_fixtures import ServedStack, make_identities
from trader.ai.rpc_clients import (
    EPOCH_METHODS, RESEARCH_COMMANDS, RESEARCH_QUERIES, SUPERVISOR_COMMANDS, SUPERVISOR_QUERIES,
    SUPERVISOR_SLOW_QUERIES, AiRpcClients, MethodNotAllowedLocally, ReadOnlySupervisor, RpcNotSent,
    RpcOutcomeUnknown, RpcRefused,
)
from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import AuthenticationError, TypedRpcRegistry, TypedRpcRemoteError


def fake_clients(epoch=None, error=None):
    sockets = {name: FakeSocket(error=error) for name in ("sc", "sq", "sd", "rc", "rq")}
    clients = AiRpcClients.from_sockets(
        supervisor_command=sockets["sc"], supervisor_query=sockets["sq"], supervisor_discovery=sockets["sd"],
        research_command=sockets["rc"], research_query=sockets["rq"], timeout=10.0)
    clients.supervisor.bind_epoch(lambda: epoch)
    return clients, sockets


@pytest.mark.asyncio
@pytest.mark.parametrize("who,method", [
    ("supervisor", "publish_ai_risk_policy"), ("supervisor", "register_ai_deployment"),
    ("supervisor", "pause_experiment"), ("supervisor", "start_experiment"),
    ("research", "submit_ai_paper_decision"), ("research", "grant_ai_controller_epoch"),
    ("research", "read_ai_signals"), ("research", "record_ai_cost")])
async def test_a_method_outside_the_set_fails_before_sending(who, method):
    clients, sockets = fake_clients(epoch=3)
    with pytest.raises(MethodNotAllowedLocally):
        await getattr(clients, who).call(method, {})
    assert all(socket.calls == [] for socket in sockets.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(EPOCH_METHODS))
async def test_epoch_methods_need_a_held_epoch(method):
    clients, sockets = fake_clients(epoch=None)
    with pytest.raises(RpcNotSent) as exc:
        await clients.supervisor.call(method, {})
    assert exc.value.code == "NOT_LEADER" and sockets["sc"].calls == sockets["sq"].calls == []


@pytest.mark.asyncio
async def test_the_held_epoch_rides_only_on_epoch_methods():
    clients, sockets = fake_clients(epoch=4)
    await clients.supervisor.call("submit_ai_paper_decision", {"x": 1})
    await clients.supervisor.call("record_ai_cost", {"x": 2})
    await clients.supervisor.call("get_experiment", {})
    await clients.supervisor.call("get_ai_paper_decision", {"decision_id": "d"}, epoch=5)
    assert [c[3] for c in sockets["sc"].calls] == [{"controller_epoch": 4}, {}]
    assert [c[3] for c in sockets["sq"].calls] == [{}, {"controller_epoch": 5}]


@pytest.mark.asyncio
async def test_discovery_uses_its_own_socket_and_a_90_second_timeout():
    clients, sockets = fake_clients(epoch=1)
    await clients.supervisor.call("discover_ai_candidates", {})
    await clients.supervisor.call("get_snapshot", {})
    assert [(c[0], c[2]) for c in sockets["sd"].calls] == [("discover_ai_candidates", 90.0)]
    assert [(c[0], c[2]) for c in sockets["sq"].calls] == [("get_snapshot", 10.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("error,kind,code", [
    (ConnectionError("no route to server"), RpcNotSent, "TRADER_UNREACHABLE"),
    (TimeoutError("late"), RpcOutcomeUnknown, "REPLY_TIMEOUT"),
    (AuthenticationError("bad reply"), RpcOutcomeUnknown, "REPLY_UNTRUSTED"),
    (TypedRpcRemoteError("CONTROLLER_EPOCH_STALE", "stale"), RpcRefused, "CONTROLLER_EPOCH_STALE")])
async def test_errors_are_classified_by_what_the_trader_can_have_seen(error, kind, code):
    clients, _ = fake_clients(epoch=1, error=error)
    with pytest.raises(kind) as exc:
        await clients.supervisor.call("submit_ai_paper_decision", {})
    assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit_ai_paper_decision", "read_ai_signals", "record_ai_cost",
                                    "grant_ai_controller_epoch"])
async def test_read_only_supervisor_refuses_commands_and_epoch_methods(method):
    clients, sockets = fake_clients(epoch=1)
    with pytest.raises(MethodNotAllowedLocally):
        await ReadOnlySupervisor(clients.supervisor).call(method, {})
    assert await ReadOnlySupervisor(clients.supervisor).call("get_snapshot", {}) == {"method": "get_snapshot"}


# Plan 3 (not merged yet) adds this trader method and its allow-list entry. Until then it is only a name here.
PLAN_3_METHODS = {("query", "discover_ai_candidates")}


def test_client_sets_match_the_trader_allow_list():
    absent = set()
    for role, methods, principal in (
            ("command", SUPERVISOR_COMMANDS, "ai_supervisor"),
            ("query", SUPERVISOR_QUERIES | set(SUPERVISOR_SLOW_QUERIES), "ai_supervisor"),
            ("command", RESEARCH_COMMANDS, "ai_research"), ("query", RESEARCH_QUERIES, "ai_research")):
        for method in methods:
            if (role, method) not in TRADER_ACL:
                absent.add((role, method))
                continue
            assert principal in TRADER_ACL[(role, method)], (role, method)
    assert absent <= PLAN_3_METHODS, absent
    every = SUPERVISOR_COMMANDS | SUPERVISOR_QUERIES | set(SUPERVISOR_SLOW_QUERIES) | RESEARCH_COMMANDS
    assert "publish_ai_risk_policy" not in every                   # spec 6.7: SP2a/b never publishes policy


def test_cross_principal_calls_are_refused_through_signed_rpc():
    ids = make_identities()
    registry = TypedRpcRegistry(acl=TRADER_ACL)
    echo = lambda body, caller: {"principal": caller.principal, "epoch": caller.controller_epoch}  # noqa: E731
    for role, method in (("command", "submit_ai_paper_decision"), ("command", "register_ai_deployment"),
                         ("query", "get_experiment")):
        registry.register(role, method, dict, dict, echo, with_caller=True)
    served = ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, ids)
    try:
        import asyncio
        clients = AiRpcClients.from_sockets(
            supervisor_command=served.client("ai_supervisor", role="command"),
            supervisor_query=served.client("ai_supervisor"), supervisor_discovery=served.client("ai_supervisor"),
            research_command=served.client("ai_research", role="command"),
            research_query=served.client("ai_research"), timeout=5.0)
        clients.supervisor.bind_epoch(lambda: 3)
        assert asyncio.run(clients.supervisor.call("submit_ai_paper_decision", {})) == \
            {"principal": "ai_supervisor", "epoch": 3}
        assert asyncio.run(clients.research.call("register_ai_deployment", {}))["principal"] == "ai_research"
        for principal, method in (("ai_research", "submit_ai_paper_decision"),
                                  ("ai_supervisor", "register_ai_deployment")):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, role="command").call(method, {}, dict)
            assert exc.value.code == "PERMISSION_DENIED"
        with pytest.raises(TypedRpcRemoteError) as exc:                              # Plan 1 Ruling 6
            served.client("ai_research", role="command").call("register_ai_deployment", {}, dict,
                                                              controller_epoch=1)
        assert exc.value.code == "AUTHENTICATION_ERROR"
    finally:
        served.close()
