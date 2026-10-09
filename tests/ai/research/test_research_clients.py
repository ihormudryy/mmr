import pytest

from trader.ai.rpc_clients import (LAB_COMMANDS, LAB_QUERIES, RESEARCH_COMMANDS, RESEARCH_QUERIES, AiRpcClients,
                                   MethodNotAllowedLocally, RpcNotSent)
from trader.messaging.principals import RESEARCH_ACL, TRADER_ACL


class Socket:
    def __init__(self, name, fail=False):
        self.name, self.fail = name, fail

    def call(self, method, body, model, timeout, **options):
        if self.fail:
            raise ConnectionError("no route")
        return {"via": self.name}

    def close(self):
        pass


def clients(lab_fail=False):
    names = ("supervisor_command", "supervisor_query", "supervisor_discovery", "research_command", "research_query")
    return AiRpcClients.from_sockets(**{name: Socket(name) for name in names}, lab_command=Socket("lab_command",
                                     lab_fail), lab_query=Socket("lab_query"), timeout=5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,via", [("submit_evaluation", "lab_command"), ("get_evaluation", "lab_query"),
                                        ("attest_from_judgment", "lab_command")])
async def test_lab_methods_go_to_the_research_server(method, via):
    assert (await clients().lab.call(method, {}))["via"] == via


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["register_ai_deployment", "record_backtest_judgment", "submit_ai_paper_decision",
                                    "claim_evaluation", "record_shadow_result", "get_active_ai_deployments"])
async def test_the_lab_client_refuses_everything_else_before_signing(method):
    with pytest.raises(MethodNotAllowedLocally):
        await clients().lab.call(method, {})


def test_the_trader_research_client_gains_the_judgment_methods_only():
    assert clients().research.methods == {"register_ai_deployment", "record_backtest_judgment", "get_ai_deployment",
                                          "get_backtest_judgment", "get_ai_deployment_version"}


@pytest.mark.asyncio
async def test_an_unreachable_research_server_is_named_as_such():
    with pytest.raises(RpcNotSent) as exc:
        await clients(lab_fail=True).lab.call("submit_evaluation", {})
    assert exc.value.code == "RESEARCH_UNREACHABLE"


@pytest.mark.asyncio
async def test_an_unreachable_trader_keeps_its_own_code():
    sockets = {name: Socket(name, fail=True) for name in (
        "supervisor_command", "supervisor_query", "supervisor_discovery", "research_command", "research_query")}
    broken = AiRpcClients.from_sockets(**sockets, lab_command=Socket("lab_command"), lab_query=Socket("lab_query"),
                                       timeout=5.0)
    with pytest.raises(RpcNotSent) as exc:
        await broken.research.call("get_backtest_judgment", {})
    assert exc.value.code == "TRADER_UNREACHABLE"


def test_every_client_method_is_one_the_server_allows_ai_research_to_call():
    allowed_by_research_server = {name for (_, name), principals in RESEARCH_ACL.items() if "ai_research" in principals}
    allowed_by_trader = {name for (_, name), principals in TRADER_ACL.items() if "ai_research" in principals}
    assert LAB_COMMANDS | LAB_QUERIES == allowed_by_research_server
    assert RESEARCH_COMMANDS | RESEARCH_QUERIES <= allowed_by_trader


def test_each_method_is_on_the_socket_role_its_acl_row_names():
    assert {name for role, name in RESEARCH_ACL if role == "command"} >= LAB_COMMANDS
    assert {name for role, name in RESEARCH_ACL if role == "query"} >= LAB_QUERIES
    assert {name for role, name in TRADER_ACL if role == "command"} >= RESEARCH_COMMANDS
    assert {name for role, name in TRADER_ACL if role == "query"} >= RESEARCH_QUERIES


def test_close_closes_the_lab_sockets_too():
    closed = []

    class Closing(Socket):
        def close(self):
            closed.append(self.name)
    names = ("supervisor_command", "supervisor_query", "supervisor_discovery", "research_command", "research_query")
    AiRpcClients.from_sockets(**{n: Closing(n) for n in names}, lab_command=Closing("lab_command"),
                              lab_query=Closing("lab_query"), timeout=5.0).close()
    assert {"lab_command", "lab_query"} <= set(closed)
