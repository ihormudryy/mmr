"""SP3 Plan 3 final review: a claim closed FAILED that gets no judgment within CLAIM_FAILED_GRACE returns its
authored record to STORED.

Without this, an authored record stays EVALUATING forever and keeps its cap slot (spec 8, final-review fix).
Inside the grace, Jev's judgment of the FAILED case still moves the record (coordinator ruling).
"""
from __future__ import annotations

import datetime as dt
import logging

from tests.automation.authored_fixtures import report, source, stored, submit_request
from tests.automation.backtest_judge_fixtures import count_judgments, finished, judgment
from tests.automation.test_authored_sp2c_hooks import authored_body, claim, hooked
from trader.automation.authored_strategies import (
    CLAIM_FAILED, CLAIM_FAILED_GRACE, EVALUATING, FAILED, REJECTED, STORED,
)
from trader.research.evaluation_case import NO_DEPLOY_MENU
from trader.research.evaluation_request import evaluation_request_id


def body_for(digest, night: int):
    """A distinct claim per night: the same body would be the same request id and an EXISTING claim."""
    return authored_body(digest, cohort=[{"RANGE_BARS": 10 + night}])


def claimed_then_failed(w, body) -> str:
    request_id = evaluation_request_id(body)
    assert claim(w, body).status == "ACCEPTED"
    w.claims.update(request_id, "RUNNING")
    w.claims.update(request_id, "FAILED")                    # research gives up: no case, no judgment
    return request_id


def wait(aw, span: dt.timedelta) -> None:
    aw.clock.now += span


def test_a_failed_claim_returns_the_record_to_stored_and_frees_the_cap(tmp_path):
    w, aw = hooked(tmp_path, max_active=1)
    digest = stored(aw)
    request_id = claimed_then_failed(w, body_for(digest, 0))
    wait(aw, CLAIM_FAILED_GRACE)
    overview = aw.store.overview()
    record = aw.store.get(digest)
    assert (record.state, record.attempts, record.reason, record.evaluation_request_id) == (STORED, 1, None, None)
    assert record.history[-1]["event"] == CLAIM_FAILED and record.history[-1]["request_id"] == request_id
    assert overview["records"][0]["state"] == "STORED"
    assert aw.store.submit(submit_request(source(20)))["code"] == "AUTHORED_CAP_FULL"   # STORED still counts
    assert claimed_then_failed(w, body_for(digest, 1)) != request_id                    # but it may run again


def test_the_third_failed_claim_fails_the_record_and_frees_the_cap(tmp_path):
    w, aw = hooked(tmp_path, max_active=1)
    digest = stored(aw)
    for night in range(3):
        claimed_then_failed(w, body_for(digest, night))
        wait(aw, CLAIM_FAILED_GRACE)
        aw.store.overview()
        assert aw.store.get(digest).state == (FAILED if night == 2 else STORED)
    record = aw.store.get(digest)
    assert (record.attempts, record.reason) == (3, CLAIM_FAILED)
    assert aw.store.submit(submit_request(source(20)))["status"] == "STORED"


def test_an_open_claim_leaves_the_record_evaluating(tmp_path):
    w, aw = hooked(tmp_path, max_active=1)
    digest = stored(aw)
    body = body_for(digest, 0)
    assert claim(w, body).status == "ACCEPTED"
    assert aw.store.overview()["records"][0]["state"] == "EVALUATING"                   # QUEUED
    w.claims.update(evaluation_request_id(body), "RUNNING")
    wait(aw, 2 * CLAIM_FAILED_GRACE)
    overview = aw.store.overview()
    assert (overview["records"][0]["state"], overview["room"]) == ("EVALUATING", 0)


def test_a_failed_claim_with_its_judgment_is_the_judgments_to_move(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    body = authored_body(digest)
    case = finished(w, body, state="FAILED", stage="FAILED", strategy_file_hash=digest)
    assert w.judgments.record(judgment(case, "NO_VERDICT", menu=NO_DEPLOY_MENU))["status"] == "RECORDED"
    wait(aw, CLAIM_FAILED_GRACE)
    aw.store.overview()
    record = aw.store.get(digest)
    assert (record.state, record.reason, record.attempts) == (FAILED, "JEV_NO_VERDICT", 0)


def test_a_judgment_inside_the_grace_still_moves_the_record(tmp_path):
    w, aw = hooked(tmp_path, max_active=1)
    digest = stored(aw)
    case = finished(w, authored_body(digest), state="FAILED", stage="FAILED", strategy_file_hash=digest)
    wait(aw, CLAIM_FAILED_GRACE - dt.timedelta(seconds=1))
    overview = aw.store.overview()
    assert (overview["records"][0]["state"], overview["room"]) == ("EVALUATING", 0)
    assert w.judgments.record(judgment(case, "REJECT", menu=NO_DEPLOY_MENU))["status"] == "RECORDED"
    record = aw.store.get(digest)
    assert (record.state, record.reason, record.attempts) == (REJECTED, "JEV_REJECT", 0)
    assert count_judgments(w.db) == 1


def test_a_report_or_judgment_after_the_grace_does_not_move_the_record_again(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    body = authored_body(digest)
    case = finished(w, body, state="FAILED", stage="FAILED", strategy_file_hash=digest)   # signed, never judged
    request_id = evaluation_request_id(body)
    wait(aw, CLAIM_FAILED_GRACE)
    aw.store.overview()
    late = report(aw, digest, request_id, "SANDBOX_UNAVAILABLE")
    assert (late["status"], late["code"]) == ("REFUSED", "AUTHORED_STATE_CONFLICT")
    receipt = w.judgments.record(judgment(case, "NO_VERDICT", menu=NO_DEPLOY_MENU))
    assert (receipt["status"], receipt["code"]) == ("REFUSED", "AUTHORED_STATE_CONFLICT")
    assert count_judgments(w.db) == 0
    record = aw.store.get(digest)
    assert (record.state, record.attempts) == (STORED, 1)


def unreadable_claim(conn, request_id):
    raise RuntimeError("the claims table cannot be read")


def test_a_broken_reconciliation_breaks_neither_reads_nor_submits(tmp_path, monkeypatch, caplog):
    w, aw = hooked(tmp_path, max_active=2)
    digest = stored(aw)
    claimed_then_failed(w, body_for(digest, 0))
    wait(aw, CLAIM_FAILED_GRACE)
    monkeypatch.setattr("trader.automation.authored_strategies.claim_row_in_tx", unreadable_claim)
    with caplog.at_level(logging.ERROR):
        assert aw.store.overview()["records"][0]["state"] == "EVALUATING"
        assert aw.store.submit(submit_request(source(20)))["status"] == "STORED"
    assert "returning" in caplog.text
    assert aw.store.get(digest).state == EVALUATING
