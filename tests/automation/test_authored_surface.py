"""SP3 Plan 3 Task 9: the seven methods over the real signed trader server (spec 5, 10 Keys and Authority)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.automation.authored_fixtures import (
    IMAGE, OTHER_IMAGE, authored_journal, authored_world, deployed, evaluating, proof_request, request_id, stored,
    submit_request,
)
from tests.automation.backtest_judge_fixtures import NOW, Clock
from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, build_full_production_registry, make_identities
from trader.automation.authored_files import AuthoredFileStore, AuthoredStoreError
from trader.automation.authored_isolation import IsolationProofs
from trader.messaging.authored_surface import register_authored_surface
from trader.messaging.principals import SERVER_ACCEPTS, TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError

ZERO = "sha256:" + "0" * 64
ROWS = {
    ("command", "submit_authored_strategy"): {"ai_author"},
    ("query", "get_authored_strategies"): {"ai_author"},
    ("query", "list_authored_strategies"): {"cli", "dashboard"},
    ("command", "report_authored_evaluation"): {"research"},
    ("command", "retire_authored_strategy"): {"strategy"},
    ("command", "record_isolation_proof"): {"cli"},
    ("query", "get_isolation_proof"): {"cli", "strategy"},
}
VALID_BODIES = {
    "submit_authored_strategy": lambda: submit_request().model_dump(),
    "get_authored_strategies": dict,
    "list_authored_strategies": dict,
    "report_authored_evaluation": lambda: {"digest": ZERO, "request_id": ZERO, "event": "SANDBOX_ERROR",
                                           "sandbox_image_digest": None, "detail": ""},
    "retire_authored_strategy": lambda: {"digest": ZERO, "code": "SANDBOX_ERROR", "detail": ""},
    "record_isolation_proof": lambda: proof_request().model_dump(),
    "get_isolation_proof": lambda: {"image_digest": None},
}


def role_of(method: str) -> str:
    return "command" if ("command", method) in TRADER_ACL else "query"


def serve(tmp_path, *, acl=TRADER_ACL, **config):
    proofs = IsolationProofs(authored_journal(tmp_path), now=Clock(NOW))
    world = authored_world(tmp_path, proofs=proofs, **config)
    registry = TypedRpcRegistry(acl=acl, default_execution="thread")
    register_authored_surface(registry, authored=world.store, proofs=proofs)
    stack = ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, make_identities())

    def call(principal: str, method: str, body: dict) -> dict:
        return stack.client(principal, role=role_of(method)).call(method, body, dict)
    return SimpleNamespace(world=world, proofs=proofs, stack=stack, call=call)


@pytest.fixture
def served(tmp_path):
    setup = serve(tmp_path)
    yield setup
    setup.stack.close()


def test_every_method_is_refused_for_every_caller_outside_its_row(served):               # spec 10 Keys
    for (role, method), allowed in ROWS.items():
        assert TRADER_ACL[(role, method)] == allowed
        for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.stack.client(principal, role=role).call(method, {}, dict)
            assert exc.value.code == "PERMISSION_DENIED", (principal, method)


def test_each_handler_refuses_a_wrong_caller_even_behind_an_open_allow_list(tmp_path):
    setup = serve(tmp_path, acl=ALLOW_ALL)
    try:
        for (role, method), allowed in ROWS.items():
            for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
                with pytest.raises(TypedRpcRemoteError) as exc:
                    setup.stack.client(principal, role=role).call(method, VALID_BODIES[method](), dict)
                assert exc.value.code == "PERMISSION_DENIED", (principal, method)
    finally:
        setup.stack.close()


def test_ai_author_stores_reads_back_and_resubmits_over_signed_rpc(served):
    first = served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
    assert (first["status"], first["record"]["state"]) == ("STORED", "STORED")
    assert served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())["status"] == "EXISTING"
    view = served.call("ai_author", "get_authored_strategies", {})
    assert (view["counted"], view["room"]) == (1, 2)
    assert [record["digest"] for record in view["records"]] == [first["record"]["digest"]]
    assert served.call("cli", "list_authored_strategies", {}) == view


def test_a_refusal_is_a_reply_body_not_an_rpc_error(tmp_path):                         # spec 10 Authority
    setup = serve(tmp_path, enabled=False)
    try:
        reply = setup.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
        assert (reply["status"], reply["code"], reply["record"]) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED", None)
    finally:
        setup.stack.close()


def test_a_malformed_body_is_a_validation_error(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_author", "submit_authored_strategy", {"source": "x"})
    assert exc.value.code == "VALIDATION_ERROR"


def test_research_reports_and_the_strategy_retires_over_signed_rpc(served):
    digest = stored(served.world)
    evaluating(served.world, digest, request_id(1))
    reply = served.call("research", "report_authored_evaluation", {
        "digest": digest, "request_id": request_id(1), "event": "STARTED", "sandbox_image_digest": IMAGE,
        "detail": ""})
    assert (reply["status"], reply["record"]["sandbox_image_digest"], reply["judged_count"]) == ("UPDATED", IMAGE, 0)
    live = deployed(served.world, 16)
    retired = served.call("strategy", "retire_authored_strategy",
                          {"digest": live, "code": "SANDBOX_ERROR", "detail": "child crashed"})
    assert (retired["status"], retired["record"]["state"]) == ("UPDATED", "RETIRED")


def test_the_operator_records_a_proof_and_the_strategy_reads_it(served):                   # ruling 13
    recorded = served.call("cli", "record_isolation_proof", proof_request().model_dump())
    assert recorded["status"] == "RECORDED"
    assert served.call("strategy", "get_isolation_proof", {"image_digest": IMAGE}) == {
        "found": True, "proof": recorded["proof"]}
    assert served.call("cli", "get_isolation_proof", {"image_digest": OTHER_IMAGE}) == {"found": False, "proof": None}


def test_an_unusable_store_is_an_rpc_error(served, monkeypatch):
    def broken(self, digest, source, meta):
        raise AuthoredStoreError("/store is not a real directory")
    monkeypatch.setattr(AuthoredFileStore, "write", broken)
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
    assert exc.value.code == "AUTHORED_STORE_UNAVAILABLE"


def test_the_production_registry_registers_the_seven_methods():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert set(ROWS) <= registered
