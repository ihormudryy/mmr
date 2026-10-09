import pytest

from tests.ai.research.cases import (BASE, CASE, REQUEST, V1, attest_refused, attested, done, judgment_refused,
                                     recorded, refused, resolved, submitted, summary, unknown, version, view)
from trader.ai.research_wire import (AttestReply, EvaluationView, JudgmentReceipt, JudgmentView, Registered,
                                     RegisterRefused, SubmitReply, VersionReply, WireError, parse_registration,
                                     parse_reply)


def test_plan_3_replies_parse():
    finished = parse_reply(EvaluationView, "get_evaluation", done())
    assert (finished.found, finished.state, finished.case_digest, finished.summary.rules_passed) == (
        True, "DONE", CASE, True)
    assert finished.summary.points[0].metrics["expectancy_bps_2x"] == 5.5
    gone = parse_reply(EvaluationView, "get_evaluation", unknown())
    assert (gone.found, gone.state, gone.summary) == (False, None, None)
    accepted = parse_reply(SubmitReply, "submit_evaluation", submitted())
    assert (accepted.status, accepted.request_id, accepted.state) == ("ACCEPTED", REQUEST, "QUEUED")
    assert parse_reply(SubmitReply, "submit_evaluation", submitted("DUPLICATE", state="DONE")).status == "DUPLICATE"
    assert parse_reply(SubmitReply, "submit_evaluation", refused("CLAIM_UNKNOWN", retryable=True)).retryable
    assert parse_reply(AttestReply, "attest_from_judgment", attested()).binding.order_notional == 1900.0
    assert parse_reply(AttestReply, "attest_from_judgment", attested("DUPLICATE")).binding.conids[0] == 1001


@pytest.mark.parametrize("bad", [{**view(), "extra": 1}, {**view(), "status": "OK"}, {**view(), "found": "yes"},
                                 {**done(), "summary": {**done()["summary"], "rules_passed": "yes"}},
                                 {k: v for k, v in done().items() if k != "summary"}])
def test_unknown_shapes_fail_loudly_with_the_method_name(bad):
    with pytest.raises(WireError, match="get_evaluation"):
        parse_reply(EvaluationView, "get_evaluation", bad)


def test_registration_receipts():
    assert parse_registration(resolved()) == Registered(BASE, V1, "2026-11-05")
    rejected = {**resolved(), "state": "REJECTED", "outcome": None, "error_code": "DEPLOY_CAP_REACHED"}
    assert parse_registration(rejected) == RegisterRefused("DEPLOY_CAP_REACHED")
    unsaved = {**rejected, "error_code": "AUDIT_UNAVAILABLE", "retryable": True}      # no ledger row was kept
    assert parse_registration(unsaved) == RegisterRefused("AUDIT_UNAVAILABLE", retryable=True)
    assert parse_registration({**resolved(), "state": "SUBMITTED", "outcome": None}) is None
    with pytest.raises(WireError):
        parse_registration({"state": "RESOLVED", "outcome": {"digest": "x"}})


def test_a_parked_or_failed_request_reads_failed_with_no_case():
    failed = parse_reply(EvaluationView, "get_evaluation", view("FAILED"))
    assert (failed.state, failed.case_digest, failed.summary) == ("FAILED", None, None)
    assert parse_reply(SubmitReply, "submit_evaluation", submitted("DUPLICATE", state="FAILED")).state == "FAILED"


@pytest.mark.parametrize("stage,passed", [("COMPLETE", True), ("HOLDOUT_FAILED", False),
                                          ("PRE_HOLDOUT_FAILED", False), ("FAILED", False)])
def test_every_initial_stage_of_the_summary_parses(stage, passed):
    parsed = parse_reply(EvaluationView, "get_evaluation", done(rules_passed=passed, stage=stage)).summary
    assert (parsed.stage, parsed.rules_passed) == (stage, passed)
    assert (parsed.error is not None, parsed.metrics == {}) == (stage == "FAILED", stage == "FAILED")


def test_a_failed_case_has_no_points_and_a_null_metric_is_allowed():
    failed = parse_reply(EvaluationView, "get_evaluation", done(rules_passed=False, stage="FAILED")).summary
    assert (failed.points, failed.rule_results, failed.params, failed.selected_index) == ([], [], None, None)
    nan_metric = summary()
    nan_metric["metrics"] = {**nan_metric["metrics"], "selection_statistic": None}
    assert parse_reply(EvaluationView, "get_evaluation", {**done(), "summary": nan_metric}).summary.metrics[
        "selection_statistic"] is None


def test_a_whole_number_metric_is_still_a_number():
    whole = summary()
    whole["metrics"] = {**whole["metrics"], "expectancy_bps_1x": 0}
    whole["order_notional"] = 2000
    parsed = parse_reply(EvaluationView, "get_evaluation", {**done(), "summary": whole}).summary
    assert (parsed.metrics["expectancy_bps_1x"], parsed.order_notional) == (0.0, 2000.0)


@pytest.mark.parametrize("reply", [{**submitted(), "extra": 1}, {k: v for k, v in submitted().items() if k != "code"},
                                   {**submitted(), "state": "PARKED"}, {**submitted(), "request_id": "abc"},
                                   {**submitted(), "retryable": 0}])
def test_a_submit_reply_of_another_shape_fails_loudly(reply):
    with pytest.raises(WireError, match="submit_evaluation"):
        parse_reply(SubmitReply, "submit_evaluation", reply)


def test_a_principal_refusal_has_no_request_id_and_still_parses():
    reply = {**refused("PRINCIPAL_FORBIDDEN"), "request_id": None}
    assert parse_reply(SubmitReply, "submit_evaluation", reply).request_id is None


def test_nan_in_a_reply_fails_loudly():
    nan_summary = summary()
    nan_summary["metrics"] = {"expectancy_bps_1x": float("nan")}
    with pytest.raises(WireError, match="get_evaluation"):
        parse_reply(EvaluationView, "get_evaluation", {**done(), "summary": nan_summary})


def test_a_reply_that_is_not_a_mapping_fails_loudly():
    for bad in (None, [], "ok", 7):
        with pytest.raises(WireError, match="attest_from_judgment"):
            parse_reply(AttestReply, "attest_from_judgment", bad)


@pytest.mark.parametrize("bad", [{**attested(), "binding": {**attested()["binding"], "order_notional": 0}},
                                 {**attested(), "binding": {**attested()["binding"], "conids": []}},
                                 {**attested(), "binding": {**attested()["binding"], "file_hash": "x"}},
                                 {**attested(), "bundle_digest": "bundle"},
                                 {**attested(), "status": "OK"}])
def test_an_attest_reply_outside_the_bundle_binding_fails_loudly(bad):
    with pytest.raises(WireError, match="attest_from_judgment"):
        parse_reply(AttestReply, "attest_from_judgment", bad)


def test_an_attest_refusal_carries_no_binding_and_may_be_retryable():
    reply = parse_reply(AttestReply, "attest_from_judgment", attest_refused("ATTEST_EXPORT_FAILED", retryable=True))
    assert (reply.status, reply.binding, reply.retryable) == ("REFUSED", None, True)


def test_judgment_receipts():
    body = {"judgment_id": "jdg-00000001", "verdict": "REJECT"}
    receipt = parse_reply(JudgmentReceipt, "record_backtest_judgment", recorded(body))
    assert (receipt.status, receipt.verdict, receipt.cooldown_until_session) == ("RECORDED", "REJECT", "2026-10-22")
    assert parse_reply(JudgmentReceipt, "record_backtest_judgment", recorded(body, "EXISTING")).status == "EXISTING"
    refusal = parse_reply(JudgmentReceipt, "record_backtest_judgment",
                          judgment_refused("jdg-00000001", "JUDGMENT_CONFLICT"))
    assert (refusal.status, refusal.code, refusal.verdict) == ("REFUSED", "JUDGMENT_CONFLICT", None)
    with pytest.raises(WireError, match="record_backtest_judgment"):
        parse_reply(JudgmentReceipt, "record_backtest_judgment", {**recorded(body), "extra": 1})
    with pytest.raises(WireError, match="record_backtest_judgment"):
        parse_reply(JudgmentReceipt, "record_backtest_judgment",
                    {k: v for k, v in recorded(body).items() if k != "verdict"})


@pytest.mark.parametrize("state", ["ACTIVE", "EXPIRED", "WITHDRAWN", "ENDED"])
def test_version_replies(state):
    reply = parse_reply(VersionReply, "get_ai_deployment_version", version(state))
    assert (reply.found, reply.version.state, reply.version.expiry_session) == (True, state, "2026-11-05")
    assert parse_reply(VersionReply, "get_ai_deployment_version", {"found": False, "version": None}).version is None


@pytest.mark.parametrize("state", ["NOT_STARTED", "SUPERSEDED", "active"])
def test_a_version_state_the_trader_never_sends_fails_loudly(state):
    with pytest.raises(WireError, match="get_ai_deployment_version"):
        parse_reply(VersionReply, "get_ai_deployment_version", version(state))


def test_a_judgment_view_is_found_or_not():
    gone = parse_reply(JudgmentView, "get_backtest_judgment", {"found": False, "judgment": None})
    assert (gone.found, gone.judgment) == (False, None)
    with pytest.raises(WireError, match="get_backtest_judgment"):
        parse_reply(JudgmentView, "get_backtest_judgment", {"found": True})


def test_a_refusal_receipt_without_a_code_is_named_not_swallowed():
    rejected = {**resolved(), "state": "REJECTED", "outcome": None, "error_code": None}
    assert parse_registration(rejected) == RegisterRefused("REJECTED_WITHOUT_CODE")


@pytest.mark.parametrize("receipt", [None, [], "RESOLVED", {"outcome": {}}])
def test_a_receipt_without_a_state_fails_loudly(receipt):
    with pytest.raises(WireError, match="register_ai_deployment"):
        parse_registration(receipt)
