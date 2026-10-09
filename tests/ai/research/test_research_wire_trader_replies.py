"""The strict wire models read what the BUILT trader sends: judgments, deployment versions, registration receipts.

Judgment replies come from the real BacktestJudgments store and its RPC handlers; version and registration
replies come from the real served trader stack (signed RPC, command ledger, version store).
"""
import datetime as dt

import pytest

from tests.ai.research import cases
from tests.automation.backtest_judge_fixtures import NOW, failed_results, finished, judgment, request_body, world
from tests.automation.judged_deployment import deploy_facts, install_seeded_judgments, seed_judged_deployment
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from trader.acceptance.scenario import AcceptanceSettings, deployment_record
from trader.ai.research_wire import (JudgmentReceipt, JudgmentView, Registered, RegisterRefused, VersionReply,
                                     WireError, parse_registration, parse_reply)
from trader.automation.ai_bundle_check import BundleFacts, BundleRefused
from trader.messaging.backtest_judge_surface import register_backtest_judge_surface
from trader.messaging.typed_rpc import RpcCaller, TypedRpcRegistry
from trader.research.evaluation_request import evaluation_request_id


def judge_handlers(w):
    registry = TypedRpcRegistry(acl=None)
    register_backtest_judge_surface(registry, claims=w.claims, judgments=w.judgments, forward_evidence=None)

    def call(method, body, principal="ai_research"):
        registration = next(r for r in registry._by_role_method.values() if r.method == method)
        parsed = registration.request_model.model_validate(body)
        return registration.handler(parsed, RpcCaller(principal=principal, on_behalf_of=None))
    return call


# -- record_backtest_judgment / get_backtest_judgment ------------------------------------------------------

def test_the_real_judgment_receipts_parse(tmp_path):
    w = world(tmp_path)
    digest = finished(w)
    request = judgment(digest, "DEPLOY")
    first = parse_reply(JudgmentReceipt, "record_backtest_judgment", w.judgments.record(request))
    assert (first.status, first.verdict, first.cooldown_until_session, first.retryable) == (
        "RECORDED", "DEPLOY", None, False)
    again = parse_reply(JudgmentReceipt, "record_backtest_judgment", w.judgments.record(request))
    assert (again.status, again.judgment_id) == ("EXISTING", "jdg-00000001")
    assert set(cases.recorded({"judgment_id": "j", "verdict": "DEPLOY"})) == set(w.judgments.record(request))


def test_a_real_reject_carries_its_cooldown_session(tmp_path):
    w = world(tmp_path)
    reject = parse_reply(JudgmentReceipt, "record_backtest_judgment",
                         w.judgments.record(judgment(finished(w), "REJECT")))
    assert reject.status == "RECORDED" and reject.verdict == "REJECT"
    assert dt.date.fromisoformat(reject.cooldown_until_session) > NOW.date()


def test_the_real_refusals_carry_null_verdict_and_cooldown(tmp_path):
    w = world(tmp_path)
    digest = finished(w, stage="HOLDOUT_FAILED", decision_state="CANDIDATE", final_rule_results=failed_results())
    refusal = w.judgments.record(judgment(digest, "DEPLOY"))
    parsed = parse_reply(JudgmentReceipt, "record_backtest_judgment", refusal)
    assert (parsed.status, parsed.code, parsed.verdict, parsed.cooldown_until_session) == (
        "REFUSED", "JUDGMENT_MENU_MISMATCH", None, None)
    assert set(cases.judgment_refused("j", "X")) == set(refusal)


def test_a_no_verdict_judgment_without_an_attempt_ref_is_recorded(tmp_path):
    w = world(tmp_path)
    request = judgment(finished(w), "NO_VERDICT", jev_attempt_ref=None)
    receipt = parse_reply(JudgmentReceipt, "record_backtest_judgment", w.judgments.record(request))
    assert (receipt.status, receipt.verdict) == ("RECORDED", "NO_VERDICT")


def test_the_real_judgment_read_by_id_and_by_case_parses(tmp_path):
    w = world(tmp_path)
    digest = finished(w)
    w.judgments.record(judgment(digest, "REJECT"))
    call = judge_handlers(w)
    by_id = parse_reply(JudgmentView, "get_backtest_judgment", call("get_backtest_judgment", {
        "judgment_id": "jdg-00000001", "case_digest": None}))
    by_case = parse_reply(JudgmentView, "get_backtest_judgment", call("get_backtest_judgment", {
        "judgment_id": None, "case_digest": digest}, principal="research"))
    assert by_id == by_case and by_id.found
    record = by_id.judgment
    assert (record.verdict, record.kind, record.case_digest, record.body["jev_model"]) == (
        "REJECT", "INITIAL", digest, "openrouter/jev-1")
    assert record.binding["strategy_file_hash"].startswith("sha256:") and record.cooldown_until_session is not None
    gone = parse_reply(JudgmentView, "get_backtest_judgment", call("get_backtest_judgment", {
        "judgment_id": "jdg-missing01", "case_digest": None}))
    assert (gone.found, gone.judgment) == (False, None)


def test_a_judgment_read_with_the_request_id_of_its_case_names_it(tmp_path):
    w = world(tmp_path)
    digest = finished(w)
    w.judgments.record(judgment(digest, "SHADOW"))
    view = parse_reply(JudgmentView, "get_backtest_judgment", judge_handlers(w)("get_backtest_judgment", {
        "judgment_id": "jdg-00000001", "case_digest": None}))
    assert view.judgment.request_id == evaluation_request_id(request_body())


# -- version reads and registration receipts through the served trader -----------------------------------

BUNDLE = "sha256:" + "b" * 64


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):  # noqa: F811
    seeded = install_seeded_judgments(monkeypatch)
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    stack.seeded = seeded
    yield stack
    stack.close()


def record():
    return {**deployment_record(AcceptanceSettings(run_id="r", account_id="DU1", strategy_bytes=b"x")),
            "evidence_ref": BUNDLE}


def reads(served, version):
    return parse_reply(VersionReply, "get_ai_deployment_version",
                       served.call("ai_research", "get_ai_deployment_version", {"version_digest": version}))


def test_the_real_version_reads_parse(served):
    _base, version = seed_judged_deployment(served.composed.stack.ai_paper, served.seeded, record(),
                                            today=served.now().date())
    active = reads(served, version)
    assert (active.found, active.version.state, active.version.kind, active.version.version_digest) == (
        True, "ACTIVE", "INITIAL", version)
    assert active.version.prior_version_digest is None and active.version.judgment_id == "jdg-seed-00000001"
    served.call("cli", "withdraw_ai_deployment", {"version_digest": version, "reason": "operator"})
    assert reads(served, version).version.state == "WITHDRAWN"
    unknown = reads(served, "sha256:" + "9" * 64)
    assert (unknown.found, unknown.version) == (False, None)
    assert set(cases.version()["version"]) == set(served.call(
        "ai_research", "get_ai_deployment_version", {"version_digest": version})["version"])


def test_a_version_whose_line_ended_reads_ended(served):
    _base, version = seed_judged_deployment(served.composed.stack.ai_paper, served.seeded, record(),
                                            today=served.now().date())
    served.seeded.end_line(version, "REJECT")
    assert reads(served, version).version.state == "ENDED"


def test_the_real_registration_refusal_parses(served):
    receipt = served.call("ai_research", "register_ai_deployment", {
        "judgment_id": "jdg-none", "bundle_digest": BUNDLE, "deployment": record()})
    assert parse_registration(receipt) == RegisterRefused("JUDGMENT_MISSING")


class OneBundle:
    """The bundle check of this test: it names the registrar's own binding facts."""

    def __init__(self, facts):
        self.facts = facts

    def manifest_artifact_id(self, digest):
        return self.facts.artifact_id

    def check(self, digest, *, artifact_id, now):
        if digest != self.facts.bundle_digest:
            raise BundleRefused("BUNDLE_MISSING", digest)
        return self.facts


def test_the_real_registration_receipt_parses_and_a_repeat_answers_the_same_version(served):
    rec = record()
    served.seeded.seed(deploy_facts(rec, "jdg-register-1"))
    served.composed.stack.ai_paper.registrar._bundles = OneBundle(BundleFacts(
        BUNDLE, "art-1", "fam-1", rec["strategy_path"], rec["class_name"], rec["strategy_digest"], rec["params"],
        tuple(sorted(rec["conids"])), rec["bar_size"], rec["evidence_order_notional"], "jev-model#jdg-register-1",
        "llm", served.now() + dt.timedelta(days=90)))
    body = {"judgment_id": "jdg-register-1", "bundle_digest": BUNDLE, "deployment": rec}
    receipt = served.call("ai_research", "register_ai_deployment", body)
    registered = parse_registration(receipt)
    assert isinstance(registered, Registered), receipt
    assert reads(served, registered.version_digest).version.base_digest == registered.base_digest
    assert reads(served, registered.version_digest).version.expiry_session == registered.expiry_session
    again = parse_registration(served.call("ai_research", "register_ai_deployment", body))
    assert again == registered
    assert set(cases.resolved()) == set(receipt) and set(cases.resolved()["outcome"]) == set(receipt["outcome"])


def test_a_receipt_that_is_still_in_flight_is_asked_again():
    receipt = {**cases.resolved(), "state": "SUBMITTED", "outcome": None}
    assert parse_registration(receipt) is None
    with pytest.raises(WireError):
        parse_registration({**cases.resolved(), "extra": 1})
