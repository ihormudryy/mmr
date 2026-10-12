"""SP3 Plan 3 Task 9: the trader wires one authored store into SP2c and serves it (spec 5, 7.3)."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from tests.automation.ai_paper_fixtures import NOW
from tests.automation.ai_paper_world import GOOD
from tests.automation.authored_fixtures import submit_request
from tests.automation.judged_deployment import seed_judged_deployment
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from tests.test_ai_paper_rpc import _served, command, enter_body, granted_epoch, publish
from trader.automation.ai_deployment_activity import ACTIVE, WITHDRAWN
from trader.automation.ai_paper_config import AiPaperConfig
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import STORE_DIR_ENV
from trader.automation.authored_isolation import IsolationProofs
from trader.automation.authored_refusals import AUTHORED_WITHDRAWAL_FAILED
from trader.automation.authored_strategies import (
    DEPLOYED, EVALUATING, SHADOW, AuthoredStrategyStore, DeploymentLines, move_in_tx, record_in_tx,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.strategy_key import authored_strategy_path


def switch_on(trader) -> None:
    """trader.yaml with ai_paper.authored_strategies.enabled: true, as the owner sets it."""
    trader.ai_paper_config = replace(trader.ai_paper_config,
                                     authored_strategies=AuthoredStrategiesConfig(enabled=True))


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):  # noqa: F811
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "authored_store"))
    stack = served_stack(tmp_path, loop_thread, monkeypatch, prepare=switch_on)
    yield stack
    stack.close()


def test_the_trader_wires_one_authored_store_everywhere(served):
    ai_paper = served.composed.stack.ai_paper
    assert isinstance(ai_paper.authored, AuthoredStrategyStore) and isinstance(ai_paper.proofs, IsolationProofs)
    assert ai_paper.claims._authored is ai_paper.authored
    assert ai_paper.judgments._authored is ai_paper.authored
    assert ai_paper.activity._authored is ai_paper.authored is ai_paper.registrar._authored
    assert {130, 131} <= set(SchemaMigrator(served.trader.journal_db).applied_versions())


def test_ai_author_submits_through_the_production_registry(served, tmp_path):
    reply = served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
    assert reply["status"] == "STORED"
    assert list((tmp_path / "authored_store").glob("*.py"))
    view = served.call("cli", "list_authored_strategies", {})
    assert (view["enabled"], view["counted"]) == (True, 1)
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_research", "submit_authored_strategy", submit_request().model_dump())
    assert exc.value.code == "PERMISSION_DENIED"


def test_the_store_and_its_proofs_write_under_the_traders_journal_lock(served):          # wiring 1
    """Without the journal a retirement and a proof write take only the database lock, so they could commit
    unseen beside the saga's SUBMITTING row or the registration seal."""
    ai_paper, trader = served.composed.stack.ai_paper, served.trader
    assert ai_paper.authored._journal is trader.domain_journal
    assert ai_paper.proofs._journal is trader.domain_journal
    assert ai_paper.authored._proofs is ai_paper.proofs
    assert ai_paper.authored._db is trader.journal_db is ai_paper.proofs._db
    assert ai_paper.claims._db is trader.journal_db is ai_paper.judgments._db
    assert trader.domain_journal.db is trader.journal_db


def test_retirement_withdraws_through_the_real_version_store(served):                     # wiring 4
    store = served.composed.stack.ai_paper.authored
    assert store._withdraw is not None and store._lines is not None
    lines = store._lines()
    assert isinstance(lines, DeploymentLines)
    assert lines._activity is served.composed.stack.ai_paper.activity
    assert lines._versions is served.composed.stack.ai_paper.versions


# -- the real command stack with an authored deployment (test_ai_paper_rpc's served stack) --------------------


def retire_body(digest: str) -> dict:
    return {"digest": digest, "code": "SANDBOX_ERROR", "detail": "child crashed"}


@pytest.fixture
def authored_stack(tmp_path, monkeypatch):
    """The production command stack and registry with the switch on and one DEPLOYED authored record behind an
    ACTIVE judged version (seeded past the bundle and proof gates, which have their own tests)."""
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "authored_store"))
    config = AiPaperConfig(enabled=True, authored_strategies=AuthoredStrategiesConfig(enabled=True))
    served = _served(tmp_path, monkeypatch, config)
    store = served.stack.ai_paper.authored
    digest = store.submit(submit_request())["record"]["digest"]
    record = {**GOOD, "strategy_path": authored_strategy_path(digest), "strategy_digest": digest,
              "class_name": "RangeBreak", "params": {"RANGE_BARS": 15}, "evidence_ref": "trial:authored"}
    today = NOW.astimezone(ZoneInfo("America/New_York")).date()
    served.base, served.version = seed_judged_deployment(served.stack.ai_paper, served.seeded, record, today=today,
                                                         judgment_id="jdg-authored-1")
    steps = ((EVALUATING, {"evaluation_request_id": "sha256:" + "1" * 64}),
             (SHADOW, {"verdict": "DEPLOY", "judgment_id": "jdg-authored-1", "evaluation_request_id": None}),
             (DEPLOYED, {"deployment_version": served.version}))
    for target, changes in steps:
        served.trader.journal_db.transaction(lambda conn, target=target, changes=changes: move_in_tx(
            conn, record_in_tx(conn, digest), target, event="TEST", reason=None, now=NOW, **changes))
    served.store, served.digest = store, digest
    yield served
    served.close()


def arm_and_enter(served) -> dict:
    publish(served)
    started = command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
    assert started["outcome"]["state"] == "ARMED", started
    served.stack.experiments.monitor.recover()
    body = enter_body(served.base, deployment_version=served.version, source_digest=served.digest,
                      expires_at=(NOW + dt.timedelta(minutes=5)).isoformat())
    return command(served, "ai_supervisor").call("submit_ai_paper_decision", body, dict,
                                                 controller_epoch=granted_epoch(served))


def test_a_deployed_authored_version_enters_on_the_real_stack(authored_stack):
    out = arm_and_enter(authored_stack)
    assert out["state"] == "SUBMITTED", out
    assert len(authored_stack.orders.plans) == 1


@pytest.mark.parametrize("withdrawal_lands", [True, False])
def test_a_retirement_before_submitting_sends_nothing_on_the_real_stack(authored_stack, withdrawal_lands):  # wiring 3
    """The final gate passes on a DEPLOYED record; the strategy retires it before the SUBMITTING transaction.
    When the version's withdrawal does not land, only the store's send gate inside that transaction can still
    stop the broker send (the SP2c withdrawal gate sees an ACTIVE version)."""
    served = authored_stack
    if not withdrawal_lands:
        def broken(version_digest):
            raise RuntimeError("journal write failed")
        served.store._withdraw = broken
    guard = served.stack.dispatch_guard
    real = guard.revalidate

    def gate_then_retire(*args, **kwargs):
        permit = real(*args, **kwargs)
        retired = command(served, "strategy").call("retire_authored_strategy", retire_body(served.digest), dict)
        assert retired["record"]["state"] == "RETIRED", retired
        assert (served.version in served.stack.ai_paper.versions.withdrawn()) is withdrawal_lands
        return permit
    guard.revalidate = gate_then_retire
    out = arm_and_enter(served)
    assert (out["state"], out["error_code"]) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE"), out
    assert served.orders.plans == []                                   # no broker send


def test_a_strategy_retirement_withdraws_the_version_on_the_real_stack(authored_stack):   # wiring 4
    served = authored_stack
    assert served.stack.ai_paper.activity.statuses()[served.version] == ACTIVE
    reply = command(served, "strategy").call("retire_authored_strategy", retire_body(served.digest), dict)
    assert (reply["status"], reply["record"]["state"]) == ("UPDATED", "RETIRED")
    assert served.stack.ai_paper.activity.statuses()[served.version] == WITHDRAWN
    assert served.version in served.stack.ai_paper.versions.withdrawn()


def test_a_failed_withdrawal_is_a_retryable_refusal_and_a_resend_withdraws(authored_stack):  # wiring 5
    served = authored_stack
    store = served.store
    real = store._withdraw

    def broken(version_digest):
        raise RuntimeError("journal write failed")
    store._withdraw = broken
    reply = command(served, "strategy").call("retire_authored_strategy", retire_body(served.digest), dict)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", AUTHORED_WITHDRAWAL_FAILED, True)
    assert reply["record"]["state"] == "RETIRED" and store.get(served.digest).state.value == "RETIRED"
    assert served.stack.ai_paper.activity.statuses()[served.version] == ACTIVE
    store._withdraw = real
    again = command(served, "strategy").call("retire_authored_strategy", retire_body(served.digest), dict)
    assert again["status"] == "UNCHANGED"
    assert served.stack.ai_paper.activity.statuses()[served.version] == WITHDRAWN


def test_a_state_conflict_is_a_refusal_reply_not_an_rpc_error(authored_stack):            # wiring 5
    served = authored_stack
    other = served.store.submit(submit_request(
        submit_request().source.replace("RANGE_BARS = 15", "RANGE_BARS = 16")))     # STORED, never deployed
    reply = command(served, "strategy").call("retire_authored_strategy",
                                             retire_body(other["record"]["digest"]), dict)
    assert (reply["status"], reply["code"], reply["record"]) == ("REFUSED", "AUTHORED_STATE_CONFLICT", None)
    unknown = command(served, "strategy").call("retire_authored_strategy", retire_body("sha256:" + "9" * 64), dict)
    assert (unknown["status"], unknown["code"]) == ("REFUSED", "AUTHORED_STRATEGY_UNKNOWN")


def test_the_authored_methods_refuse_other_principals_on_the_real_stack(authored_stack):   # wiring 6
    served = authored_stack
    for principal in ("cli", "dashboard", "ai_supervisor", "ai_research", "research", "ai_author"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            command(served, principal).call("retire_authored_strategy", retire_body(served.digest), dict)
        assert exc.value.code == "PERMISSION_DENIED", principal
    assert served.store.get(served.digest).state == DEPLOYED


def test_catalogue_entries_ignore_the_authored_switch(tmp_path, monkeypatch):             # catalogue regression
    from tests.test_ai_paper_rpc import bound_enter_body
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "authored_store"))
    for enabled, path in ((False, tmp_path / "off"), (True, tmp_path / "on")):
        path.mkdir()
        config = AiPaperConfig(enabled=True, authored_strategies=AuthoredStrategiesConfig(enabled=enabled))
        served = _served(path, monkeypatch, config)
        try:
            publish(served)
            body = bound_enter_body(served)
            command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
            served.stack.experiments.monitor.recover()
            out = command(served, "ai_supervisor").call("submit_ai_paper_decision", body, dict,
                                                        controller_epoch=granted_epoch(served))
            assert out["state"] == "SUBMITTED", (enabled, out)
            assert len(served.orders.plans) == 1
        finally:
            served.close()


def test_a_line_lookup_failure_after_retiring_is_a_retryable_refusal(authored_stack):     # fix round 1
    served = authored_stack

    def broken_lines():
        raise RuntimeError("journal read failed")
    served.store._lines = broken_lines
    reply = command(served, "strategy").call("retire_authored_strategy", retire_body(served.digest), dict)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", AUTHORED_WITHDRAWAL_FAILED, True)
    assert reply["record"]["state"] == "RETIRED" and served.store.get(served.digest).state.value == "RETIRED"
