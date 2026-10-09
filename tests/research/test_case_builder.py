import datetime as dt

import pytest

from tests.research.case_fixtures import complete_result, pre_holdout_result
from tests.research.evaluation_fixtures import CONIDS
from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
from trader.automation.paper_materials import PaperMaterialsError, require_qualified_research_evidence
from trader.research.case_builder import build_failed_case, build_initial_case, evaluation_summary
from trader.research.cohort import CohortSpec
from trader.research.evaluation_case import (FULL_MENU, NO_DEPLOY_MENU, case_path, load_verified_case,
                                             offered_menu, write_evaluation_case)
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2024, 3, 29, 21, tzinfo=dt.timezone.utc)
ONE_POINT = ({"ENTRY_MINUTE": 600},)
TWO_POINTS = ({"ENTRY_MINUTE": 540}, {"ENTRY_MINUTE": 600})


def body_for(cohort):
    return EvaluationRequestBody.model_validate({"strategy_key": "strategies/time_of_day.py:TimeOfDay",
                                                 "cohort": [dict(p) for p in cohort], "conids": CONIDS,
                                                 "bar_size": "15 mins", "research_day": "2024-03-29"})


def spec_for(cohort):
    body = body_for(cohort)
    return CohortSpec(request_id=evaluation_request_id(body), body=body, strategy_key=body.strategy_key, base=None,
                      cohort=tuple(cohort), neighbours=(({"ENTRY_MINUTE": 480},),) * len(cohort),
                      file_hash="sha256:" + "b" * 64)


BODY, SPEC = body_for(ONE_POINT), spec_for(ONE_POINT)


def build(result, cohort=ONE_POINT):
    return build_initial_case(spec_for(cohort), "2024-03-29", result, created_at=NOW, warmup_sessions=5)


def failed_case():
    return build_failed_case(BODY, request_id=SPEC.request_id, claim_day="2024-03-29", file_hash=SPEC.file_hash,
                             error="EvaluationError: no bars", created_at=NOW, warmup_sessions=5)


def test_a_rule_failure_is_a_case_without_an_artifact_that_offers_shadow_or_reject():
    case = build(pre_holdout_result())
    assert case.stage == "PRE_HOLDOUT_FAILED" and case.artifact_id is None and case.final_rule_results == []
    assert case.holdout_passed is None and case.evidence["holdout"] is None
    assert case.selected_params is None and case.evidence["selected_index"] is None
    assert offered_menu(case) == NO_DEPLOY_MENU
    assert case.evidence["points"][0]["rules"][0]["observed"] is None         # NaN never reaches the bytes


def test_a_holdout_failure_names_the_decision_and_offers_no_deploy():
    case = build(complete_result(holdout_passed=False))
    assert case.stage == "HOLDOUT_FAILED" and case.eligibility_decision_digest == "d" * 64
    assert case.holdout_passed is False and evaluation_summary(case, order_notional=1900.0)["holdout_passed"] is False
    assert case.evidence["holdout"]["passed"] is False                          # one result, header and evidence
    assert [r.passed for r in case.final_rule_results] == [False] * len(case.final_rule_results)
    assert offered_menu(case) == NO_DEPLOY_MENU


def test_a_complete_passing_case_offers_deploy_and_its_summary_matches_the_menu():
    case = build(complete_result())
    assert case.stage == "COMPLETE" and case.holdout_passed is True and offered_menu(case) == FULL_MENU
    assert case.evidence["holdout"]["passed"] is True
    summary = evaluation_summary(case, order_notional=1900.0)
    assert summary["rules_passed"] is True and summary["holdout_passed"] is True
    assert summary["params"] == {"ENTRY_MINUTE": 600} and summary["prior_holdouts"] == 1
    assert summary["previously_revealed_sessions"] == 1 and summary["forward"] is None
    assert (summary["selected_index"], summary["error"]) == (0, None)
    point = summary["points"][0]
    assert point["pre_holdout_passed"] is True and point["metrics"]["expectancy_bps_2x"] == 5.5


def test_a_failure_after_the_claim_is_a_failed_case():
    case = failed_case()
    assert case.stage == "FAILED" and case.holdout_passed is None and case.evidence["error"] == "EvaluationError: no bars"
    assert "holdout" in case.evidence and case.evidence["holdout"] is None
    summary = evaluation_summary(case, order_notional=1900.0)
    assert summary["rules_passed"] is False and summary["error"] == "EvaluationError: no bars"
    assert (summary["points"], summary["selected_index"]) == ([], None)


def test_the_selected_point_is_the_one_the_holdout_opened_for_even_when_it_is_not_first():
    case = build(complete_result(TWO_POINTS, selected=1), TWO_POINTS)
    assert case.selected_params == {"ENTRY_MINUTE": 600} and case.evidence["selected_index"] == 1
    assert case.evidence["replay_index"] == 1 and case.selected_trial_id == "t1"
    first, second = case.evidence["points"]
    assert [r["passed"] for r in second["rules"]] == [r.passed for r in case.final_rule_results]
    assert first["trial_id"] == "t0"


STAGE_CASES = {
    "COMPLETE": lambda: build(complete_result()),
    "HOLDOUT_FAILED": lambda: build(complete_result(holdout_passed=False)),
    "PRE_HOLDOUT_FAILED": lambda: build(pre_holdout_result()),
    "FAILED": failed_case,
}


@pytest.mark.parametrize("stage", STAGE_CASES)
def test_a_built_case_of_every_stage_is_written_and_loaded_back_unchanged(tmp_path, stage):
    signer = AttestationSigner.generate()
    case = STAGE_CASES[stage]()
    assert case.stage == stage
    digest = write_evaluation_case(tmp_path / "cases", case, signer)
    assert load_verified_case(tmp_path / "cases", digest, {signer.public_key_id: signer.public_key}) == case


def test_a_written_case_is_never_bundle_evidence(tmp_path):
    signer = AttestationSigner.generate()
    case = build(complete_result())
    digest = write_evaluation_case(tmp_path / "cases", case, signer)
    path = case_path(tmp_path / "cases", digest)
    with pytest.raises(ArtifactVerifierError):
        ArtifactVerifier([signer.public_key]).verify(path, "paper", case.artifact_id, NOW)
    for candidate in (path, path.parent):
        with pytest.raises(PaperMaterialsError):
            require_qualified_research_evidence(candidate)
