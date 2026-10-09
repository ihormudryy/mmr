"""SP2c Plan 1 Task 6: the six methods over the real signed trader server (spec 5.1 table, 9)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tests.automation.backtest_judge_fixtures import (
    ZERO, count_claims, finished, judge_config, judgment, request_body, world,
)
from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, build_full_production_registry, make_identities
from trader.automation.evaluation_claims import EvaluationClaims
from trader.automation.backtest_judgments import JudgmentRefused
from trader.automation.forward_evidence import ForwardEvidenceRefused, NoDeploymentVersions
from trader.data.duckdb_store import DuckDBConnection
from trader.messaging.backtest_judge_surface import register_backtest_judge_surface
from trader.messaging.principals import SERVER_ACCEPTS, TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError
from trader.research.evaluation_request import evaluation_request_id

ROWS = {
    ("command", "claim_evaluation"): {"research"},
    ("query", "get_evaluation_claim"): {"research"},
    ("command", "update_evaluation_claim"): {"research"},
    ("query", "get_deployment_forward_evidence"): {"research"},
    ("command", "record_backtest_judgment"): {"ai_research"},
    ("query", "get_backtest_judgment"): {"research", "ai_research", "cli", "dashboard"},
}


def claim_body(body=None) -> dict:
    body = body or request_body()
    return {"request_id": evaluation_request_id(body), "body": body.model_dump()}


VALID_BODIES = {
    "claim_evaluation": claim_body,
    "get_evaluation_claim": lambda: {"request_id": ZERO},
    "update_evaluation_claim": lambda: {"request_id": ZERO, "state": "RUNNING"},
    "get_deployment_forward_evidence": lambda: {"deployment_version": ZERO},
    "record_backtest_judgment": lambda: judgment(ZERO, "SHADOW").model_dump(),
    "get_backtest_judgment": lambda: {"judgment_id": "jdg-00000001", "case_digest": None},
}


class HeldFirstReply:
    """The claim commits, then the first reply waits for ``release``: the caller times out after acceptance."""

    SAFETY_TIMEOUT_SECONDS = 30.0

    def __init__(self, claims):
        self._claims, self._first = claims, True
        self.release = threading.Event()

    def claim(self, *args, **kwargs):
        result = self._claims.claim(*args, **kwargs)
        if self._first:
            self._first = False
            self.release.wait(timeout=self.SAFETY_TIMEOUT_SECONDS)
        return result

    def __getattr__(self, name):
        return getattr(self._claims, name)


def serve(w, *, claims=None, acl=TRADER_ACL, judgments=None, forward_evidence=None) -> ServedStack:
    registry = TypedRpcRegistry(acl=acl, default_execution="thread")
    register_backtest_judge_surface(registry, claims=claims or w.claims, judgments=judgments or w.judgments,
                                    forward_evidence=forward_evidence or NoDeploymentVersions())
    return ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, make_identities())


@pytest.fixture
def w(tmp_path):
    return world(tmp_path)


@pytest.fixture
def served(w):
    stack = serve(w)
    yield stack
    stack.close()


def test_every_method_is_refused_for_every_caller_outside_its_row(served):            # review focus 5
    for (role, method), allowed in ROWS.items():
        assert TRADER_ACL[(role, method)] == allowed
        for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, role=role).call(method, {}, dict)
            assert exc.value.code == "PERMISSION_DENIED", (principal, method)


def test_each_handler_refuses_a_wrong_caller_even_behind_an_open_allow_list(w):      # review focus 5
    stack = serve(w, acl=ALLOW_ALL)
    try:
        for (role, method), allowed in ROWS.items():
            for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
                with pytest.raises(TypedRpcRemoteError) as exc:
                    stack.client(principal, role=role).call(method, VALID_BODIES[method](), dict)
                assert exc.value.code == "PERMISSION_DENIED", (principal, method)
    finally:
        stack.close()


def test_research_claims_reads_back_and_moves_the_claim_forward(served):
    command, query = served.client("research", role="command"), served.client("research", role="query")
    reply = command.call("claim_evaluation", claim_body(), dict)
    assert (reply["status"], reply["claim"]["state"], reply["claim"]["ny_day"]) == ("ACCEPTED", "QUEUED", "2026-10-08")
    request_id = reply["claim"]["request_id"]
    assert command.call("update_evaluation_claim", {"request_id": request_id, "state": "RUNNING"}, dict)["status"] \
        == "UPDATED"
    assert query.call("get_evaluation_claim", {"request_id": request_id}, dict)["claim"]["state"] == "RUNNING"
    assert command.call("update_evaluation_claim", {"request_id": request_id, "state": "RUNNING"}, dict)["status"] \
        == "UNCHANGED"


def test_a_conflict_is_a_refused_reply_not_an_rpc_error(served):
    command = served.client("research", role="command")
    command.call("claim_evaluation", claim_body(), dict)
    other = {**claim_body(), "body": request_body(bar_size="1 min").model_dump()}
    reply = command.call("claim_evaluation", other, dict)
    assert (reply["status"], reply["code"], reply["claim"]) == ("REFUSED", "EVALUATION_REQUEST_CONFLICT", None)


def test_two_concurrent_claims_for_the_last_slot_over_rpc_accept_exactly_one(tmp_path):    # review focus 1
    w = world(tmp_path, evaluations_per_day=2)
    stack = serve(w)
    try:
        stack.client("research", role="command").call(
            "claim_evaluation", claim_body(request_body(research_day="2026-10-01")), dict)
        clients = [stack.client("research", role="command") for _ in range(2)]
        bodies = [claim_body(request_body(research_day=day)) for day in ("2026-10-02", "2026-10-03")]
        barrier, outcomes = threading.Barrier(2), []

        def attempt(client, body):
            barrier.wait()
            reply = client.call("claim_evaluation", body, dict)
            outcomes.append(reply["code"] or reply["status"])

        threads = [threading.Thread(target=attempt, args=pair) for pair in zip(clients, bodies)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(outcomes) == ["ACCEPTED", "EVALUATION_LIMIT_REACHED"] and count_claims(w.db) == 2
    finally:
        stack.close()


def test_a_lost_reply_after_acceptance_is_read_back_and_never_takes_a_second_slot(tmp_path):    # review focus 2
    w = world(tmp_path, evaluations_per_day=1)
    held = HeldFirstReply(w.claims)
    stack = serve(w, claims=held)
    try:
        body = claim_body()
        with pytest.raises(TimeoutError):
            stack.client("research", role="command", timeout=0.5).call("claim_evaluation", body, dict)
        found = stack.client("research", role="query").call("get_evaluation_claim",
                                                            {"request_id": body["request_id"]}, dict)
        assert found["found"] and found["claim"]["state"] == "QUEUED"
        held.release.set()
        retry = stack.client("research", role="command").call("claim_evaluation", body, dict)
        assert (retry["status"], retry["claim"]["request_id"]) == ("EXISTING", body["request_id"])
        assert count_claims(w.db) == 1
    finally:
        held.release.set()
        stack.close()


def test_a_restarted_trader_serves_the_same_claim(tmp_path, w):
    body = claim_body()
    first = serve(w)
    try:
        first.client("research", role="command").call("claim_evaluation", body, dict)
    finally:
        first.close()
    reopened = EvaluationClaims(DuckDBConnection(str(tmp_path / "journal.duckdb")), config=judge_config(), now=w.clock)
    second = serve(w, claims=reopened)
    try:
        found = second.client("research", role="query").call("get_evaluation_claim",
                                                              {"request_id": body["request_id"]}, dict)
        assert found["claim"]["state"] == "QUEUED"
        assert second.client("research", role="command").call("claim_evaluation", body, dict)["status"] == "EXISTING"
    finally:
        second.close()


def test_ai_research_records_and_every_reader_reads_the_judgment(served, w):
    reply = served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(finished(w), "DEPLOY").model_dump(), dict)
    assert reply["status"] == "RECORDED"
    for reader in ("research", "ai_research", "cli", "dashboard"):
        got = served.client(reader, role="query").call(
            "get_backtest_judgment", {"judgment_id": "jdg-00000001", "case_digest": None}, dict)
        assert got["found"] and got["judgment"]["verdict"] == "DEPLOY"
        assert got["judgment"]["body"]["jev_model"] == "openrouter/jev-1"


def test_a_judgment_is_read_by_exactly_one_of_its_id_or_its_case(served, w):          # Plan 3 shadow discovery
    case = finished(w)
    served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(case, "SHADOW").model_dump(), dict)
    reader = served.client("research", role="query")
    by_case = reader.call("get_backtest_judgment", {"judgment_id": None, "case_digest": case}, dict)
    assert by_case["found"] and by_case["judgment"]["judgment_id"] == "jdg-00000001"
    unknown = reader.call("get_backtest_judgment", {"judgment_id": None, "case_digest": "sha256:" + "e" * 64}, dict)
    assert unknown == {"found": False, "judgment": None}
    for both_or_none in ({"judgment_id": "jdg-00000001", "case_digest": case},
                         {"judgment_id": None, "case_digest": None}):
        with pytest.raises(TypedRpcRemoteError) as exc:
            reader.call("get_backtest_judgment", both_or_none, dict)
        assert exc.value.code == "VALIDATION_ERROR"


def test_a_tampered_judgment_is_an_rpc_error_not_a_missing_row(served, w):
    served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(finished(w), "SHADOW").model_dump(), dict)
    w.db.execute("UPDATE backtest_judgments SET strategy_key = 'strategies/x.py:X' WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("cli", role="query").call("get_backtest_judgment",
                                                {"judgment_id": "jdg-00000001", "case_digest": None}, dict)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_a_retried_record_of_a_tampered_judgment_is_an_rpc_error_not_a_reply(served, w):
    recorder, case = served.client("ai_research", role="command"), finished(w)
    recorder.call("record_backtest_judgment", judgment(case, "SHADOW").model_dump(), dict)
    w.db.execute("UPDATE backtest_judgments SET strategy_key = 'strategies/x.py:X' WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(TypedRpcRemoteError) as exc:
        recorder.call("record_backtest_judgment", judgment(case, "SHADOW").model_dump(), dict)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_a_judgment_read_by_case_digest_fails_loudly_when_tampered(served, w):
    case = finished(w)
    served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(case, "SHADOW").model_dump(), dict)
    w.db.execute("UPDATE backtest_judgments SET strategy_key = 'strategies/x.py:X' WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("research", role="query").call("get_backtest_judgment",
                                                     {"judgment_id": None, "case_digest": case}, dict)
    assert exc.value.code == "JUDGMENT_TAMPERED"


@pytest.mark.parametrize("state", ["QUEUED", "EXPIRED", "running", "", "DONE "])
def test_a_claim_update_with_a_state_a_caller_may_not_set_is_a_validation_error(served, state):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("research", role="command").call(
            "update_evaluation_claim", {"request_id": ZERO, "state": state}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_forward_evidence_is_refused_until_versions_exist(served):
    reply = served.client("research", role="query").call(
        "get_deployment_forward_evidence", {"deployment_version": ZERO}, dict)
    assert (reply["status"], reply["code"], reply["evidence"]) == ("REFUSED", "DEPLOYMENT_VERSION_UNKNOWN", None)


class RefusingForwardEvidence:
    def __init__(self, code):
        self._code = code

    def read(self, deployment_version):
        raise ForwardEvidenceRefused(self._code, "a sealed record is damaged")


class RefusingJudgments:
    def __init__(self, code):
        self._code = code

    def record(self, parsed):
        raise JudgmentRefused(self._code, "a sealed record is damaged")


@pytest.mark.parametrize("code", ["FORWARD_EVIDENCE_TAMPERED", "DEPLOYMENT_VERSION_TAMPERED", "JUDGMENT_TAMPERED"])
def test_a_tampered_forward_evidence_read_is_a_named_rpc_error_not_a_refused_body(w, code):
    stack = serve(w, forward_evidence=RefusingForwardEvidence(code))
    try:
        with pytest.raises(TypedRpcRemoteError) as exc:
            stack.client("research", role="query").call(
                "get_deployment_forward_evidence", {"deployment_version": ZERO}, dict)
        assert exc.value.code == code
    finally:
        stack.close()


def test_a_business_refusal_of_forward_evidence_stays_a_refused_body(w):
    stack = serve(w, forward_evidence=RefusingForwardEvidence("RENEWAL_NOT_DUE"))
    try:
        reply = stack.client("research", role="query").call(
            "get_deployment_forward_evidence", {"deployment_version": ZERO}, dict)
        assert (reply["status"], reply["code"], reply["evidence"]) == ("REFUSED", "RENEWAL_NOT_DUE", None)
    finally:
        stack.close()


@pytest.mark.parametrize("code", ["FORWARD_EVIDENCE_TAMPERED", "DEPLOYMENT_VERSION_TAMPERED"])
def test_a_tampered_record_behind_a_judgment_is_a_named_rpc_error(w, code):
    stack = serve(w, judgments=RefusingJudgments(code))
    try:
        with pytest.raises(TypedRpcRemoteError) as exc:
            stack.client("ai_research", role="command").call(
                "record_backtest_judgment", judgment(ZERO, "SHADOW").model_dump(), dict)
        assert exc.value.code == code
    finally:
        stack.close()


def test_the_full_production_registry_registers_the_six_methods():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert set(ROWS) <= registered


def test_an_ai_paper_stack_without_its_judge_services_fails_loudly(w):
    from trader.messaging.production_api import _register_backtest_judge
    stack = SimpleNamespace(claims=None, judgments=w.judgments, forward_evidence=NoDeploymentVersions())
    with pytest.raises(RuntimeError, match="missing its backtest-judge services: claims"):
        _register_backtest_judge(TypedRpcRegistry(acl=TRADER_ACL), stack)
