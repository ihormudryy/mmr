"""SP3 Plan 3 Task 7: authored keys in SP2c claims and judgments (spec 7.3, 7.7, 8; rulings 7-9)."""
from __future__ import annotations

import pytest

from tests.automation.authored_fixtures import TUNABLES, authored_world, report, stored, with_config
from tests.automation.backtest_judge_fixtures import (
    World, case_keys, count_judgments, finished, judge_config, judgment, request_body,
)
from trader.authored.keys import authored_key
from trader.automation.authored_strategies import EVALUATING, STORED
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.evaluation_claims import ClaimRefused, EvaluationClaims
from trader.research.evaluation_request import evaluation_request_id


def hooked(tmp_path, *, judge=None, **config):
    """SP2c claims and judgments wired to an authored store on one journal, as the trader wires them."""
    aw = authored_world(tmp_path, **config)
    keys, cfg = case_keys(tmp_path), judge_config(**(judge or {}))
    judgments = BacktestJudgments(aw.db, config=cfg, calendar=XNYSCalendarPolicy(), cases_dir=keys.cases_dir,
                                  verify_dir=keys.verify_dir, now=aw.clock, authored=aw.store)
    claims = EvaluationClaims(aw.db, config=cfg, now=aw.clock, authored=aw.store)
    return World(aw.db, aw.clock, claims, judgments, keys), aw


DEFAULTS = {name: bounds["default"] for name, bounds in TUNABLES.items()}


def point(**changes) -> dict:
    """A cohort point naming every stored tunable: the defaults with ``changes`` (Plan 4 ruling F6)."""
    return {**DEFAULTS, **changes}


def authored_body(digest, class_name="RangeBreak", **changes):
    raw = {"strategy_key": authored_key(digest, class_name), "cohort": [point(), point(RANGE_BARS=20)]}
    raw.update(changes)
    return request_body(**raw)


def claim(w, body):
    return w.claims.claim(evaluation_request_id(body), body, principal="research")


def refused_claim(w, body) -> ClaimRefused:
    with pytest.raises(ClaimRefused) as refused:
        claim(w, body)
    return refused.value


def test_an_accepted_claim_moves_the_record_to_evaluating(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    body = authored_body(digest)
    assert claim(w, body).status == "ACCEPTED"
    record = aw.store.get(digest)
    assert (record.state, record.evaluation_request_id) == (EVALUATING, evaluation_request_id(body))
    assert claim(w, body).status == "EXISTING"                       # a retry moves nothing again
    assert len(aw.store.get(digest).history) == 2


def test_the_switch_off_refuses_an_authored_claim(tmp_path):                                 # ruling 8
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    claims = EvaluationClaims(aw.db, config=judge_config(), now=aw.clock, authored=with_config(aw, enabled=False))
    with pytest.raises(ClaimRefused) as refused:
        claims.claim(evaluation_request_id(authored_body(digest)), authored_body(digest), principal="research")
    assert refused.value.code == "AUTHORED_STRATEGIES_DISABLED"
    assert aw.store.get(digest).state == STORED


@pytest.mark.parametrize("change, hint", [
    ({"cohort": [{"RANGE_BARS": 40}]}, "outside [5, 30]"),
    ({"cohort": [{"WINDOW": 3}]}, "not a tunable"),
    ({"cohort": [{"RANGE_BARS": "10"}]}, "not a number"),
    ({"bar_size": "1 min"}, "runs on 5 mins"),
])
def test_a_claim_outside_the_authored_file_is_not_allowed(tmp_path, change, hint):           # ruling 7
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    refused = refused_claim(w, authored_body(digest, **change))
    assert refused.code == "STRATEGY_NOT_ALLOWED" and hint in refused.detail
    assert aw.store.get(digest).state == STORED


@pytest.mark.parametrize("cohort, hint", [
    ([{"RANGE_BARS": 15}], "point 0 leaves out ['MIN_PROBABILITY']"),
    ([point(), {"RANGE_BARS": 20}], "point 1 leaves out ['MIN_PROBABILITY']"),
    ([point(), point(RANGE_BARS=20, WINDOW=3)], "WINDOW is not a tunable"),
    ([point(RANGE_BARS=10), point()], "point 0 is not the stored defaults"),
])
def test_a_cohort_must_name_every_tunable_and_start_at_the_defaults(tmp_path, cohort, hint):        # ruling F6
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    refused = refused_claim(w, authored_body(digest, cohort=cohort))
    assert refused.code == "STRATEGY_NOT_ALLOWED" and hint in refused.detail, refused.detail
    assert aw.store.get(digest).state == STORED


def test_another_class_an_unknown_digest_or_a_second_claim_is_not_allowed(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    assert "class" in refused_claim(w, authored_body(digest, class_name="Other")).detail
    assert "no authored strategy" in refused_claim(w, authored_body("sha256:" + "f" * 64)).detail
    claim(w, authored_body(digest))
    second = refused_claim(w, authored_body(digest, research_day="2026-10-07"))
    assert second.code == "STRATEGY_NOT_ALLOWED" and "only a STORED" in second.detail


def test_a_claim_refused_by_the_daily_limit_leaves_the_record_stored(tmp_path):              # review focus 5
    w, aw = hooked(tmp_path, judge={"evaluations_per_day": 1})
    digest = stored(aw)
    assert claim(w, request_body()).status == "ACCEPTED"             # a catalogue claim takes the only slot
    assert refused_claim(w, authored_body(digest)).code == "EVALUATION_LIMIT_REACHED"
    record = aw.store.get(digest)
    assert (record.state, len(record.history)) == (STORED, 1)


def test_an_authored_key_without_a_store_is_not_allowed(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    claims = EvaluationClaims(aw.db, config=judge_config(), now=aw.clock)
    with pytest.raises(ClaimRefused) as refused:
        claims.claim(evaluation_request_id(authored_body(digest)), authored_body(digest), principal="research")
    assert refused.value.code == "STRATEGY_NOT_ALLOWED"


def judged_case(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    body = authored_body(digest)
    return w, aw, digest, body, finished(w, body, strategy_file_hash=digest)


@pytest.mark.parametrize("verdict, state, reason", [
    ("REJECT", "REJECTED", "JEV_REJECT"),
    ("SHADOW", "SHADOW", "JEV_SHADOW"),
    ("DEPLOY", "SHADOW", "AWAITING_REGISTRATION"),
    ("NO_VERDICT", "FAILED", "JEV_NO_VERDICT"),
])
def test_a_judgment_moves_the_record_in_its_own_transaction(tmp_path, verdict, state, reason):   # ruling 9
    w, aw, digest, _body, case = judged_case(tmp_path)
    assert w.judgments.record(judgment(case, verdict))["status"] == "RECORDED"
    record = aw.store.get(digest)
    assert (record.state.value, record.reason, record.verdict, record.judgment_id) == (
        state, reason, verdict, "jdg-00000001")
    assert record.evaluation_request_id is None
    assert aw.store.overview()["judged_count"] == 1                 # spec 7.7: Plan 4 adds it to the next case


def test_a_judgment_for_a_record_not_evaluating_on_that_request_records_nothing(tmp_path):    # review focus 5
    w, aw, digest, body, case = judged_case(tmp_path)
    report(aw, digest, evaluation_request_id(body), "SANDBOX_ERROR")
    receipt = w.judgments.record(judgment(case, "SHADOW"))
    assert (receipt["status"], receipt["code"]) == ("REFUSED", "AUTHORED_STATE_CONFLICT")
    assert count_judgments(w.db) == 0
    assert aw.store.get(digest).state.value == "REJECTED"


def test_an_authored_case_without_a_store_is_refused(tmp_path):
    w, aw, digest, _body, case = judged_case(tmp_path)
    bare = BacktestJudgments(aw.db, config=judge_config(), calendar=XNYSCalendarPolicy(),
                             cases_dir=w.keys.cases_dir, verify_dir=w.keys.verify_dir, now=aw.clock)
    receipt = bare.record(judgment(case, "SHADOW"))
    assert (receipt["status"], receipt["code"]) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED")
    assert count_judgments(w.db) == 0
