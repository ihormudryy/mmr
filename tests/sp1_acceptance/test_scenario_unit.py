"""Plan 6 Task 2: the acceptance scenario against a scripted port, the journal and the signed report."""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from tests.sp1_acceptance.fakes import AAPL, MSFT, NOW
from trader.acceptance.journal import JournalError, RunJournal
from trader.acceptance.report import AcceptanceReport, build_report
from trader.acceptance.resume import validate_resume
from trader.acceptance.scenario import AcceptanceScenario, decision_id

ROOT = Path(__file__).resolve().parents[2]


def later(minutes):
    return NOW + dt.timedelta(minutes=minutes)


def test_run_sends_the_spec_sequence_with_the_right_principals(fake_port, settings, journal):
    from tests.sp1_acceptance.fakes import stop, target
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=2)])
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert all(r.passed for r in results), results
    assert fake_port.calls == [
        ("supervisor", "get_acceptance_preflight"), ("supervisor", "get_acceptance_preflight"),
        ("supervisor", "get_experiment"),
        ("research", "register_ai_deployment"), ("supervisor", "publish_ai_risk_policy"),
        ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),     # ENTER A
        ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),     # ENTER B
        ("supervisor", "submit_ai_paper_decision"),                                     # PARTIAL_CLOSE A
        ("supervisor", "submit_ai_paper_decision"),                                     # CLOSE A
        ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),     # ENTER S, with a target
        ("operator", "acceptance_mark_start"),
        ("operator", "acceptance_shrink_probe"),
        ("supervisor", "submit_ai_paper_decision")]                                     # settle_s: CLOSE S
    assert [r.name for r in results] == ["preflight", "register", "publish", "enter_a", "enter_b",
                                         "partial_close_a", "close_a", "enter_s", "shrink_proof", "settle_s"]
    assert results[-2].evidence["oca_shrink"] == "PROVEN" and results[-1].evidence["settled_by"] == "close"
    assert AcceptanceScenario(fake_port, settings, journal).planned_calls() == fake_port.calls


def test_a_port_without_the_operator_channel_reads_the_preflight_and_writes_nothing(settings, journal):
    from tests.sp1_acceptance.fakes import FakePort
    port = FakePort(operator=False)
    results = AcceptanceScenario(port, settings, journal).run()
    assert [(r.name, r.code) for r in results] == [("preflight", None),
                                                   ("operator_channel", "OPERATOR_CHANNEL_UNAVAILABLE")]
    assert not port.writes() and journal.entries() == []


def test_decision_ids_are_deterministic_and_valid(settings):
    ids = [decision_id(settings.run_id, s) for s in ("e-a", "e-b", "pc-a", "c-a", "e-s", "c-s")]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", i) for i in ids) and len(set(ids)) == 6
    assert ids == [decision_id(settings.run_id, s) for s in ("e-a", "e-b", "pc-a", "c-a", "e-s", "c-s")]


def test_deployment_record_is_honest(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run()
    dep = fake_port.body_of("register_ai_deployment")["deployment"]
    assert dep["strategy_digest"] == "sha256:" + hashlib.sha256(settings.strategy_bytes).hexdigest()
    assert (dep["decider"], dep["decider_verdict"], dep["evidence_ref"]) == (
        "acceptance_harness", "DEPLOY", f"acceptance:{settings.run_id}")
    assert sorted(dep["conids"]) == sorted([settings.conid_a, settings.conid_b])
    assert dep["evidence_order_notional"] == settings.notional and dep["style"] == "intraday_long"


@pytest.mark.parametrize("state", [None, "PAUSED", "KILLED", "STOPPED"])
def test_refuses_unless_an_experiment_is_armed(fake_port, settings, journal, state):
    fake_port.experiment_state = state
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert (results[-1].name, results[-1].code) == ("preflight", "EXPERIMENT_NOT_ARMED")
    assert not fake_port.writes()


def test_a_dirty_account_stops_at_the_preflight_with_no_write(fake_port, settings, journal):
    fake_port.positions[MSFT] = 5.0
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert (results[-1].name, results[-1].code) == ("preflight", "POSITIONS_OPEN") and not fake_port.writes()


def test_a_wrong_account_stops_at_the_preflight(fake_port, settings, journal):
    from dataclasses import replace
    results = AcceptanceScenario(fake_port, replace(settings, account_id="DU999999"), journal).run()
    assert results[-1].code == "ACCOUNT_MISMATCH" and not fake_port.writes()


def test_notional_too_small_refuses_before_the_entry(fake_port, settings, journal):
    fake_port.ask = 1_000.0                                       # 3 x 1000 > 2000
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert results[-1].code == "HARNESS_NOTIONAL_TOO_SMALL" and "submit_ai_paper_decision" not in fake_port.methods()


def test_a_timeout_fails_the_step_and_sends_nothing_more(fake_port, settings, journal):
    fake_port.never_fill("e-a")
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert results[-1].code == "ENTRY_NOT_PROTECTED"
    assert fake_port.methods().count("submit_ai_paper_decision") == 1
    assert fake_port.clock - NOW >= dt.timedelta(seconds=120)


def test_the_scenario_never_reads_get_open_orders(fake_port, settings, journal):          # guards ruling 14
    from tests.sp1_acceptance.fakes import stop, target
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=2)])
    scenario = AcceptanceScenario(fake_port, settings, journal)
    scenario.run()
    fake_port.equity_row = {"end_state": "FLAT", "open_positions": 0}
    scenario.finish()
    assert "get_open_orders" not in fake_port.methods_used_for_assertions()


def test_resume_validation_accepts_the_journals_own_position_and_stop_and_refuses_extras(fake_port, settings, journal):
    journal.record_receipt("enter_a")
    fake_port.hold(A=3, stop=True)
    assert validate_resume(journal, fake_port).passed
    fake_port.hold(extra_position=MSFT)
    assert "RESUME_UNKNOWN_POSITION" in validate_resume(journal, fake_port).failures


def test_resume_refuses_a_foreign_working_order(fake_port, settings, journal):
    journal.record_receipt("enter_a")
    fake_port.hold(A=3, stop=True)
    fake_port.orders.append(fake_port._row(AAPL, "entry", 5, group="og-somebody-else"))
    assert "RESUME_UNKNOWN_ORDER" in validate_resume(journal, fake_port).failures


def test_the_intent_record_holds_the_exact_body_and_resume_replays_those_bytes(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run_until("enter_a", crash_before_receipt=True)
    stored = journal.entries()[-1]["body_json"]
    assert journal.entries()[-1]["kind"] == "intent"
    fake_port.forget_commands()                              # the command never reached the ledger
    fake_port.positions.clear()
    fake_port.orders.clear()
    resumed = AcceptanceScenario(fake_port, settings, journal, now=lambda: later(5)).resume()
    assert fake_port.sent_bodies("submit_ai_paper_decision")[:2] == [stored, stored]   # same bytes, same expires_at
    assert [r.name for r in resumed][:5] == ["preflight", "register", "publish", "enter_a", "enter_b"]


def test_resume_takes_a_found_command_as_the_receipt_and_sends_nothing(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run_until("enter_a", crash_before_receipt=True)
    AcceptanceScenario(fake_port, settings, journal, now=lambda: later(5)).run()
    assert fake_port.decision_ids().count(decision_id(settings.run_id, "e-a")) == 1


def test_resume_with_an_expired_body_and_no_command_fails_without_a_new_decision(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run_until("enter_a", crash_before_receipt=True)
    fake_port.forget_commands()
    r = AcceptanceScenario(fake_port, settings, journal, now=lambda: later(30)).resume()
    assert r[-1].code == "RESUME_EXPIRED_NO_COMMAND" and fake_port.methods().count("submit_ai_paper_decision") == 1


def test_resume_never_runs_the_flat_account_preflight(fake_port, settings, journal):
    scenario = AcceptanceScenario(fake_port, settings, journal)
    scenario.crash_after("enter_a")
    from trader.acceptance.scenario import SimulatedCrash
    with pytest.raises(SimulatedCrash):
        scenario.run()
    before = fake_port.methods().count("get_acceptance_preflight")
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert "POSITIONS_OPEN" not in [r.code for r in results]
    assert fake_port.methods().count("get_acceptance_preflight") == before + 1     # validate_resume's one read
    assert fake_port.decision_ids().count(decision_id(settings.run_id, "e-a")) == 1


def test_partial_close_needs_done_and_both_legs_in_one_oca_group(fake_port, settings, journal):
    fake_port.after_partial(liquidation_state="DONE", legs=[("stop", 2, "g1"), ("take_profit", 2, "g2")])
    assert AcceptanceScenario(fake_port, settings, journal).run()[-1].code == "REPROTECT_NOT_LINKED"


def test_a_partial_close_that_ended_closed_fails_the_step(fake_port, settings, journal):
    fake_port.after_partial(liquidation_state="CLOSED", legs=[])
    result = AcceptanceScenario(fake_port, settings, journal).run()[-1]
    assert (result.name, result.code) == ("partial_close_a", "PARTIAL_ENDED_CLOSED")


def test_finish_passes_only_on_a_flat_equity_row_and_empty_broker(fake_port, settings, journal):
    from tests.sp1_acceptance.fakes import stop, target
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=2)])
    AcceptanceScenario(fake_port, settings, journal).run()
    fake_port.positions[MSFT] = 0.0
    fake_port._cancel(MSFT)
    fake_port.trips_rows = [
        {"conid": AAPL, "closed_quantity": 3.0, "state": "CLOSED"},
        {"conid": AAPL, "closed_quantity": 3.0, "state": "CLOSED"},
        {"conid": MSFT, "closed_quantity": 1.0, "state": "CLOSED"}]
    fake_port.equity_row = {"end_state": "FLAT", "open_positions": 0}
    end = AcceptanceScenario(fake_port, settings, journal).finish()
    assert all(r.passed for r in end), end
    fake_port.equity_row = {"end_state": "FAILED_SAFE", "open_positions": 1}
    end = AcceptanceScenario(fake_port, settings, journal).finish()
    assert not all(r.passed for r in end)
    assert {r.name: r.code for r in end}["session_flat"] == "FAILED_SAFE"


def test_finish_waits_for_the_equity_row_then_fails(fake_port, settings, journal):
    end = AcceptanceScenario(fake_port, settings, journal).finish()
    assert {r.name: r.code for r in end}["session_flat"] == "EQUITY_ROW_MISSING"
    assert fake_port.clock - NOW >= dt.timedelta(minutes=20)


def test_journal_is_append_only_and_private(tmp_path):
    j = RunJournal(tmp_path / "acc-1")
    j.append("step", {"name": "a", "passed": True})
    j.append("step", {"name": "b", "passed": False})
    assert stat.S_IMODE(os.stat(tmp_path / "acc-1" / "journal.jsonl").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "acc-1").st_mode) == 0o700
    assert [e["name"] for e in j.entries()] == ["a", "b"]


def test_journal_refuses_a_symlink(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "acc-2").mkdir()
    (tmp_path / "real" / "x").write_text("")
    os.symlink(tmp_path / "real" / "x", tmp_path / "acc-2" / "journal.jsonl")
    with pytest.raises(JournalError):
        RunJournal(tmp_path / "acc-2")


def _passing_report(**changes):
    steps = [{"name": "enter_a", "passed": True, "code": None, "evidence": {}}]
    fields = dict(steps=steps, oca_shrink="PROVEN", evidence_source="ib_paper")
    fields.update(changes)
    return build_report(**fields)


def test_report_signature_round_trip_and_tamper(tmp_path):
    from trader.research.signing import AttestationSigner
    signer = AttestationSigner.generate()
    report = _passing_report()
    report.sign(signer, key_source="operator")
    assert report.passed is True
    path = report.write(tmp_path / "report.json")
    loaded = AcceptanceReport.load(path)
    loaded.verify(signer.public_key)
    loaded.oca_shrink = "UNPROVEN"
    with pytest.raises(Exception):
        loaded.verify(signer.public_key)
    other = AttestationSigner.generate()
    with pytest.raises(Exception):
        AcceptanceReport.load(path).verify(other.public_key)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_report_flags_a_fixture_untested_restart_and_the_signing_key_kind():   # rulings 18-22
    from trader.research.signing import AttestationSigner
    report = _passing_report()
    assert (report.deployment_record, report.live_restart_recovery, report.telegram_live_delivery) == (
        "harness_fixture", "NOT_PROVEN", "UNTESTED")
    report.sign(AttestationSigner.generate(), key_source="ephemeral")
    assert report.signing_key == "ephemeral" and report.passed is False          # a real report needs the operator key


@pytest.mark.parametrize("change", [{"oca_shrink": "UNPROVEN"}, {"evidence_source": "synthetic"},
                                    {"steps": [{"name": "x", "passed": False, "code": "E", "evidence": {}}]}])
def test_a_report_passes_only_with_a_live_proven_shrink_and_every_step(change):
    from trader.research.signing import AttestationSigner
    report = _passing_report(**change)
    report.sign(AttestationSigner.generate(), key_source="operator")
    assert report.passed is False


def test_a_synthetic_proven_never_marks_the_real_session_accepted():
    assert build_report(oca_shrink="PROVEN", evidence_source="synthetic").passed is False


def test_acceptance_package_never_imports_a_store():
    banned = ("duckdb", "trader.data", "trader.scoreboard.store", "trader.automation.ai_deployments")
    for path in (ROOT / "trader" / "acceptance").glob("*.py"):
        tree = ast.parse(path.read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in names if m.startswith(banned)], path
