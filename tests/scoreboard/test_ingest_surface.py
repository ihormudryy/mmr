import pytest

from tests.rpc_identity_fixtures import ServedStack, make_identities
from tests.scoreboard.ingest_world import FakeDecisions, cost_body, enter_fact, make_ingest, sim_body
from trader.messaging.ai_ingest_surface import register_ai_ingest_surface
from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError

METHODS = {"record_ai_cost": cost_body, "record_simulated_decision": sim_body}


@pytest.fixture
def served(store):
    command = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_ai_ingest_surface(command, make_ingest(store, decisions=FakeDecisions(enter_fact())))
    query = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_ai_ingest_surface(query, None, model_budget=1500.0)
    stack = ServedStack({("trader", "command"): command, ("trader", "query"): query}, make_identities())
    yield stack
    stack.close()


def test_only_ai_supervisor_reads_the_model_budget_and_nobody_writes_it(served):     # review focus 5
    reply = served.client("ai_supervisor", role="query").call("get_ai_model_budget", {}, dict)
    assert reply == {"model_budget_usd_per_day": 1500.0, "source": "trader.yaml"}
    for principal in ("cli", "dashboard", "ai_research"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.client(principal, role="query").call("get_ai_model_budget", {}, dict)
        assert exc.value.code == "PERMISSION_DENIED"
    assert TRADER_ACL[("query", "get_ai_model_budget")] == {"ai_supervisor"}
    assert not [key for key in TRADER_ACL if key[0] == "command" and "budget" in key[1]]


def test_only_ai_supervisor_may_ingest(served):
    for method, body in METHODS.items():
        assert served.client("ai_supervisor", role="command").call(method, body(), dict)["status"] == "INSERTED"
        for principal in ("cli", "dashboard", "ai_research"):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, role="command").call(method, body(), dict)
            assert exc.value.code == "PERMISSION_DENIED"


def test_a_conflict_is_a_refused_reply_not_an_rpc_error(served):
    client = served.client("ai_supervisor", role="command")
    client.call("record_ai_cost", cost_body(), dict)
    reply = client.call("record_ai_cost", cost_body(cost_usd=9.0), dict)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "CONFLICTING_DUPLICATE", False)


def test_a_malformed_body_is_a_validation_error(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("ai_supervisor", role="command").call("record_ai_cost", cost_body(cost_status="unknown"), dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_ingestion_rights_are_exact_and_the_ai_has_no_other_scoreboard_write():
    assert TRADER_ACL[("command", "record_ai_cost")] == {"ai_supervisor"}
    assert TRADER_ACL[("command", "record_simulated_decision")] == {"ai_supervisor"}
    for principal in ("ai_supervisor", "ai_research"):
        writes = {k[1] for k, allowed in TRADER_ACL.items() if principal in allowed and k[0] == "command"
                  and any(word in k[1] for word in ("scoreboard", "ai_cost", "simulated", "benchmark", "equity"))}
        assert writes == ({"record_ai_cost", "record_simulated_decision"} if principal == "ai_supervisor" else set())


def test_the_full_production_registry_registers_both_commands():
    from tests.rpc_identity_fixtures import build_full_production_registry
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert {("command", "record_ai_cost"), ("command", "record_simulated_decision"),
            ("query", "get_ai_model_budget")} <= registered
