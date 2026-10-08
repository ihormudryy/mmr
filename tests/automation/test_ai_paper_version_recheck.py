"""SP2c Plan 2 Task 7: the deployment version recheck at admission (spec 5.2 item 8, spec 9).

Exits never depend on it. A state that cannot be read refuses an entry as retryable and never admits one.
"""
from __future__ import annotations

import datetime as dt
import logging

import pytest

from tests.automation.ai_paper_fixtures import CONID
from tests.automation.ai_paper_world import GOOD, World
from tests.automation.discretionary_world import discretionary_world
from tests.automation.test_ai_paper_reductions import close_body, partial_body
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.backtest_judgments import JudgmentRefused
from trader.research.evaluation_case import CaseRefused

KEY = "strategies/opening_range_breakout.py:OpeningRangeBreakout"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, real_liquidation=True)


def test_an_active_version_enters_and_the_row_keeps_the_binding(world):
    assert world.submit().state == "SUBMITTED"
    row = world.decisions.row("dec-00000001")
    assert (row.deployment_version, row.source_digest) == (world.version_digest, GOOD["strategy_digest"])


@pytest.mark.parametrize("change,code", [
    ({"deployment_version": None, "source_digest": None}, "DEPLOYMENT_VERSION_REQUIRED"),
    ({"source_digest": "sha256:" + "f" * 64}, "STRATEGY_SOURCE_MISMATCH"),
    ({"deployment_version": "sha256:" + "e" * 64}, "DEPLOYMENT_NOT_ACTIVE")])
def test_admission_refuses_a_bad_binding(world, change, code):
    receipt = world.submit(**change)
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", code, False)
    assert world.dispatch.plans == []


def test_a_version_of_another_base_deployment_is_not_active(world):
    other = world.deployments.register(
        type(world.deployments.get_sealed(world.digest)).from_json({**GOOD, "evidence_ref": "trial:43"}),
        principal="ai_research", command_id="dep-2")[0]
    receipt = world.submit(deployment_digest=other, deployment_version=world.version_digest,
                           source_digest=GOOD["strategy_digest"])
    assert receipt.error_code == "DEPLOYMENT_NOT_ACTIVE"


def test_an_expired_version_is_refused_at_admission(world):
    world.version_digest = world.seal_version("jdg-old", first=dt.date(2026, 6, 1), expiry=dt.date(2026, 7, 16))
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_EXPIRED")
    assert world.dispatch.plans == []


def test_a_cooldown_refuses_at_admission(world):
    world.cooldowns.keys.add(KEY)
    assert world.submit().error_code == "FAMILY_COOLING_DOWN"


def test_a_withdrawn_version_is_refused_at_admission(world):
    world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w1")
    assert world.submit().error_code == "DEPLOYMENT_NOT_ACTIVE"


def test_a_version_whose_first_session_is_ahead_is_not_active(world):
    world.version_digest = world.seal_version("jdg-future", first=dt.date(2026, 7, 20), expiry=dt.date(2026, 8, 14))
    assert world.activity.status(world.version_digest) == "NOT_STARTED"
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE", False)
    assert world.dispatch.plans == []


def test_a_version_over_the_active_cap_is_not_active(world):
    for n in range(3):                                   # max_active=3; earlier first sessions rank first
        world.seal_version(f"jdg-early-{n}", first=dt.date(2026, 7, 13))
    assert world.activity.status(world.version_digest) == "OVER_CAP"
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE", False)
    assert world.dispatch.plans == []


def test_a_discretionary_enter_must_carry_no_binding(tmp_path):
    world = discretionary_world(tmp_path)
    receipt = world.submit(deployment_digest=world.ddigest, deployment_version=world.version_digest,
                           source_digest=GOOD["strategy_digest"])
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_VERSION_UNEXPECTED")


def test_an_unwired_activity_fails_closed(world):
    world.service._activity = None
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "DEPLOYMENT_STATE_UNAVAILABLE", True)
    assert world.dispatch.plans == []


class _Unreadable:
    """The ways a version state can fail to read; each is raised, never returned."""

    @staticmethod
    def tampered_row(world):
        world.db.execute("UPDATE ai_deployment_versions SET record_json = replace(record_json, '2026-08-14', "
                         "'2026-12-31')")

    @staticmethod
    def judgment_tampered(world):
        def refuse(judgment_id):
            raise JudgmentRefused("JUDGMENT_TAMPERED", "the judgment row does not match its digest")
        world.judgments.get = refuse

    @staticmethod
    def case_tampered(world):
        def refuse(judgment_id):
            raise CaseRefused("CASE_TAMPERED", "the case file does not match its digest")
        world.judgments.get = refuse

    @staticmethod
    def deployment_tampered(world):
        def refuse(digest):
            raise DeploymentRefused("DEPLOYMENT_TAMPERED", "the sealed deployment does not match its digest")
        world.deployments.get_sealed = refuse

    @staticmethod
    def database_error(world):
        def fail():
            raise RuntimeError("database is locked")
        world.versions.sealed = fail


UNREADABLE = [_Unreadable.tampered_row, _Unreadable.judgment_tampered, _Unreadable.case_tampered,
              _Unreadable.deployment_tampered, _Unreadable.database_error]


@pytest.mark.parametrize("break_state", UNREADABLE, ids=lambda f: f.__name__)
def test_an_unreadable_state_refuses_the_entry_as_retryable(world, break_state, caplog):
    break_state(world)
    with caplog.at_level(logging.ERROR, logger="trader.automation.ai_paper_decision"):
        receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "DEPLOYMENT_STATE_UNAVAILABLE", True)
    assert world.dispatch.plans == []
    assert world.decisions.row("dec-00000001").error_code == "DEPLOYMENT_STATE_UNAVAILABLE"
    assert [r for r in caplog.records if "state unavailable" in r.getMessage()]


def test_the_log_names_the_exception_type_and_code(world, caplog):
    _Unreadable.judgment_tampered(world)
    with caplog.at_level(logging.ERROR, logger="trader.automation.ai_paper_decision"):
        world.submit()
    (record,) = [r for r in caplog.records if "state unavailable" in r.getMessage()]
    assert "JudgmentRefused" in record.getMessage() and "JUDGMENT_TAMPERED" in record.getMessage()
    assert record.exc_info is not None      # the traceback keeps the cause


@pytest.mark.parametrize("break_state", UNREADABLE, ids=lambda f: f.__name__)
@pytest.mark.parametrize("body_for", [close_body, partial_body], ids=["close", "partial_close"])
def test_a_reduction_passes_when_every_deployment_state_is_unreadable(tmp_path, break_state, body_for):
    world = World(tmp_path, real_liquidation=True)
    world.owned(CONID, 300.0)
    for break_all in UNREADABLE:
        break_all(world)
    receipt = world.submit(body_for(world, decision_id="dec-close-01"))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")


def test_a_reduction_passes_when_the_version_is_withdrawn_expired_and_cooling(tmp_path):
    world = World(tmp_path, real_liquidation=True)
    world.owned(CONID, 300.0)
    world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w1")
    world.cooldowns.keys.add(KEY)
    world.activity_clock.advance(days=60)
    receipt = world.submit(close_body(world, decision_id="dec-close-01"))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")


# Task 8: the same rules again at final dispatch, inside the guard, right before the send.

def test_withdrawal_between_jev_and_send_is_refused_at_dispatch(world):
    world.on_before_guard(lambda: world.versions.withdraw(world.version_digest, reason="operator",
                                                          principal="cli", command_id="w-late"))
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE")
    assert world.dispatch.plans == []


def test_expiry_between_jev_and_send_is_refused_at_dispatch(world):
    world.version_digest = world.seal_version("jdg-last", expiry=dt.date(2026, 7, 17))
    world.on_before_guard(lambda: world.activity_clock.advance(days=1))
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_EXPIRED")
    assert world.dispatch.plans == []


def test_a_cooldown_between_jev_and_send_is_refused_at_dispatch(world):
    world.on_before_guard(lambda: world.cooldowns.keys.add(KEY))
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "FAMILY_COOLING_DOWN")
    assert world.dispatch.plans == []


def test_a_judgment_that_stops_standing_between_jev_and_send_is_refused_at_dispatch(world):
    world.on_before_guard(lambda: world.judgments.end_line(world.version_digest))
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE")
    assert world.dispatch.plans == []


def test_an_unreadable_state_at_dispatch_becomes_the_guards_gate_unavailable_code(world, caplog):
    world.on_before_guard(lambda: _Unreadable.database_error(world))
    with caplog.at_level(logging.ERROR, logger="trader.automation.ai_deployment_activity"):
        receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "AI_ENTRY_GATE_UNAVAILABLE", False)
    assert world.dispatch.plans == []
    (record,) = [r for r in caplog.records if "version gate failed" in r.getMessage()]
    assert "RuntimeError" in record.getMessage() and record.exc_info is not None


def test_an_exit_on_the_same_conid_still_goes_out_after_a_late_withdrawal(world):
    world.owned(CONID, 300.0)
    world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w-late")
    receipt = world.submit(close_body(world, decision_id="dec-close-01"))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")


def test_a_reduction_never_reaches_the_version_gate(world):
    world.owned(CONID, 300.0)
    world.on_before_guard(lambda: _Unreadable.database_error(world))
    receipt = world.submit(close_body(world, decision_id="dec-close-01"))
    assert receipt.error_code == "CLOSE_PENDING"


def test_the_discretionary_scope_gate_answers_first_when_both_gates_would_refuse(world):
    world.scope_gate = lambda request, approval, quote, now: "OUT_OF_DISCRETIONARY_SCOPE"
    world.on_before_guard(lambda: world.versions.withdraw(world.version_digest, reason="operator",
                                                          principal="cli", command_id="w-late"))
    assert world.submit().error_code == "OUT_OF_DISCRETIONARY_SCOPE"


def test_the_command_stack_composes_the_version_gate_into_the_guard_options(world):
    from types import SimpleNamespace

    from trader.automation.ai_paper_decision import AI_PAPER_ACTION
    from trader.trading.command_stack import _AiPaperParts, _ai_paper_guard_options

    parts = _AiPaperParts(config=None, policy=world.policy, entry_filter=world.entry_filter,
                          deployments=world.deployments, scope_checks=None, filter_refusal=None,
                          activity=world.activity)
    gate = _ai_paper_guard_options(parts, frozenset())["ai_entry_gate"]
    request = SimpleNamespace(action=AI_PAPER_ACTION, body=world.body())
    world.versions.withdraw(world.version_digest, reason="operator", principal="cli", command_id="w1")
    assert gate(request, None, None, world.clock()) == "DEPLOYMENT_NOT_ACTIVE"
