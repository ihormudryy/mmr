"""SP2c Plan 2 Task 2: Plan 1 judgments and cooldowns behind two ports."""
import datetime as dt
from types import SimpleNamespace

import pytest

from tests.automation.backtest_judge_fixtures import KEY, NOW, FILE_HASH, finished, insert_reject, judgment, world
from trader.automation.ai_judgment_port import (
    Plan1Cooldowns, Plan1Judgments, cooldown_reader_for, judgment_reader_for, strategy_key,
)
from trader.automation.backtest_judgments import JudgmentRefused

CASE = SimpleNamespace(strategy_key="strategies/orb.py:Orb", strategy_file_hash="sha256:" + "a" * 64,
                       selected_params={"RANGE_MINUTES": 15}, conids=[265598, 272093], bar_size="1 min",
                       artifact_id="art-1", family_id="fam-1")


def stored_judgment(judgment_id, verdict="DEPLOY", kind="INITIAL", prior=None):
    """The fields of Plan 1's BacktestJudgment this port reads."""
    return SimpleNamespace(judgment_id=judgment_id, case_digest="sha256:" + "c" * 64, kind=kind, verdict=verdict,
                           body={"jev_model": "openrouter/jev", "renewal_of_version": prior,
                                 "decided_at": "2026-10-09T21:00:00+00:00"})


def test_facts_join_the_judgment_and_its_verified_case():
    store = SimpleNamespace(get=lambda j: stored_judgment(j) if j == "jdg-1" else None)
    reader = Plan1Judgments(store, read_case=lambda digest: CASE,
                            renewals_of=lambda v: [stored_judgment("jdg-2", "SHADOW", "RENEWAL", v)])
    facts = reader.get("jdg-1")
    assert (facts.verdict, facts.model_id, facts.conids, facts.file_hash) == (
        "DEPLOY", "openrouter/jev", (265598, 272093), CASE.strategy_file_hash)
    assert (facts.strategy_path, facts.class_name, facts.params) == ("strategies/orb.py", "Orb", {"RANGE_MINUTES": 15})
    assert reader.get("jdg-x") is None
    assert reader.renewal_verdicts("sha256:" + "d" * 64) == ("SHADOW",)


def test_no_renewal_verdict_exists_before_plan_5():
    reader = Plan1Judgments(SimpleNamespace(get=lambda j: None), read_case=lambda digest: CASE)
    assert reader.renewal_verdicts("sha256:" + "d" * 64) == ()


def test_cooldown_reads_plan_1_in_one_transaction(monkeypatch):
    import trader.automation.ai_judgment_port as port
    seen = []

    def cooling_until(conn, key, today):
        seen.append((key, today))
        return dt.date(2026, 10, 22) if key == "k" else None
    monkeypatch.setattr(port, "cooling_until_in_tx", cooling_until)
    db = SimpleNamespace(transaction=lambda fn: fn("conn"))
    cooldowns = Plan1Cooldowns(db)
    late_evening = dt.datetime(2026, 10, 9, 1, 30, tzinfo=dt.timezone.utc)          # still 8 Oct in New York
    assert cooldowns.cooling_down("k", late_evening) is True
    assert cooldowns.cooling_down("other", late_evening) is False
    assert seen[0] == ("k", dt.date(2026, 10, 8))


def test_strategy_key_is_file_and_class():
    assert strategy_key("strategies/x.py", "Foo") == "strategies/x.py:Foo"


def test_real_judgment_store_and_signed_case_give_the_facts(tmp_path):
    w = world(tmp_path)
    digest = finished(w)
    assert w.judgments.record(judgment(digest, "DEPLOY"))["status"] == "RECORDED"
    reader = judgment_reader_for(w.judgments, cases_dir=w.keys.cases_dir, verify_dir=w.keys.verify_dir)
    facts = reader.get("jdg-00000001")
    assert (facts.verdict, facts.kind, facts.renews_version, facts.model_id) == (
        "DEPLOY", "INITIAL", None, "openrouter/jev-1")
    assert (facts.strategy_path, facts.class_name) == tuple(KEY.split(":"))
    assert (facts.file_hash, facts.params, facts.conids) == (FILE_HASH, {"RANGE_MINUTES": 15}, (265598, 272093))
    assert (facts.bar_size, facts.artifact_id, facts.family_id) == ("5 mins", "art-1", "fam-1")
    assert reader.get("jdg-unknown") is None


def test_a_tampered_judgment_row_is_an_error_not_a_missing_judgment(tmp_path):
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "DEPLOY"))
    w.db.execute("UPDATE backtest_judgments SET verdict = 'SHADOW' WHERE judgment_id = 'jdg-00000001'")
    reader = judgment_reader_for(w.judgments, cases_dir=w.keys.cases_dir, verify_dir=w.keys.verify_dir)
    with pytest.raises(JudgmentRefused) as refused:
        reader.get("jdg-00000001")
    assert refused.value.code == "JUDGMENT_TAMPERED"


def test_real_cooldown_covers_the_rejected_strategy_until_its_last_session(tmp_path):
    w = world(tmp_path)
    insert_reject(w.db, KEY, dt.date(2026, 10, 22))
    cooldowns = cooldown_reader_for(w.db)
    assert cooldowns.cooling_down(KEY, NOW) is True
    assert cooldowns.cooling_down("strategies/other.py:Other", NOW) is False
    after_last_session = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)
    assert cooldowns.cooling_down(KEY, after_last_session) is False
