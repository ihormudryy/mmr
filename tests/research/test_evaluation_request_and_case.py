"""SP2c Plan 1 Task 2: the canonical evaluation request and the signed evaluation case (spec 5.1)."""
from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from tests.automation.backtest_judge_fixtures import (
    ZERO, case_body, case_keys, failed_results, make_case, renewal_case_body, request_body, write_signed_body,
)
from trader.research.evaluation_case import (
    FULL_MENU, NO_DEPLOY_MENU, CaseRefused, EvaluationCase, case_path, initial_deploy_allowed, load_case_verify_keys,
    load_verified_case, offered_menu, write_evaluation_case,
)
from trader.research.evaluation_request import evaluation_request_id
from trader.research.signing import AttestationSigner


def refusal(fn) -> str:
    with pytest.raises(CaseRefused) as exc:
        fn()
    return exc.value.code


def test_the_request_id_is_the_digest_of_the_canonical_body():
    body = request_body()
    assert evaluation_request_id(body) == evaluation_request_id(request_body())
    assert evaluation_request_id(body).startswith("sha256:") and len(evaluation_request_id(body)) == 71
    assert evaluation_request_id(request_body(research_day="2026-10-09")) != evaluation_request_id(body)
    assert evaluation_request_id(request_body(cohort=[{"B": 1, "A": 2}])) == \
        evaluation_request_id(request_body(cohort=[{"A": 2, "B": 1}]))


@pytest.mark.parametrize("changes", [
    {"conids": [272093, 265598]}, {"conids": [265598, 265598]}, {"conids": [True]}, {"conids": []},
    {"cohort": []}, {"cohort": [{"A": i} for i in range(11)]}, {"cohort": [{"A": 1}, {"A": 1}]},
    {"cohort": [{"range": 1}]}, {"cohort": [{"A": float("nan")}]}, {"cohort": [{"A": [1, 2]}]},
    {"bar_size": "1 hour"}, {"bar_size": "soon"}, {"strategy_key": "strategies/x.py"},
    {"research_day": "08/10/2026"}, {"research_day": "2026-W41-4"}, {"extra": 1},
])
def test_a_request_with_two_spellings_or_bad_values_is_refused(changes):
    with pytest.raises(ValidationError):
        request_body(**changes)


def test_a_written_case_loads_back_verified(tmp_path):
    keys = case_keys(tmp_path)
    case = make_case()
    digest = write_evaluation_case(keys.cases_dir, case, keys.signer)
    assert case_path(keys.cases_dir, digest).name == f"sha256_{digest[7:]}.json"
    assert load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir)) == case
    assert write_evaluation_case(keys.cases_dir, case, keys.signer) == digest       # same bytes: a no-op


def test_a_changed_case_body_fails_its_digest(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), keys.signer)
    path = case_path(keys.cases_dir, digest)
    envelope = json.loads(path.read_text())
    envelope["case"]["bar_size"] = "1 min"
    path.write_text(json.dumps(envelope))
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_DIGEST_MISMATCH"


def test_a_case_signed_by_an_unknown_key_is_refused(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), AttestationSigner.generate())
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_KEY_UNKNOWN"


def test_a_foreign_signature_under_a_trusted_key_id_is_refused(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), AttestationSigner.generate())
    path = case_path(keys.cases_dir, digest)
    envelope = json.loads(path.read_text())
    envelope["public_key_id"] = keys.signer.public_key_id
    path.write_text(json.dumps(envelope))
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_SIGNATURE_INVALID"


def test_a_missing_or_symlinked_case_is_not_found(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), keys.signer)
    path = case_path(keys.cases_dir, digest)
    elsewhere = tmp_path / "elsewhere.json"
    os.replace(path, elsewhere)
    path.symlink_to(elsewhere)
    verify = load_case_verify_keys(keys.verify_dir)
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, verify)) == "CASE_NOT_FOUND"
    assert refusal(lambda: load_verified_case(keys.cases_dir, ZERO, verify)) == "CASE_NOT_FOUND"
    assert refusal(lambda: load_verified_case(keys.cases_dir, "../x", verify)) == "CASE_DIGEST_INVALID"


def test_no_verify_keys_is_a_refusal_not_a_crash(tmp_path):
    assert refusal(lambda: load_case_verify_keys(tmp_path / "nothing")) == "CASE_VERIFY_KEYS_MISSING"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "broken.pem").write_text("not a key")
    assert refusal(lambda: load_case_verify_keys(bad)) == "CASE_VERIFY_KEYS_UNREADABLE"


def test_only_a_complete_case_with_every_paper_v1_rule_passed_offers_deploy():
    assert offered_menu(make_case()) == FULL_MENU
    assert offered_menu(make_case(final_rule_results=failed_results())) == NO_DEPLOY_MENU
    short = case_body(request_body())
    short["final_rule_results"].pop()
    selected_point(short)["rules"].pop()
    assert offered_menu(EvaluationCase.model_validate(short)) == NO_DEPLOY_MENU
    assert offered_menu(make_case(decision_state="CANDIDATE")) == NO_DEPLOY_MENU
    assert offered_menu(make_case(stage="HOLDOUT_FAILED", decision_state="CANDIDATE")) == NO_DEPLOY_MENU
    assert offered_menu(make_case(stage="PRE_HOLDOUT_FAILED")) == NO_DEPLOY_MENU
    assert offered_menu(EvaluationCase.model_validate(renewal_case_body())) == FULL_MENU
    incomplete = renewal_case_body(renewal={"prior_deployment_version": "sha256:" + "b" * 64,
                                            "forward_sessions": 20, "incomplete_sessions": 1})
    assert offered_menu(EvaluationCase.model_validate(incomplete)) == NO_DEPLOY_MENU


@pytest.mark.parametrize("changes", [
    {"stage": "PRE_HOLDOUT_FAILED", "artifact_id": "art-1"},  # a pre-holdout failure seals no artifact
    {"artifact_id": None},                                     # a sealed case names its artifact
    {"request_id": None},                                      # an INITIAL case names its claim
    {"renewal": {"prior_deployment_version": "sha256:" + "b" * 64, "forward_sessions": 1,
                 "incomplete_sessions": 0}},                   # only a RENEWAL has renewal facts
    {"created_at": "2026-10-08T21:30:00"},                     # naive time
    {"conids": [272093, 265598]},
    {"claim_day": "20261008"}, {"claim_day": "2026-W41-4"},   # one spelling of a day
])
def test_a_case_shape_that_contradicts_its_stage_is_refused(changes):
    with pytest.raises(ValidationError):
        make_case(**changes)


@pytest.mark.parametrize("changes", [
    {"stage": "COMPLETE", "holdout_passed": False}, {"stage": "COMPLETE", "holdout_passed": None},
    {"stage": "HOLDOUT_FAILED", "holdout_passed": True}, {"stage": "HOLDOUT_FAILED", "holdout_passed": None},
    {"stage": "PRE_HOLDOUT_FAILED", "holdout_passed": True}, {"stage": "FAILED", "holdout_passed": False},
])
def test_holdout_passed_must_agree_with_the_initial_stage(changes):
    with pytest.raises(ValidationError):
        make_case(**changes)


def test_a_renewal_case_has_no_holdout():
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate(renewal_case_body(holdout_passed=True))


@pytest.mark.parametrize("changes", [
    {"holdout_passed": False},                                            # COMPLETE with a failed holdout
    {"selected_params": {"RANGE_MINUTES": 99}},                           # never claimed
    {"cohort": [{"RANGE_MINUTES": 15}, {"RANGE_MINUTES": 15}]},          # a repeated point
    {"evidence": {"holdout": {"start": "2025-01-02", "end": "2025-12-31", "passed": False, "detail": "x"}}},
])
def test_a_signed_case_with_a_bad_shape_is_malformed_on_load(tmp_path, changes):
    keys = case_keys(tmp_path)
    digest = write_signed_body(keys, case_body(request_body(), **changes))
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_MALFORMED"


@pytest.mark.parametrize("stage", ["COMPLETE", "HOLDOUT_FAILED", "FAILED"])
def test_the_selected_params_must_be_one_of_the_claimed_cohort_points(stage):
    with pytest.raises(ValidationError, match="cohort"):
        make_case(stage=stage, selected_params={"RANGE_MINUTES": 99})
    with pytest.raises(ValidationError, match="cohort"):
        make_case(stage=stage, selected_params={"RANGE_MINUTES": 15.0})       # another spelling is another point
    assert make_case(stage=stage, selected_params={"RANGE_MINUTES": 30}).selected_params == {"RANGE_MINUTES": 30}


@pytest.mark.parametrize("stage", ["COMPLETE", "PRE_HOLDOUT_FAILED"])
def test_a_case_never_repeats_a_cohort_point(stage):
    with pytest.raises(ValidationError, match="distinct"):
        make_case(stage=stage, cohort=[{"RANGE_MINUTES": 15}, {"RANGE_MINUTES": 15}])


def test_a_failed_holdout_never_allows_a_deploy_even_when_every_rule_passed():
    assert initial_deploy_allowed(make_case())
    assert not initial_deploy_allowed(make_case().model_copy(update={"holdout_passed": False}))
    assert not initial_deploy_allowed(make_case().model_copy(update={"holdout_passed": None}))


def holdout(passed) -> dict:
    return {"start": "2025-01-02", "end": "2025-12-31", "passed": passed, "detail": "x"}


@pytest.mark.parametrize("stage,evidence", [
    ("COMPLETE", {}),                                       # no holdout key
    ("COMPLETE", {"holdout": None}),
    ("COMPLETE", {"holdout": holdout(1)}),                  # not a real bool
    ("COMPLETE", {"holdout": holdout("true")}),
    ("COMPLETE", {"holdout": {"start": "2025-01-02"}}),      # no passed
    ("COMPLETE", {"holdout": [True]}),
    ("COMPLETE", {"holdout": holdout(False)}),              # header True, evidence False
    ("HOLDOUT_FAILED", {"holdout": holdout(True)}),         # header False, evidence True
    ("HOLDOUT_FAILED", {"holdout": holdout(0)}),
    ("PRE_HOLDOUT_FAILED", {}),
    ("PRE_HOLDOUT_FAILED", {"holdout": holdout(False)}),    # no holdout ran
    ("FAILED", {"holdout": holdout(True)}),
])
def test_the_holdout_evidence_must_agree_with_the_header(stage, evidence):
    with pytest.raises(ValidationError, match="holdout"):
        make_case(stage=stage, evidence=evidence)


def with_holdout(stage: str, value) -> EvaluationCase:
    body = case_body(request_body(), stage=stage)
    body["evidence"]["holdout"] = value
    return EvaluationCase.model_validate(body)


def test_matching_holdout_evidence_is_accepted():
    assert with_holdout("COMPLETE", holdout(True)).holdout_passed is True
    assert with_holdout("HOLDOUT_FAILED", holdout(False)).holdout_passed is False
    assert make_case(stage="FAILED", evidence={"holdout": None}).holdout_passed is None


def test_a_renewal_case_carries_no_holdout_evidence():
    assert EvaluationCase.model_validate(renewal_case_body(evidence={})).kind == "RENEWAL"
    with pytest.raises(ValidationError, match="holdout"):
        EvaluationCase.model_validate(renewal_case_body(evidence={"holdout": holdout(True)}))


def selected_point(body: dict) -> dict:
    return body["evidence"]["points"][body["evidence"]["selected_index"]]


def complete_body(**changes) -> dict:
    return case_body(request_body(), **changes)


def point_failed_its_gate(body: dict) -> None:
    point = selected_point(body)
    point["pre_holdout_passed"] = False
    point["rules"][0]["passed"] = False


@pytest.mark.parametrize("damage", [
    pytest.param(lambda b: b["evidence"].update(selected_index=1), id="another-point"),
    pytest.param(lambda b: b["evidence"].update(selected_index=True), id="bool-index"),
    pytest.param(lambda b: b["evidence"].update(selected_index=-1), id="negative-index"),
    pytest.param(lambda b: b["evidence"].update(selected_index=7), id="index-out-of-range"),
    pytest.param(lambda b: b["evidence"].pop("selected_index"), id="no-index"),
    pytest.param(lambda b: b["evidence"].update(points={"0": selected_point(b)}), id="points-not-a-list"),
    pytest.param(lambda b: b["evidence"].update(points=[]), id="no-points"),
    pytest.param(lambda b: b["evidence"]["points"].__setitem__(0, "point"), id="point-not-a-dict"),
    pytest.param(lambda b: selected_point(b).update(index=1), id="point-index-mismatch"),
    pytest.param(lambda b: selected_point(b).update(index="0"), id="point-index-text"),
    pytest.param(lambda b: selected_point(b).update(params={"RANGE_MINUTES": 30}), id="params-mismatch"),
    pytest.param(lambda b: selected_point(b).update(params={"RANGE_MINUTES": 15.0}), id="params-respelled"),
    pytest.param(lambda b: selected_point(b).update(trial_id="trial-9"), id="trial-mismatch"),
    pytest.param(lambda b: selected_point(b).update(pre_holdout_passed=False), id="gate-failed"),
    pytest.param(lambda b: selected_point(b).update(pre_holdout_passed=1), id="gate-not-a-bool"),
    pytest.param(lambda b: selected_point(b)["rules"][0].update(passed=False), id="rule-failed"),
    pytest.param(lambda b: selected_point(b)["rules"][0].update(passed=1), id="rule-passed-not-a-bool"),
    pytest.param(lambda b: selected_point(b)["rules"][0].update(code="other"), id="rule-code-mismatch"),
    pytest.param(lambda b: selected_point(b)["rules"].pop(), id="rule-missing"),
    pytest.param(lambda b: selected_point(b)["rules"].append({"code": "extra", "passed": True}), id="rule-extra"),
    pytest.param(lambda b: selected_point(b).update(rules=["passed"]), id="rule-not-a-dict"),
    pytest.param(lambda b: selected_point(b).pop("rules"), id="no-rules"),
    pytest.param(point_failed_its_gate, id="gate-and-rule-failed"),
])
@pytest.mark.parametrize("stage", ["COMPLETE", "HOLDOUT_FAILED"])
def test_the_selected_point_evidence_must_agree_with_the_header(stage, damage):      # PR #93 round 1
    body = complete_body(stage=stage)
    damage(body)
    with pytest.raises(ValidationError, match="selected"):
        EvaluationCase.model_validate(body)


def test_a_signed_complete_case_whose_selected_point_failed_its_gate_is_malformed_on_load(tmp_path):
    keys = case_keys(tmp_path)
    body = complete_body()                       # header: COMPLETE, PAPER_ELIGIBLE, every rule passed
    point_failed_its_gate(body)
    digest = write_signed_body(keys, body)
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_MALFORMED"


def test_a_selected_point_carries_the_final_rules_even_when_one_failed():
    """Plan 3 gives the selected point the final decision: a failed holdout-stage rule is no contradiction."""
    for stage in ("COMPLETE", "HOLDOUT_FAILED"):
        case = make_case(stage=stage, decision_state="CANDIDATE", final_rule_results=failed_results())
        assert offered_menu(case) == NO_DEPLOY_MENU


def test_a_pre_holdout_failure_binds_its_selected_point_when_it_names_one():
    named = complete_body(stage="PRE_HOLDOUT_FAILED", selected_params={"RANGE_MINUTES": 30},
                          selected_trial_id="trial-2")
    assert EvaluationCase.model_validate(named).selected_params == {"RANGE_MINUTES": 30}
    selected_point(named)["pre_holdout_passed"] = True                       # it never reached the holdout
    with pytest.raises(ValidationError, match="selected"):
        EvaluationCase.model_validate(named)
    unnamed = complete_body(stage="PRE_HOLDOUT_FAILED")
    assert EvaluationCase.model_validate(unnamed).selected_params is None
    unnamed["evidence"]["selected_index"] = 0
    with pytest.raises(ValidationError, match="selected"):
        EvaluationCase.model_validate(unnamed)


def test_a_failed_evaluation_binds_no_selected_point():
    body = complete_body(stage="FAILED")
    body["evidence"].update(points=[], selected_index=None)
    assert EvaluationCase.model_validate(body).stage == "FAILED"
