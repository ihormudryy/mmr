from types import SimpleNamespace

import pytest

from tests.research.service_fakes import AI, CLI
from tests.rpc_identity_fixtures import ServedStack, make_identities, write_keyset
from trader.messaging.principals import KNOWN_PRINCIPALS, RESEARCH_ACL
from trader.research.evaluation_service import EvaluationService
from trader.research.judgment_attest import JudgmentAttest
from trader.research.research_surface import build_research_registry

BODIES = {
    "submit_evaluation": {"kind": "INITIAL", "strategy_key": "strategies/x.py:X", "cohort": [{"A": 1}],
                          "conids": [1, 2], "bar_size": "15 mins"},
    "get_evaluation": {"request_id": "sha256:" + "a" * 64},
    "attest_from_judgment": {"judgment_id": "jdg-00000001"},
}
SUBMIT_REPLY_KEYS = {"status", "request_id", "state", "code", "detail", "retryable"}
GET_REPLY_KEYS = {"found", "request_id", "state", "case_digest", "summary"}
ATTEST_REPLY_KEYS = {"status", "bundle_digest", "code", "detail", "retryable", "binding"}


class Recorder:
    def __init__(self):
        self.calls = []

    def submit(self, body, caller):
        self.calls.append(("submit", caller.principal, body))
        return {"status": "ACCEPTED"}

    def get(self, request_id, caller):
        self.calls.append(("get", caller.principal))
        return {"found": False}

    def attest(self, body, caller):
        self.calls.append(("attest", caller.principal))
        return {"status": "REFUSED"}


@pytest.fixture
def stack():
    recorder = Recorder()
    registry = build_research_registry(evaluations=recorder, attest=recorder)
    served = ServedStack({("research", "command"): registry, ("research", "query"): registry}, make_identities())
    served.recorder = recorder
    yield served
    served.close()


def send(stack, method, body, caller="ai_research"):
    role = next(role for (role, name) in RESEARCH_ACL if name == method)
    return stack.send_raw(stack.signed(caller, server="research", role=role, method=method, body=body))


@pytest.mark.parametrize("role,method", sorted(RESEARCH_ACL))
def test_each_method_is_refused_at_the_server_for_every_caller_outside_its_row(stack, role, method):
    for caller in sorted(KNOWN_PRINCIPALS - RESEARCH_ACL[(role, method)]):
        code = stack.raw_code(stack.signed(caller, server="research", role=role, method=method,
                                           body=BODIES[method]))
        assert code in ("PERMISSION_DENIED", "AUTHENTICATION_ERROR"), (caller, code)
    assert stack.recorder.calls == []
    for caller in sorted(RESEARCH_ACL[(role, method)]):
        assert stack.raw_code(stack.signed(caller, server="research", role=role, method=method,
                                           body=BODIES[method])) == "OK"


@pytest.mark.parametrize("method,body", [
    ("submit_evaluation", {**BODIES["submit_evaluation"], "conids": [1, True]}),
    ("submit_evaluation", {**BODIES["submit_evaluation"], "research_day": "2024-03-29"}),
    ("submit_evaluation", {**BODIES["submit_evaluation"], "kind": "RENEWAL"}),
    ("submit_evaluation", {"kind": "RENEWAL", "strategy_key": "strategies/x.py:X"}),
    ("submit_evaluation", {"kind": "INITIAL", "strategy_key": "strategies/x.py:X"}),
    ("submit_evaluation", {**BODIES["submit_evaluation"], "cohort": [{"A": [1]}]}),
    ("get_evaluation", {"request_id": "sha256:" + "a" * 64, "extra": 1}),
    ("get_evaluation", {"request_id": "not-a-digest"}),
    ("attest_from_judgment", {"judgment_id": "jdg-00000001", "verdict": "DEPLOY"}),
    ("attest_from_judgment", {"judgment_id": "x"}),
])
def test_wire_models_are_strict(stack, method, body):
    assert send(stack, method, body).ok is False
    assert stack.recorder.calls == []


def test_a_renewal_shape_reaches_the_service_which_refuses_it(stack):
    body = {"kind": "RENEWAL", "prior_version_digest": "sha256:" + "9" * 64}
    assert stack.raw_code(stack.signed("ai_research", server="research", role="command",
                                       method="submit_evaluation", body=body)) == "OK"
    assert [call[:2] for call in stack.recorder.calls] == [("submit", "ai_research")]


def test_the_service_gets_the_body_with_kind_and_strips_it_itself(stack):
    send(stack, "submit_evaluation", BODIES["submit_evaluation"])
    assert stack.recorder.calls[0][2] == BODIES["submit_evaluation"]


def test_the_signing_key_must_not_be_an_rpc_key(tmp_path, monkeypatch):
    from trader.research.signing import InvalidKeyType, load_signing_key
    write_keyset(tmp_path / "rpc", ["research"])
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "rpc"))
    with pytest.raises(InvalidKeyType):
        load_signing_key(str(tmp_path / "rpc" / "research.key"))


class NothingStored:
    def get(self, request_id):
        return None


@pytest.fixture
def real_services():
    evaluations = EvaluationService(
        store=NothingStored(), trader=None, build_spec=None, evaluate=None, signer=SimpleNamespace(),
        artifacts_root="/nonexistent", warmup_sessions=5, order_notional=1900.0, queue_max=1, now=None)
    attest = JudgmentAttest(research_db=None, store=None, trader=None, signer=SimpleNamespace(),
                            artifacts_root="/nonexistent", repo_root="/nonexistent", is_paper=lambda: True,
                            now=None)
    return evaluations, attest


def handler(registry, role, method):
    registration = registry.resolve(role, method)
    return lambda body, caller: registration.handler(registration.request_model.model_validate(body), caller)


def test_every_handler_checks_its_caller_again_and_answers_with_a_reply_body(real_services):
    registry = build_research_registry(evaluations=real_services[0], attest=real_services[1])
    submit = handler(registry, "command", "submit_evaluation")(BODIES["submit_evaluation"], CLI)
    attest = handler(registry, "command", "attest_from_judgment")(BODIES["attest_from_judgment"], CLI)
    found = handler(registry, "query", "get_evaluation")(BODIES["get_evaluation"], SimpleNamespace(principal="trader"))
    assert (submit["status"], submit["code"]) == ("REFUSED", "PRINCIPAL_FORBIDDEN")
    assert (attest["status"], attest["code"]) == ("REFUSED", "PRINCIPAL_FORBIDDEN")
    assert found["found"] is False


def test_the_reply_shapes_are_the_ones_the_services_return(real_services):
    registry = build_research_registry(evaluations=real_services[0], attest=real_services[1])
    renewal = {"kind": "RENEWAL", "prior_version_digest": "sha256:" + "9" * 64}
    submit = handler(registry, "command", "submit_evaluation")(renewal, AI)
    attest = handler(registry, "command", "attest_from_judgment")(BODIES["attest_from_judgment"], CLI)
    found = handler(registry, "query", "get_evaluation")(BODIES["get_evaluation"], CLI)
    assert set(submit) == SUBMIT_REPLY_KEYS and submit["code"] == "RENEWAL_NOT_SUPPORTED"
    assert set(attest) == ATTEST_REPLY_KEYS and attest["retryable"] is False and attest["binding"] is None
    assert set(found) == GET_REPLY_KEYS


def test_replies_cross_the_wire_with_their_full_shape(real_services):
    registry = build_research_registry(evaluations=real_services[0], attest=real_services[1])
    served = ServedStack({("research", "command"): registry, ("research", "query"): registry}, make_identities())
    try:
        renewal = {"kind": "RENEWAL", "prior_version_digest": "sha256:" + "9" * 64}
        submit = send(served, "submit_evaluation", renewal)
        found = send(served, "get_evaluation", BODIES["get_evaluation"], caller="cli")
        assert submit.ok and set(submit.body) == SUBMIT_REPLY_KEYS
        assert found.ok and set(found.body) == GET_REPLY_KEYS
    finally:
        served.close()
