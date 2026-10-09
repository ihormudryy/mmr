"""SP2c Plan 5 Task 5: a RENEWAL case from forward evidence: no holdout, no trial, cohort = the deployed point."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.research.renewal_fixtures import PARAMS, SESSIONS, V1, forward_view, trip
from trader.ai.research_wire import CaseSummary, parse_reply
from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
from trader.automation.paper_materials import PaperMaterialsError, require_qualified_research_evidence
from trader.research.case_builder import evaluation_summary
from trader.research.evaluation_case import (
    FULL_MENU, NO_DEPLOY_MENU, case_path, load_verified_case, offered_menu, write_evaluation_case,
)
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.research.renewal_case import build_renewal_case, pending_sessions, renewal_request_id
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2024, 4, 4, 21, 0, tzinfo=dt.timezone.utc)                # the evening after the last session


def build(**view_changes):
    view = ForwardEvidenceView.model_validate(forward_view(**view_changes))
    return build_renewal_case(view, created_at=NOW, warmup_sessions=5)


def test_a_complete_window_is_a_forward_complete_case_that_offers_deploy():
    case = build(trips=(trip(),))
    assert (case.kind, case.stage, case.request_id, case.claim_day) == ("RENEWAL", "FORWARD_COMPLETE", None, None)
    assert case.cohort == [PARAMS] and case.selected_params == PARAMS and offered_menu(case) == FULL_MENU
    assert (case.artifact_id, case.family_id, case.decision_state, case.final_rule_results) == (
        "a" * 64, "f" * 64, None, [])
    assert case.renewal.model_dump() == {"prior_deployment_version": V1, "forward_sessions": 3,
                                         "incomplete_sessions": 0}
    assert (case.evidence["replay_index"], case.evidence["points"], case.evidence["warmup_sessions"]) == (0, [], 5)
    summary = evaluation_summary(case, order_notional=1.0)
    parsed = parse_reply(CaseSummary, "case", summary)                     # Plan 4's strict model accepts it
    assert (parsed.kind, parsed.rules_passed, parsed.renewal_checks_passed, parsed.prior_version_digest) == (
        "RENEWAL", True, True, V1)
    assert (parsed.order_notional, parsed.holdout_passed, parsed.strategy_trials) == (1900.0, None, 0)
    assert parsed.forward["pnl_usd"] == 15.0 and parsed.forward["paper_trips"] == 1
    assert parsed.forward["paper_net_pnl_usd"] == 7.5


@pytest.mark.parametrize("state", ["INCOMPLETE", "MISSING", "NOT_REPLAYED"])
def test_any_session_not_complete_makes_a_forward_incomplete_case(state):                  # review focus 4
    case = build(states=("COMPLETE", state, "COMPLETE"))
    assert (case.stage, case.renewal.incomplete_sessions, offered_menu(case)) == (
        "FORWARD_INCOMPLETE", 1, NO_DEPLOY_MENU)
    summary = evaluation_summary(case, order_notional=1900.0)
    assert (summary["rules_passed"], summary["renewal_checks_passed"], summary["forward"]["pnl_usd"]) == (
        False, False, None)
    assert summary["forward"]["known_pnl_usd"] == 10.0
    forward = summary["forward"]                                    # no partial sum reads like a whole window
    assert (forward["fees_usd"], forward["trades"], forward["worst_session_pnl_usd"], forward["end_equity_usd"]) == (
        None, None, None, None)


def test_a_complete_window_carries_every_sum_and_counts_unpriced_trips():
    forward = evaluation_summary(build(trips=(trip(), trip("rt-2", net_pnl=None), trip("rt-3", status="OPEN"))),
                                 order_notional=1900.0)["forward"]
    assert (forward["fees_usd"], forward["trades"], forward["worst_session_pnl_usd"], forward["end_equity_usd"]) == (
        3.0, 6, 5.0, 100_005.0)
    assert (forward["paper_trips"], forward["paper_trips_closed"], forward["paper_trips_unpriced"],
            forward["paper_net_pnl_usd"]) == (3, 2, 1, 7.5)


def test_a_renewal_case_without_its_order_notional_is_a_named_error():
    case = build()
    del case.evidence["order_notional"]
    with pytest.raises(ValueError, match="RENEWAL case .*order_notional"):
        evaluation_summary(case, order_notional=1900.0)


def test_missing_rows_are_pending_until_the_replay_deadline():
    view = ForwardEvidenceView.model_validate(forward_view(states=("COMPLETE", "COMPLETE", "MISSING")))
    before = dt.datetime(2024, 4, 4, 12, 0, tzinfo=dt.timezone.utc)        # 04-03 close + 17 h is 04-04 13:00 UTC
    assert pending_sessions(view, now=before, incomplete_after_hours=16) == [SESSIONS[2]]
    assert pending_sessions(view, now=NOW, incomplete_after_hours=16) == []


def test_the_renewal_request_id_names_the_version_only():
    assert renewal_request_id(V1) == renewal_request_id(V1) and renewal_request_id(V1).startswith("sha256:")
    assert renewal_request_id("sha256:" + "2" * 64) != renewal_request_id(V1)


def test_a_signed_renewal_case_verifies_and_is_never_bundle_evidence(tmp_path):
    signer = AttestationSigner.generate()
    case = build()
    digest = write_evaluation_case(tmp_path / "cases", case, signer)
    assert load_verified_case(tmp_path / "cases", digest, {signer.public_key_id: signer.public_key}) == case
    path = case_path(tmp_path / "cases", digest)
    with pytest.raises(ArtifactVerifierError):
        ArtifactVerifier([signer.public_key]).verify(path, "paper", case.artifact_id, NOW)
    with pytest.raises(PaperMaterialsError):
        require_qualified_research_evidence(path)


def test_a_complete_session_without_numbers_is_refused_loudly():
    wire = forward_view()
    wire["sessions"][1]["pnl_usd"] = None
    with pytest.raises(ValueError, match="2024-04-02"):
        build_renewal_case(ForwardEvidenceView.model_validate(wire), created_at=NOW, warmup_sessions=5)
