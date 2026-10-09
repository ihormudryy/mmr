"""The strict wire models read what the BUILT research servers send, not what the plan text says.

Each reply below is produced by the real service code (EvaluationService, JudgmentAttest, case_builder), through
the registry handlers where one exists. cases.py is checked against the same builders so it cannot drift.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.ai.research import cases
from tests.ai.runtime.fakes import FakeSocket
from tests.research.case_fixtures import complete_result
from tests.rpc_identity_fixtures import ServedStack, make_identities
from tests.research.service_fakes import AI, CLI
from tests.research.test_case_builder import STAGE_CASES
from tests.research.test_evaluation_service import KEY, NOW, case_of, world  # noqa: F401
from tests.research.test_evaluation_service import request as submit_body
from trader.ai.rpc_clients import AiRpcClients
from trader.ai.research_wire import AttestReply, CaseSummary, EvaluationView, SubmitReply, parse_reply
from trader.research.case_builder import evaluation_summary
from trader.research.evaluation_service import _submit_reply
from trader.research.judgment_attest import JudgmentAttest
from trader.research.research_surface import build_research_registry


def real_summary(stage):
    return evaluation_summary(STAGE_CASES[stage](), order_notional=1900.0)


def call(registry, role, method, body, caller):
    registration = registry.resolve(role, method)
    return registration.handler(registration.request_model.model_validate(body), caller)


def registry_of(trader, service):
    attest = JudgmentAttest(research_db=None, store=None, trader=trader, signer=SimpleNamespace(),
                            artifacts_root="/nonexistent", repo_root="/nonexistent", is_paper=lambda: False,
                            now=lambda: NOW)
    return build_research_registry(evaluations=service, attest=attest)


# -- the case summary, every stage the builder writes ----------------------------------------------------

@pytest.mark.parametrize("stage", sorted(STAGE_CASES))
def test_the_real_summary_of_every_stage_parses(stage):
    parsed = CaseSummary.model_validate_json(json.dumps(real_summary(stage)))
    assert parsed.stage == stage and parsed.kind == "INITIAL" and parsed.order_notional == 1900.0


def test_the_complete_summary_carries_what_jev_judges():
    parsed = CaseSummary.model_validate(real_summary("COMPLETE"))
    assert (parsed.rules_passed, parsed.holdout_passed, parsed.eligibility) == (True, True, "PAPER_ELIGIBLE")
    assert parsed.params == {"ENTRY_MINUTE": 600} and parsed.selected_index == 0
    assert (parsed.strategy_trials, parsed.prior_holdouts, parsed.previously_revealed_sessions) == (3, 1, 1)
    assert parsed.points[0].metrics["expectancy_bps_2x"] == 5.5 and parsed.rule_results[0].point == 0


def test_a_pre_holdout_failure_summary_has_no_selected_point_and_null_observations():
    parsed = CaseSummary.model_validate(real_summary("PRE_HOLDOUT_FAILED"))
    assert (parsed.rules_passed, parsed.params, parsed.selected_index, parsed.holdout_passed) == (False, None, None,
                                                                                                None)
    assert parsed.points[0].pre_holdout_passed is False and parsed.metrics["selection_statistic"] == 0.4


def test_the_failed_summary_has_empty_collections_and_the_error():
    parsed = CaseSummary.model_validate(real_summary("FAILED"))
    assert (parsed.points, parsed.rule_results, parsed.metrics) == ([], [], {})
    assert parsed.error == cases.FAILED_ERROR and parsed.strategy_trials == 0


@pytest.mark.parametrize("stage", ["COMPLETE", "HOLDOUT_FAILED", "PRE_HOLDOUT_FAILED", "FAILED"])
def test_cases_py_has_exactly_the_keys_of_the_real_builder(stage):
    handwritten = cases.summary(rules_passed=stage == "COMPLETE", stage=stage)
    real = real_summary(stage)
    assert set(handwritten) == set(real)
    if real["points"]:
        assert set(handwritten["points"][0]) == set(real["points"][0])
        assert set(handwritten["rule_results"][0]) == set(real["rule_results"][0])
    assert set(handwritten["metrics"]) == set(real["metrics"])
    assert (handwritten["error"] is None) == (real["error"] is None)
    CaseSummary.model_validate(handwritten)


def test_cases_py_submit_replies_have_the_keys_of_the_real_builder():
    assert set(cases.submitted()) == set(cases.refused("X")) == set(_submit_reply("ACCEPTED"))


# -- submit_evaluation and get_evaluation through the real service ---------------------------------------

def test_the_real_submit_replies_parse(world):  # noqa: F811
    service = world.service()
    registry = registry_of(world.trader, service)
    accepted = parse_reply(SubmitReply, "submit_evaluation",
                           call(registry, "command", "submit_evaluation", submit_body(), AI))
    assert (accepted.status, accepted.state, accepted.code, accepted.retryable) == ("ACCEPTED", "QUEUED", None, False)
    again = parse_reply(SubmitReply, "submit_evaluation",
                        call(registry, "command", "submit_evaluation", submit_body(), AI))
    assert (again.status, again.request_id, again.state) == ("DUPLICATE", accepted.request_id, "QUEUED")
    renewal = {"kind": "RENEWAL", "prior_version_digest": cases.V1}
    refusal = parse_reply(SubmitReply, "submit_evaluation", call(registry, "command", "submit_evaluation", renewal, AI))
    assert (refusal.status, refusal.code, refusal.request_id, refusal.state) == (
        "REFUSED", "RENEWAL_NOT_SUPPORTED", None, None)
    forbidden = parse_reply(SubmitReply, "submit_evaluation",
                            call(registry, "command", "submit_evaluation", submit_body(), CLI))
    assert (forbidden.status, forbidden.code) == ("REFUSED", "PRINCIPAL_FORBIDDEN")


def test_the_real_refusals_of_a_claim_parse(world):  # noqa: F811
    world.trader.cooling.add(KEY)
    cooling = parse_reply(SubmitReply, "submit_evaluation", world.service().submit(submit_body(), AI))
    assert (cooling.status, cooling.code, cooling.retryable) == ("REFUSED", "FAMILY_COOLING_DOWN", False)
    assert cooling.request_id is not None and cooling.state is None


def test_an_unreachable_trader_is_a_retryable_refusal_on_the_wire(world, monkeypatch):  # noqa: F811
    from trader.research.trader_port import TraderUnavailable

    def down(*args, **kwargs):
        raise TraderUnavailable("no route to the trader")
    monkeypatch.setattr(world.trader, "claim", down)
    monkeypatch.setattr(world.trader, "claim_readback", down)
    reply = parse_reply(SubmitReply, "submit_evaluation", world.service().submit(submit_body(), AI))
    assert (reply.status, reply.code, reply.retryable) == ("REFUSED", "CLAIM_UNKNOWN", True)


def test_a_full_queue_is_a_retryable_refusal_on_the_wire(world):  # noqa: F811
    service = world.service()
    for minute in (600, 615):
        service.submit(submit_body(cohort=[{"ENTRY_MINUTE": minute}]), AI)
    full = parse_reply(SubmitReply, "submit_evaluation",
                       service.submit(submit_body(cohort=[{"ENTRY_MINUTE": 630}]), AI))
    assert (full.status, full.code, full.retryable) == ("REFUSED", "QUEUE_FULL", True)


def test_the_real_get_replies_parse_for_every_state(world):  # noqa: F811
    service = world.service()
    registry = registry_of(world.trader, service)
    unknown = parse_reply(EvaluationView, "get_evaluation",
                          call(registry, "query", "get_evaluation", {"request_id": cases.REQUEST}, AI))
    assert (unknown.found, unknown.state, unknown.case_digest, unknown.summary) == (False, None, None, None)
    request_id = service.submit(submit_body(), AI)["request_id"]
    queued = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, AI))
    assert (queued.found, queued.state, queued.summary) == (True, "QUEUED", None)
    assert service.run_next() is True
    finished = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, CLI))
    assert (finished.state, finished.summary.stage, finished.summary.rules_passed) == (
        "DONE", "PRE_HOLDOUT_FAILED", False)
    assert case_of(world, service.get(request_id, CLI)).stage == "PRE_HOLDOUT_FAILED"
    assert set(cases.view()) == set(service.get(request_id, CLI)) == set(cases.unknown())
    stranger = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, SimpleNamespace(
        principal="strategy")))
    assert stranger.found is False


def test_a_withheld_case_reads_running_and_a_parked_request_reads_failed(world):  # noqa: F811
    service = world.service()
    request_id = service.submit(submit_body(), AI)["request_id"]
    world.db.execute("UPDATE research_requests SET pending_report = 'DONE' WHERE request_id = ?", [request_id])
    withheld = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, AI))
    assert (withheld.state, withheld.case_digest, withheld.summary) == ("RUNNING", None, None)
    world.db.execute("UPDATE research_requests SET pending_report = NULL, state = 'PARKED' WHERE request_id = ?",
                     [request_id])
    parked = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, AI))
    assert (parked.state, parked.case_digest, parked.summary) == ("FAILED", None, None)
    again = parse_reply(SubmitReply, "submit_evaluation", service.submit(submit_body(), AI))
    assert (again.status, again.state) == ("DUPLICATE", "FAILED")


def test_a_failed_evaluation_is_a_done_failed_case_with_the_error_in_its_summary(world):  # noqa: F811
    def boom(spec):
        raise RuntimeError("no bars for the cohort")
    service = world.service(evaluate=boom)
    request_id = service.submit(submit_body(), AI)["request_id"]
    assert service.run_next() is True
    failed = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, AI))
    assert failed.state == "FAILED" and failed.summary.stage == "FAILED"
    assert failed.summary.error.startswith("RuntimeError") and failed.summary.points == []


@pytest.mark.parametrize("holdout_passed,stage,deployable", [(True, "COMPLETE", True),
                                                             (False, "HOLDOUT_FAILED", False)])
def test_a_holdout_result_arrives_in_the_summary(world, holdout_passed, stage, deployable):  # noqa: F811
    service = world.service(evaluate=lambda spec: complete_result(spec.cohort, holdout_passed=holdout_passed))
    request_id = service.submit(submit_body(), AI)["request_id"]
    assert service.run_next() is True
    done = parse_reply(EvaluationView, "get_evaluation", service.get(request_id, AI))
    assert (done.state, done.summary.stage, done.summary.rules_passed) == ("DONE", stage, deployable)
    assert done.summary.holdout_passed is holdout_passed and done.summary.params == {"ENTRY_MINUTE": 600}


# -- attest_from_judgment refusals through the real service ----------------------------------------------

def test_the_real_attest_refusals_parse(world):  # noqa: F811
    registry = registry_of(world.trader, world.service())
    body = {"judgment_id": "jdg-00000001"}
    forbidden = parse_reply(AttestReply, "attest_from_judgment", call(registry, "command", "attest_from_judgment",
                                                                      body, CLI))
    assert (forbidden.status, forbidden.code, forbidden.binding, forbidden.retryable) == (
        "REFUSED", "PRINCIPAL_FORBIDDEN", None, False)
    not_paper = parse_reply(AttestReply, "attest_from_judgment", call(registry, "command", "attest_from_judgment",
                                                                      body, AI))
    assert (not_paper.status, not_paper.code, not_paper.bundle_digest) == ("REFUSED", "ACCOUNT_NOT_PAPER", None)


# -- the same replies over the signed wire, through the lab client ---------------------------------------

def test_the_lab_client_reads_the_real_replies_over_signed_rpc(world):  # noqa: F811
    service = world.service()
    registry = registry_of(world.trader, service)
    served = ServedStack({("research", "command"): registry, ("research", "query"): registry}, make_identities())
    try:
        clients = AiRpcClients.from_sockets(
            supervisor_command=FakeSocket(), supervisor_query=FakeSocket(), supervisor_discovery=FakeSocket(),
            research_command=FakeSocket(), research_query=FakeSocket(), timeout=5.0,
            lab_command=served.client("ai_research", server="research", role="command"),
            lab_query=served.client("ai_research", server="research", role="query"))

        async def exchange():
            submitted = parse_reply(SubmitReply, "submit_evaluation",
                                    await clients.lab.call("submit_evaluation", submit_body()))
            queued = parse_reply(EvaluationView, "get_evaluation",
                                 await clients.lab.call("get_evaluation", {"request_id": submitted.request_id}))
            attest = parse_reply(AttestReply, "attest_from_judgment",
                                 await clients.lab.call("attest_from_judgment", {"judgment_id": "jdg-00000001"}))
            return submitted, queued, attest
        submitted, queued, attest = asyncio.run(exchange())
        assert (submitted.status, submitted.state, queued.state, queued.summary) == ("ACCEPTED", "QUEUED", "QUEUED",
                                                                                    None)
        assert (attest.status, attest.code) == ("REFUSED", "ACCOUNT_NOT_PAPER")
        assert service.run_next() is True
        finished = parse_reply(EvaluationView, "get_evaluation", asyncio.run(
            clients.lab.call("get_evaluation", {"request_id": submitted.request_id})))
        assert (finished.state, finished.summary.stage) == ("DONE", "PRE_HOLDOUT_FAILED")
    finally:
        served.close()
