"""SP2c Plan 1 Task 4: durable judgments, rules first, one per case, cooldown on REJECT (spec 5.2 items 2-3)."""
from __future__ import annotations

import datetime as dt
import json

import exchange_calendars as xcals
import pytest
from pydantic import ValidationError

from tests.automation.backtest_judge_fixtures import (
    FILE_HASH, NARRATIVE, VERSION, ClockMovesWhileWaitingForTheLock, World, case_body, count_judgments,
    failed_results, finished, judge_config, judgment, make_case, renewal_case_body, request_body, world,
    write_signed_body,
)
from trader.automation.backtest_judgments import (
    BacktestJudgments, JudgmentRefused, RenewalStatus, nth_session_after,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.evaluation_claims import ClaimRefused
from trader.data.duckdb_store import DuckDBConnection
from trader.research.evaluation_case import NO_DEPLOY_MENU, EvaluationCase, case_path, write_evaluation_case
from trader.research.evaluation_request import evaluation_request_id
from trader.research.signing import AttestationSigner


def claim_status(w: World, body) -> str:
    try:
        return w.claims.claim(evaluation_request_id(body), body, principal="research").status
    except ClaimRefused as refused:
        return refused.code


def test_a_deploy_on_a_complete_passing_case_is_recorded_with_its_binding(tmp_path):
    w = world(tmp_path)
    reply = w.judgments.record(judgment(finished(w), "DEPLOY"))
    assert (reply["status"], reply["verdict"], reply["cooldown_until_session"]) == ("RECORDED", "DEPLOY", None)
    stored = w.judgments.get("jdg-00000001")
    assert stored.binding["strategy_file_hash"] == FILE_HASH and stored.binding["params"] == {"RANGE_MINUTES": 15}
    assert stored.binding["class_name"] == "OpeningRangeBreakout" and stored.body["narrative"] == NARRATIVE
    assert stored.request_id == evaluation_request_id(request_body())


def test_rules_first_a_rule_failing_case_is_never_deployed(tmp_path):                  # review focus 4
    w = world(tmp_path)
    digest = finished(w, stage="HOLDOUT_FAILED", decision_state="CANDIDATE", final_rule_results=failed_results())
    for request in (judgment(digest, "DEPLOY"), judgment(digest, "SHADOW")):          # both claim the full menu
        assert w.judgments.record(request)["code"] == "JUDGMENT_MENU_MISMATCH"
    assert count_judgments(w.db) == 0
    assert w.judgments.record(judgment(digest, "SHADOW", menu=NO_DEPLOY_MENU))["status"] == "RECORDED"


def test_a_deploy_on_a_case_whose_holdout_failed_is_refused_and_writes_nothing(tmp_path):
    w = world(tmp_path)
    digest = finished(w, stage="HOLDOUT_FAILED")          # every rule passed, but the holdout did not
    assert w.judgments.record(judgment(digest, "DEPLOY"))["code"] == "JUDGMENT_MENU_MISMATCH"
    assert count_judgments(w.db) == 0
    assert claim_status(w, request_body(cohort=[{"RANGE_MINUTES": 45}])) == "ACCEPTED"     # no cooldown started


def test_a_deploy_on_a_case_whose_selected_params_were_never_claimed_writes_nothing(tmp_path):
    w = world(tmp_path)
    body = request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")
    w.claims.update(evaluation_request_id(body), "DONE")
    digest = write_signed_body(w.keys, case_body(body, selected_params={"RANGE_MINUTES": 99}))
    reply = w.judgments.record(judgment(digest, "DEPLOY"))
    assert (reply["status"], reply["code"]) == ("REFUSED", "CASE_MALFORMED")
    assert count_judgments(w.db) == 0


def test_a_complete_case_whose_signed_holdout_evidence_failed_is_never_deployed(tmp_path):    # PR #91 round 2
    w = world(tmp_path)
    body = request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")
    w.claims.update(evaluation_request_id(body), "DONE")
    contradictory = case_body(body)                  # header: holdout_passed True, every rule passed
    contradictory["evidence"]["holdout"]["passed"] = False
    digest = write_signed_body(w.keys, contradictory)
    reply = w.judgments.record(judgment(digest, "DEPLOY"))
    assert (reply["status"], reply["code"], reply["cooldown_until_session"]) == ("REFUSED", "CASE_MALFORMED", None)
    assert count_judgments(w.db) == 0
    assert claim_status(w, request_body(cohort=[{"RANGE_MINUTES": 45}])) == "ACCEPTED"     # no cooldown started


def test_a_complete_case_whose_signed_selected_point_failed_its_gate_is_never_deployed(tmp_path):  # PR #93 r1
    w = world(tmp_path)
    body = request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")
    w.claims.update(evaluation_request_id(body), "DONE")
    contradictory = case_body(body)                  # header: COMPLETE, PAPER_ELIGIBLE, every rule passed
    point = contradictory["evidence"]["points"][contradictory["evidence"]["selected_index"]]
    point["pre_holdout_passed"] = False
    point["rules"][0]["passed"] = False
    digest = write_signed_body(w.keys, contradictory)
    reply = w.judgments.record(judgment(digest, "DEPLOY"))
    assert (reply["status"], reply["code"]) == ("REFUSED", "CASE_MALFORMED")
    assert count_judgments(w.db) == 0


def test_a_pre_holdout_failure_reaches_jev_for_shadow_or_reject(tmp_path):
    w = world(tmp_path)
    digest = finished(w, stage="PRE_HOLDOUT_FAILED")
    assert w.judgments.record(judgment(digest, "REJECT", menu=NO_DEPLOY_MENU))["status"] == "RECORDED"
    assert w.judgments.get("jdg-00000001").binding["artifact_id"] is None


@pytest.mark.parametrize("damage,code", [
    ("body", "CASE_DIGEST_MISMATCH"), ("key", "CASE_KEY_UNKNOWN"), ("gone", "CASE_NOT_FOUND"),
])
def test_a_tampered_foreign_or_missing_case_records_nothing(tmp_path, damage, code):     # review focus 4
    w = world(tmp_path)
    digest = finished(w, signer=AttestationSigner.generate() if damage == "key" else None)
    path = case_path(w.keys.cases_dir, digest)
    if damage == "body":
        envelope = json.loads(path.read_text())
        envelope["case"]["bar_size"] = "1 min"
        path.write_text(json.dumps(envelope))
    if damage == "gone":
        path.unlink()
    reply = w.judgments.record(judgment(digest, "REJECT"))
    assert (reply["status"], reply["code"]) == ("REFUSED", code)
    assert count_judgments(w.db) == 0
    assert claim_status(w, request_body(cohort=[{"RANGE_MINUTES": 45}])) == "ACCEPTED"     # no cooldown started


def test_one_judgment_per_case_and_per_evaluation(tmp_path):                           # review focus 3
    w = world(tmp_path)
    digest = finished(w)
    assert w.judgments.record(judgment(digest, "SHADOW"))["status"] == "RECORDED"
    assert w.judgments.record(judgment(digest, "SHADOW"))["status"] == "EXISTING"
    assert w.judgments.record(judgment(digest, "REJECT"))["code"] == "JUDGMENT_CONFLICT"      # same id, other body
    assert w.judgments.record(judgment(digest, "SHADOW", judgment_id="jdg-00000002"))["code"] == "JUDGMENT_CONFLICT"
    second_case = write_evaluation_case(
        w.keys.cases_dir, make_case(request_body(), created_at="2026-10-08T21:31:00+00:00"), w.keys.signer)
    assert w.judgments.record(judgment(second_case, "SHADOW", judgment_id="jdg-00000003"))["code"] \
        == "JUDGMENT_CONFLICT"                                                             # same evaluation
    assert count_judgments(w.db) == 1


def test_a_retry_that_only_respells_the_decided_time_is_the_same_judgment(tmp_path):     # review focus 3
    w = world(tmp_path)
    digest = finished(w)
    w.judgments.record(judgment(digest, "SHADOW", decided_at="2026-10-08T21:30:00+00:00"))
    for spelling in ("2026-10-08T21:30:00Z", "2026-10-08T17:30:00-04:00"):
        assert w.judgments.record(judgment(digest, "SHADOW", decided_at=spelling))["status"] == "EXISTING"


def test_reject_cools_down_the_strategy_key_for_ten_sessions(tmp_path):
    w = world(tmp_path)
    assert w.judgments.record(judgment(finished(w), "REJECT"))["cooldown_until_session"] == "2026-10-22"
    another = request_body(cohort=[{"RANGE_MINUTES": 45}], research_day="2026-10-09")
    w.clock.now = dt.datetime(2026, 10, 22, 14, 0, tzinfo=dt.timezone.utc)
    assert claim_status(w, another) == "FAMILY_COOLING_DOWN"
    w.clock.now = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)
    assert claim_status(w, another) == "ACCEPTED"


def test_reject_cooldown_starts_from_commit_day(tmp_path):                            # PR #93 round 1
    w = world(tmp_path)
    digest = finished(w)                                  # claimed Thursday 2026-10-08 17:30 ET
    w.clock.now = dt.datetime(2026, 10, 9, 3, 59, 59, tzinfo=dt.timezone.utc)      # 23:59:59 ET Thursday
    committed = dt.datetime(2026, 10, 9, 4, 0, 1, tzinfo=dt.timezone.utc)          # 00:00:01 ET Friday
    waiting = BacktestJudgments(ClockMovesWhileWaitingForTheLock(w.db, w.clock, committed),
                                config=judge_config(family_cooldown_sessions=1), calendar=XNYSCalendarPolicy(),
                                cases_dir=w.keys.cases_dir, verify_dir=w.keys.verify_dir, now=w.clock)
    reply = waiting.record(judgment(digest, "REJECT"))
    assert (reply["status"], reply["cooldown_until_session"]) == ("RECORDED", "2026-10-12")    # Monday, not Friday
    assert w.judgments.get("jdg-00000001").recorded_at == committed


def test_shadow_and_no_verdict_start_no_cooldown(tmp_path):
    w = world(tmp_path)
    shadow_case = finished(w, request_body(research_day="2026-10-01"))
    silent_case = finished(w, request_body(research_day="2026-10-02"))
    assert w.judgments.record(judgment(shadow_case, "SHADOW"))["cooldown_until_session"] is None
    assert w.judgments.record(judgment(silent_case, "NO_VERDICT", judgment_id="jdg-00000002"))["status"] == "RECORDED"
    assert claim_status(w, request_body(research_day="2026-10-03")) == "ACCEPTED"


def test_a_no_verdict_without_a_model_call_is_recorded_with_a_null_attempt(tmp_path):
    w = world(tmp_path)
    case = finished(w)
    reply = w.judgments.record(judgment(case, "NO_VERDICT", jev_attempt_ref=None))
    assert (reply["status"], reply["cooldown_until_session"]) == ("RECORDED", None)
    assert w.judgments.get("jdg-00000001").body["jev_attempt_ref"] is None
    assert w.judgments.record(judgment(case, "NO_VERDICT", jev_attempt_ref=None))["status"] == "EXISTING"


def test_a_case_needs_its_own_finished_claim(tmp_path):
    w = world(tmp_path)
    body = request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")              # still QUEUED
    queued = write_evaluation_case(w.keys.cases_dir, make_case(body), w.keys.signer)
    reply = w.judgments.record(judgment(queued, "SHADOW"))
    assert (reply["code"], reply["retryable"]) == ("CASE_CLAIM_NOT_FINISHED", True)
    orphan = write_evaluation_case(w.keys.cases_dir, make_case(request_body(research_day="2026-10-05")), w.keys.signer)
    assert w.judgments.record(judgment(orphan, "SHADOW", judgment_id="jdg-00000002"))["code"] == "CASE_CLAIM_UNKNOWN"
    w.claims.update(evaluation_request_id(body), "DONE")
    other_day = write_evaluation_case(w.keys.cases_dir, make_case(body, claim_day="2026-10-07"), w.keys.signer)
    assert w.judgments.record(judgment(other_day, "SHADOW", judgment_id="jdg-00000003"))["code"] \
        == "CASE_CLAIM_MISMATCH"


def test_a_decision_time_before_the_claim_is_refused(tmp_path):
    w = world(tmp_path)
    reply = w.judgments.record(judgment(finished(w), "SHADOW", decided_at="2026-10-08T21:29:59+00:00"))
    assert (reply["status"], reply["code"]) == ("REFUSED", "DECIDED_BEFORE_CLAIM")
    assert count_judgments(w.db) == 0


def test_a_failed_evaluation_still_gets_a_verdict(tmp_path):                         # spec 8
    w = world(tmp_path)
    digest = finished(w, state="FAILED", stage="FAILED")
    reply = w.judgments.record(judgment(digest, "REJECT", menu=NO_DEPLOY_MENU))
    assert (reply["status"], reply["verdict"], reply["cooldown_until_session"]) == ("RECORDED", "REJECT", "2026-10-22")


def test_failed_claim_cannot_deploy_from_a_complete_signed_case(tmp_path):             # PR #93 round 1
    w = world(tmp_path)
    digest = finished(w, state="FAILED")                  # a passing COMPLETE case on a FAILED claim
    reply = w.judgments.record(judgment(digest, "DEPLOY"))
    assert (reply["status"], reply["code"]) == ("REFUSED", "CASE_CLAIM_MISMATCH")
    assert count_judgments(w.db) == 0
    assert claim_status(w, request_body(cohort=[{"RANGE_MINUTES": 45}])) == "ACCEPTED"     # no cooldown started


def test_a_done_claim_cannot_carry_a_failed_stage_case(tmp_path):
    w = world(tmp_path)
    digest = finished(w, state="DONE", stage="FAILED")
    reply = w.judgments.record(judgment(digest, "REJECT", menu=NO_DEPLOY_MENU))
    assert (reply["status"], reply["code"], reply["cooldown_until_session"]) \
        == ("REFUSED", "CASE_CLAIM_MISMATCH", None)
    assert count_judgments(w.db) == 0


def test_a_decision_time_ahead_of_the_trader_is_refused(tmp_path):
    w = world(tmp_path)
    reply = w.judgments.record(judgment(finished(w), "SHADOW", decided_at="2026-10-08T21:40:00+00:00"))
    assert reply["code"] == "DECIDED_IN_FUTURE"


def test_a_renewal_waits_for_plan_5(tmp_path):
    w = world(tmp_path)
    digest = write_evaluation_case(w.keys.cases_dir, EvaluationCase.model_validate(renewal_case_body()), w.keys.signer)
    reply = w.judgments.record(judgment(digest, "SHADOW", kind="RENEWAL", renewal_of_version=VERSION))
    assert reply["code"] == "RENEWAL_NOT_SUPPORTED" and count_judgments(w.db) == 0


def test_a_renewal_port_that_blocks_deploy_still_lets_jev_shadow(tmp_path):
    class ExpiredBundle:
        def status(self, case, *, now):
            return RenewalStatus(deploy_block_code="BUNDLE_EXPIRED", detail="attestation expired")

    w = world(tmp_path, renewals=ExpiredBundle())
    digest = write_evaluation_case(w.keys.cases_dir, EvaluationCase.model_validate(renewal_case_body()), w.keys.signer)
    deploy = judgment(digest, "DEPLOY", kind="RENEWAL", renewal_of_version=VERSION)
    assert w.judgments.record(deploy)["code"] == "BUNDLE_EXPIRED"
    shadow = judgment(digest, "SHADOW", kind="RENEWAL", renewal_of_version=VERSION, judgment_id="jdg-00000002")
    assert w.judgments.record(shadow)["status"] == "RECORDED"


def test_an_edited_judgment_reads_as_tampered(tmp_path):
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "SHADOW"))
    w.db.execute("UPDATE backtest_judgments SET verdict = 'DEPLOY' WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(JudgmentRefused) as exc:
        w.judgments.get("jdg-00000001")
    assert exc.value.code == "JUDGMENT_TAMPERED"


def edit_cooldown(w: World) -> None:
    w.db.execute("UPDATE backtest_judgments SET cooldown_until_session = '2026-10-09' "
                 "WHERE judgment_id = 'jdg-00000001'")


def test_retrying_an_edited_judgment_raises_instead_of_replying(tmp_path):
    w = world(tmp_path)
    request = judgment(finished(w), "REJECT")
    w.judgments.record(request)
    edit_cooldown(w)
    with pytest.raises(JudgmentRefused) as exc:
        w.judgments.record(request)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_a_concurrent_retry_against_an_edited_judgment_raises(tmp_path):
    w = world(tmp_path)
    request = judgment(finished(w), "REJECT")
    w.judgments.record(request)
    edit_cooldown(w)
    w.judgments._existing_reply = lambda judgment_id, body_digest: None      # the retry passed the first look
    with pytest.raises(JudgmentRefused) as exc:
        w.judgments.record(request)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_a_judgment_is_found_by_its_case(tmp_path):
    w = world(tmp_path)
    case = finished(w)
    w.judgments.record(judgment(case, "SHADOW"))
    assert w.judgments.get_by_case(case) == w.judgments.get("jdg-00000001")
    assert w.judgments.get_by_case("sha256:" + "e" * 64) is None


def test_judgments_survive_a_restart(tmp_path):
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "REJECT"))
    fresh = BacktestJudgments(DuckDBConnection(str(tmp_path / "journal.duckdb")), config=judge_config(),
                              calendar=XNYSCalendarPolicy(), cases_dir=w.keys.cases_dir,
                              verify_dir=w.keys.verify_dir, now=w.clock)
    assert fresh.get("jdg-00000001") == w.judgments.get("jdg-00000001")
    assert fresh.get("jdg-00000009") is None


def test_nth_session_after_skips_weekends_and_holidays():
    calendar = XNYSCalendarPolicy()
    assert nth_session_after(calendar, dt.date(2026, 10, 8), 10) == dt.date(2026, 10, 22)
    assert nth_session_after(calendar, dt.date(2026, 11, 25), 1) == dt.date(2026, 11, 27)    # Thanksgiving
    assert nth_session_after(calendar, dt.date(2026, 10, 10), 1) == dt.date(2026, 10, 12)    # Saturday


def sessions_after(day: dt.date, count: int) -> list[dt.date]:
    """The first ``count`` XNYS sessions after ``day``, read from a separate wide calendar."""
    wide = xcals.get_calendar("XNYS", start="2020-01-02", end=day + dt.timedelta(days=4 * count + 400))
    return [ts.date() for ts in wide.sessions_in_range(day + dt.timedelta(days=1), wide.last_session)][:count]


def default_calendar_end() -> dt.date:
    """Where a calendar built now ends (about one year ahead)."""
    return xcals.get_calendar("XNYS").last_session.date()


def test_nth_session_after_serves_a_day_past_the_end_of_the_calendar_it_was_built_with():
    calendar = XNYSCalendarPolicy()                       # built now: it ends about one year ahead
    day = default_calendar_end() + dt.timedelta(days=60)
    assert nth_session_after(calendar, day, 10) == sessions_after(day, 10)[-1]
    assert nth_session_after(calendar, dt.date(2026, 10, 8), 10) == dt.date(2026, 10, 22)


def test_a_long_cooldown_accepted_by_config_still_records_a_reject(tmp_path):
    w = world(tmp_path, family_cooldown_sessions=120)
    digest = finished(w)
    trader_up_for_months = default_calendar_end() - dt.timedelta(days=30)
    w.clock.now = dt.datetime.combine(trader_up_for_months, dt.time(21, 30), tzinfo=dt.timezone.utc)
    reply = w.judgments.record(judgment(digest, "REJECT"))
    assert reply["status"] == "RECORDED"
    assert reply["cooldown_until_session"] == sessions_after(trader_up_for_months, 120)[-1].isoformat()


def test_a_calendar_that_cannot_reach_the_cooldown_refuses_the_reject_and_writes_nothing(tmp_path):
    short = XNYSCalendarPolicy(calendar=xcals.get_calendar("XNYS", start="2026-01-02", end="2026-10-30"))
    with pytest.raises(JudgmentRefused) as exc:
        nth_session_after(short, dt.date(2026, 10, 8), 30)
    assert exc.value.code == "COOLDOWN_CALENDAR_UNAVAILABLE"
    w = world(tmp_path, calendar=short, family_cooldown_sessions=30)
    reply = w.judgments.record(judgment(finished(w), "REJECT"))
    assert (reply["status"], reply["code"]) == ("REFUSED", "COOLDOWN_CALENDAR_UNAVAILABLE")
    assert count_judgments(w.db) == 0


@pytest.mark.parametrize("changes", [
    {"verdict": "DEPLOY", "narrative": None},
    {"verdict": "DEPLOY", "narrative": {**NARRATIVE, "episode_dominance": "   "}},
    {"verdict": "SHADOW", "narrative": NARRATIVE},
    {"verdict": "DEPLOY", "menu": NO_DEPLOY_MENU},
    {"menu": ("REJECT", "SHADOW")},
    {"decided_at": "2026-10-08T21:30:00"},
    {"decided_at": "0001-01-01T00:00:00+01:00"},          # out of range in UTC
    {"renewal_of_version": VERSION},
    {"kind": "RENEWAL"},
    {"judgment_id": "short"},
    {"jev_model": "has spaces"},
    {"jev_attempt_ref": None},
    {"verdict": "DEPLOY", "narrative": NARRATIVE, "jev_attempt_ref": None},
    {"jev_attempt_ref": "att none"},
])
def test_a_malformed_judgment_body_is_refused_by_the_wire_model(changes):
    with pytest.raises(ValidationError):
        judgment("sha256:" + "c" * 64, **changes)
