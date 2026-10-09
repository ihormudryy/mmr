"""SP2c Plan 5 Task 1: at most one renewal judgment per deployment version (migration 125)."""
from __future__ import annotations

import pytest

from tests.automation.backtest_judge_fixtures import (
    VERSION, count_judgments, finished, judgment, renewal_case_body, world,
)
from trader.automation.backtest_judgments import JudgmentRefused, RenewalStatus
from trader.research.evaluation_case import EvaluationCase, write_evaluation_case

OTHER = "sha256:" + "c" * 64


class AllowRenewals:
    """A renewal port that lets every RENEWAL through; Task 3 tests the real one."""

    def status(self, case, *, now):
        return RenewalStatus()


def renewal_case(w, prior=VERSION, created_at="2026-10-08T21:30:00+00:00"):
    raw = renewal_case_body(created_at=created_at)
    raw["renewal"] = {**raw["renewal"], "prior_deployment_version": prior}
    return write_evaluation_case(w.keys.cases_dir, EvaluationCase.model_validate(raw), w.keys.signer)


def renew(w, case, verdict="SHADOW", prior=VERSION, judgment_id="jdg-renewal-1"):
    return w.judgments.record(judgment(case, verdict, kind="RENEWAL", renewal_of_version=prior,
                                       judgment_id=judgment_id))


def test_one_renewal_judgment_per_version_even_with_another_case(tmp_path):
    w = world(tmp_path, renewals=AllowRenewals())
    first = renewal_case(w)
    assert renew(w, first)["status"] == "RECORDED"
    assert renew(w, first)["status"] == "EXISTING"                                       # an exact retry
    second = renewal_case(w, created_at="2026-10-08T21:31:00+00:00")                      # another case, same version
    refused = renew(w, second, "REJECT", judgment_id="jdg-renewal-2")
    assert (refused["status"], refused["code"]) == ("REFUSED", "RENEWAL_ALREADY_JUDGED")
    assert count_judgments(w.db) == 1
    assert w.db.execute("SELECT prior_version, judgment_id FROM renewal_judgments", fetch="all") == [
        (VERSION, "jdg-renewal-1")]


def test_renewals_of_reads_the_sealed_judgments_of_one_version(tmp_path):
    w = world(tmp_path, renewals=AllowRenewals())
    renew(w, renewal_case(w), "REJECT")
    renew(w, renewal_case(w, prior=OTHER), "DEPLOY", prior=OTHER, judgment_id="jdg-renewal-9")
    (found,) = w.judgments.renewals_of(VERSION)
    assert (found.judgment_id, found.kind, found.verdict) == ("jdg-renewal-1", "RENEWAL", "REJECT")
    assert found.cooldown_until_session is not None                                       # a renewal REJECT cools down
    assert w.judgments.renewals_of("sha256:" + "d" * 64) == ()


def test_an_index_row_without_its_judgment_is_tampering(tmp_path):
    w = world(tmp_path, renewals=AllowRenewals())
    w.db.execute("INSERT INTO renewal_judgments VALUES (?, 'jdg-ghost-01', now())", [VERSION])
    with pytest.raises(JudgmentRefused) as exc:
        w.judgments.renewals_of(VERSION)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_an_initial_judgment_writes_no_renewal_index_row(tmp_path):
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "SHADOW"))
    assert w.db.execute("SELECT COUNT(*) FROM renewal_judgments", fetch="one")[0] == 0
