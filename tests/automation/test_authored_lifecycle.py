"""SP3 Plan 3 Task 6: research reports, retirement and lazy expiry (spec 7.3, 7.6, 8, 10 Cap; rulings 11, 17-18)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.automation.authored_fixtures import (
    IMAGE, VERSION, authored_world, deployed, evaluating, moved, report, request_id, source, stored,
    submit_request,
)
from tests.automation.backtest_judge_fixtures import NOW
from trader.automation.ai_deployment_versions import WITHDRAWAL_ENTRY_IN_FLIGHT
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.authored_strategies import (
    AUTHORED_RETIRE_REASON, SHADOW, DeploymentLines, authored_retire_command_id, withdraw_with,
)
from trader.automation.authored_wire import RetireAuthoredStrategyRequest

ZERO = "sha256:" + "0" * 64


def retire_request(digest: str, code: str = "SANDBOX_ERROR") -> RetireAuthoredStrategyRequest:
    return RetireAuthoredStrategyRequest.model_validate({"digest": digest, "code": code, "detail": "live fault"})


def test_started_records_the_sandbox_image_and_a_repeat_is_unchanged(tmp_path):
    w = authored_world(tmp_path)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    first = report(w, digest, request_id(1), "STARTED", IMAGE)
    assert (first["status"], first["record"]["state"], first["record"]["sandbox_image_digest"]) == (
        "UPDATED", "EVALUATING", IMAGE)
    assert first["judged_count"] == 0
    assert first["record"]["tunables"]["RANGE_BARS"] == {"low": 5, "high": 30, "default": 15}  # cohort point 0
    again = report(w, digest, request_id(1), "STARTED", IMAGE)
    assert (again["status"], len(again["record"]["history"])) == ("UNCHANGED", len(first["record"]["history"]))


def test_a_report_must_name_the_records_current_request(tmp_path):                        # ruling 17
    w = authored_world(tmp_path)
    digest = stored(w)
    assert report(w, digest, request_id(1), "SANDBOX_ERROR")["code"] == "AUTHORED_STATE_CONFLICT"   # not claimed
    evaluating(w, digest, request_id(1))
    assert report(w, digest, request_id(2), "SANDBOX_ERROR")["code"] == "AUTHORED_STATE_CONFLICT"
    assert report(w, ZERO, request_id(1), "SANDBOX_ERROR")["code"] == "AUTHORED_STRATEGY_UNKNOWN"
    assert w.store.get(digest).state.value == "EVALUATING"


def test_a_sandbox_error_rejects_the_record_and_frees_the_cap(tmp_path):
    w = authored_world(tmp_path, max_active=1)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    reply = report(w, digest, request_id(1), "SANDBOX_ERROR")
    assert (reply["record"]["state"], reply["record"]["reason"]) == ("REJECTED", "SANDBOX_ERROR")
    assert w.store.submit(submit_request(source(16)))["status"] == "STORED"


def test_a_repeated_unavailable_report_counts_once(tmp_path):                                # review focus 4
    w = authored_world(tmp_path)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    first = report(w, digest, request_id(1), "SANDBOX_UNAVAILABLE")
    again = report(w, digest, request_id(1), "SANDBOX_UNAVAILABLE")
    assert (first["status"], first["record"]["state"], first["record"]["attempts"]) == ("UPDATED", "STORED", 1)
    assert (again["status"], again["record"]["attempts"]) == ("UNCHANGED", 1)
    assert first["record"]["evaluation_request_id"] is None


def test_three_unavailable_nights_fail_the_record_and_free_the_cap(tmp_path):          # focus 4, spec 10 Cap
    w = authored_world(tmp_path, max_active=3)
    digests = [stored(w, n) for n in (10, 11, 12)]
    assert w.store.submit(submit_request(source(13)))["code"] == "AUTHORED_CAP_FULL"
    for night in range(3):
        for index, digest in enumerate(digests):
            claim = request_id(100 * night + index + 1)
            evaluating(w, digest, claim)                 # a STORED record is evaluated again the next night
            reply = report(w, digest, claim, "SANDBOX_UNAVAILABLE")
            assert reply["record"]["state"] == ("FAILED" if night == 2 else "STORED")
    assert {w.store.get(digest).reason for digest in digests} == {"SANDBOX_UNAVAILABLE"}
    assert w.store.submit(submit_request(source(13)))["status"] == "STORED"


class FakeWithdrawals:
    """The SP2c withdraw path as the store sees it: True when this call withdrew, False when done before."""

    def __init__(self, refuse_first: int = 0):
        self.calls, self.withdrawn, self.refuse_first = [], set(), refuse_first

    def __call__(self, version_digest):
        self.calls.append(version_digest)
        if self.refuse_first:
            self.refuse_first -= 1
            raise DeploymentRefused(WITHDRAWAL_ENTRY_IN_FLIGHT, "an entry of this version is being sent")
        newly = version_digest not in self.withdrawn
        self.withdrawn.add(version_digest)
        return newly


def test_the_strategy_retires_a_deployed_record_once_and_withdraws_its_version(tmp_path):  # ruling 18, D4
    withdrawals = FakeWithdrawals()
    w = authored_world(tmp_path, withdraw=withdrawals)
    digest = deployed(w)
    first = w.store.retire(retire_request(digest, "SANDBOX_DIGEST_MISMATCH"))
    assert (first["status"], first["record"]["state"], first["record"]["reason"]) == ("UPDATED", "RETIRED",
                                                                                     "SANDBOX_ERROR")
    assert first["record"]["history"][-1]["detail"].startswith("SANDBOX_DIGEST_MISMATCH")
    assert withdrawals.withdrawn == {VERSION}
    assert w.store.retire(retire_request(digest))["status"] == "UNCHANGED"
    assert withdrawals.calls == [VERSION, VERSION]                # the repeat re-checks the withdrawal (a no-op)
    assert w.store.overview()["counted"] == 0                     # spec 10 Cap: RETIRED is not counted


def test_an_entry_in_flight_keeps_the_withdrawal_retryable_and_the_record_retired(tmp_path):   # D4
    withdrawals = FakeWithdrawals(refuse_first=1)
    w = authored_world(tmp_path, withdraw=withdrawals)
    digest = deployed(w)
    first = w.store.retire(retire_request(digest))
    assert (first["status"], first["code"], first["retryable"]) == ("REFUSED", "WITHDRAWAL_ENTRY_IN_FLIGHT", True)
    assert first["record"]["state"] == "RETIRED"                  # entries are refused from here on (ruling 16)
    again = w.store.retire(retire_request(digest))
    assert again["status"] == "UNCHANGED" and withdrawals.withdrawn == {VERSION}


def test_the_withdrawal_uses_the_sp2c_path_with_a_stable_command_id():
    seen = []
    versions = SimpleNamespace(withdraw=lambda digest, **kw: seen.append((digest, kw)) or True)
    assert withdraw_with(versions)(VERSION) is True
    assert seen == [(VERSION, {"reason": AUTHORED_RETIRE_REASON, "principal": "strategy",
                               "command_id": authored_retire_command_id(VERSION)})]
    assert authored_retire_command_id(VERSION) == authored_retire_command_id(VERSION)


def test_retire_refuses_a_record_that_is_not_deployed(tmp_path):
    w = authored_world(tmp_path)
    digest = stored(w)
    assert w.store.retire(retire_request(digest))["code"] == "AUTHORED_STATE_CONFLICT"
    assert w.store.retire(retire_request(ZERO))["code"] == "AUTHORED_STRATEGY_UNKNOWN"


def test_a_shadow_record_expires_after_the_deploy_window(tmp_path):                       # ruling 11
    w = authored_world(tmp_path)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    moved(w, digest, SHADOW, reason="JEV_SHADOW", verdict="SHADOW", judgment_id="jdg-00000001")
    w.clock.now = NOW + dt.timedelta(days=20)        # 2026-10-28: fewer than 20 sessions after 2026-10-08
    assert w.store.overview()["records"][0]["state"] == "SHADOW"
    w.clock.now = NOW + dt.timedelta(days=40)        # 2026-11-17: after the 20th session (2026-11-05)
    view = w.store.overview()
    assert (view["records"][0]["state"], view["records"][0]["reason"], view["counted"]) == (
        "EXPIRED", "SHADOW_EXPIRED", 0)


class NoSessions:
    def sessions_in_range(self, start, end):
        return []


def test_a_calendar_that_cannot_serve_keeps_the_record_counted(tmp_path):
    w = authored_world(tmp_path, calendar=NoSessions())
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    moved(w, digest, SHADOW, reason="JEV_SHADOW", verdict="SHADOW", judgment_id="jdg-00000001")
    w.clock.now = NOW + dt.timedelta(days=400)
    assert w.store.overview()["counted"] == 1


class FakeLines:
    def __init__(self, status):
        self.status = status

    def end_status(self, version_digest):
        return self.status if version_digest == VERSION else None


@pytest.mark.parametrize("status, state, reason", [
    ("WITHDRAWN", "RETIRED", "OWNER_RETIRED"),
    ("EXPIRED", "EXPIRED", "DEPLOY_EXPIRED"),
    ("JUDGMENT_ENDED", "EXPIRED", "RENEWAL_ENDED"),
    ("ACTIVE", "DEPLOYED", None),
    ("OVER_CAP", "DEPLOYED", None),
    (None, "DEPLOYED", None),
])
def test_a_deployed_record_follows_its_version_line(tmp_path, status, state, reason):
    lines = FakeLines(status)
    w = authored_world(tmp_path, lines=lambda: lines)
    deployed(w)
    record = w.store.overview()["records"][0]
    assert (record["state"], record["reason"]) == (state, reason)


def test_the_line_end_follows_renewals_to_the_last_version():
    def sealed(digest, prior):
        return SimpleNamespace(digest=digest, version=SimpleNamespace(prior_version=prior))
    statuses = {"v1": "SUPERSEDED", "v2": "SUPERSEDED", "v3": "WITHDRAWN"}
    lines = DeploymentLines(activity=SimpleNamespace(statuses=lambda: statuses),
                            versions=SimpleNamespace(sealed=lambda: (sealed("v1", None), sealed("v2", "v1"),
                                                                     sealed("v3", "v2"))))
    assert lines.end_status("v1") == "WITHDRAWN"
    assert lines.end_status("v3") == "WITHDRAWN"
    assert lines.end_status("unknown") is None
