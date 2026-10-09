"""SP2c Plan 2 Task 6: the four deployment methods over signed RPC (spec 5.1 table)."""
from __future__ import annotations

import json

import pytest

from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from trader.acceptance.scenario import AcceptanceSettings, deployment_record
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.backtest_judgments import JudgmentRefused
from trader.messaging.ai_deployment_wire import WithdrawAiDeploymentRequest
from trader.messaging.principals import TRADER_ACL
from trader.research.evaluation_case import CaseRefused
from trader.messaging.typed_rpc import RpcCaller, TypedRpcRemoteError, _DispatchProblem

DIGEST = "sha256:" + "a" * 64


def record():
    return deployment_record(AcceptanceSettings(run_id="r", account_id="DU1", strategy_bytes=b"x"))


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    seeded = install_seeded_judgments(monkeypatch)
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    stack.seeded = seeded
    yield stack
    stack.close()


def seed(served):
    return seed_judged_deployment(served.composed.stack.ai_paper, served.seeded, record(),
                                  today=served.now().date())


def test_rights_are_exact():
    assert TRADER_ACL[("query", "get_active_ai_deployments")] == {"strategy"}
    assert TRADER_ACL[("query", "get_ai_deployment_version")] == {"cli", "dashboard", "ai_supervisor", "ai_research"}
    assert TRADER_ACL[("command", "withdraw_ai_deployment")] == {"cli", "dashboard"}


@pytest.mark.parametrize("principal,method,body", [
    ("cli", "get_active_ai_deployments", {}), ("ai_supervisor", "get_active_ai_deployments", {}),
    ("ai_research", "withdraw_ai_deployment", {"version_digest": DIGEST, "reason": "x"}),
    ("strategy", "get_ai_deployment_version", {"version_digest": DIGEST})])
def test_callers_outside_the_row_are_denied(served, principal, method, body):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call(principal, method, body)
    assert exc.value.code == "PERMISSION_DENIED"


def test_strategy_reads_the_active_set_and_an_operator_withdraws(served):
    _base, version = seed(served)
    active = served.call("strategy", "get_active_ai_deployments", {})
    assert [d["version_digest"] for d in active["deployments"]] == [version]
    assert served.call("ai_research", "get_ai_deployment_version", {"version_digest": version})["version"][
        "state"] == "ACTIVE"
    receipt = served.call("cli", "withdraw_ai_deployment", {"version_digest": version, "reason": "operator"})
    assert receipt["state"] == "RESOLVED" and receipt["outcome"]["already_withdrawn"] is False
    assert served.call("strategy", "get_active_ai_deployments", {})["deployments"] == []


def test_a_withdrawal_refused_for_an_entry_in_flight_can_be_retried_after_the_send(served, monkeypatch):
    _base, version = seed(served)
    versions = served.composed.stack.ai_paper.versions
    real = versions.entries_being_sent_in_tx
    monkeypatch.setattr(versions, "entries_being_sent_in_tx", lambda conn, digest: ("aip-dec-00000001",))
    body = {"version_digest": version, "reason": "operator"}
    refused = served.call("cli", "withdraw_ai_deployment", body)         # a refusal receipt, not an RPC error
    assert (refused["state"], refused["error_code"]) == ("REJECTED", "WITHDRAWAL_ENTRY_IN_FLIGHT")
    assert "aip-dec-00000001" in refused["outcome"]["message"] and "retry" in refused["outcome"]["message"]
    again = served.call("cli", "withdraw_ai_deployment", body)           # the entry is still being sent
    assert again["error_code"] == "WITHDRAWAL_ENTRY_IN_FLIGHT" and again["command_id"] != refused["command_id"]
    monkeypatch.setattr(versions, "entries_being_sent_in_tx", real)      # the send returned
    receipt = served.call("cli", "withdraw_ai_deployment", body)
    assert receipt["state"] == "RESOLVED" and receipt["outcome"]["already_withdrawn"] is False
    assert served.call("cli", "withdraw_ai_deployment", body)["command_id"] == receipt["command_id"]   # now it replays


def test_registration_without_a_judgment_is_refused_on_the_wire(served):
    receipt = served.call("ai_research", "register_ai_deployment", {
        "judgment_id": "jdg-none", "bundle_digest": "sha256:" + "b" * 64, "deployment": record()})
    assert (receipt["state"], receipt["error_code"]) == ("REJECTED", "JUDGMENT_MISSING")
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_research", "register_ai_deployment", {"deployment": record()})
    assert exc.value.code == "VALIDATION_ERROR"


def test_registration_uses_the_judgment_bound_command_id(served):
    body = {"judgment_id": "jdg-none", "bundle_digest": "sha256:" + "b" * 64, "deployment": record()}
    served.call("ai_research", "register_ai_deployment", body)
    expected = served.composed.stack.ai_paper.actions.registration_command_id(
        {**body, "deployment": json.loads(json.dumps(body["deployment"]))})
    rows = served.trader.journal_db.execute(
        "SELECT command_id FROM command_ledger WHERE action = 'register_ai_deployment'", fetch="all")
    assert [row[0] for row in rows] == [expected]


def test_the_version_migrations_are_applied_to_the_trader_journal(served):
    versions = {row[0] for row in served.trader.journal_db.execute(
        "SELECT version FROM schema_migrations", fetch="all")}
    assert {115, 116} <= versions


def test_every_ai_paper_store_shares_the_one_journal_connection(served):
    services = served.composed.stack.ai_paper
    journal = served.trader.journal_db
    assert services.versions._db is journal and services.registrar._db is journal
    assert services.deployments._db is journal and services.judgments._db is journal
    assert served.trader.ai_deployment_versions is services.versions
    assert served.trader.ai_deployment_activity is services.activity


def test_a_bad_withdraw_body_is_a_validation_error(served):
    for body in ({"version_digest": "nope", "reason": "x"}, {"version_digest": DIGEST, "reason": ""},
                 {"version_digest": DIGEST, "reason": "x" * 201}, {"version_digest": DIGEST, "reason": "x", "y": 1},
                 {"version_digest": DIGEST + "\n", "reason": "x"}):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.call("cli", "withdraw_ai_deployment", body)
        assert exc.value.code == "VALIDATION_ERROR"


def test_the_wire_model_is_strict():
    with pytest.raises(ValueError):
        WithdrawAiDeploymentRequest.model_validate({"version_digest": DIGEST, "reason": 1})


# -- every handler checks its caller again, behind an open allow-list --------------------------------

@pytest.fixture
def open_registry(served, monkeypatch):
    """The production handlers on a registry whose allow-list lets every principal in."""
    from trader.messaging.production_api import register_ai_paper_authority
    from trader.messaging.typed_rpc import TypedRpcRegistry
    registry = TypedRpcRegistry(acl=None)
    register_ai_paper_authority(registry, served.composed.stack.coordinator, served.composed.stack.ai_paper,
                                account_id=served.trader.ib_account)
    return registry


def call_open(registry, served, principal, method, body):
    registration = next(r for r in registry._by_role_method.values() if r.method == method)
    parsed = registration.request_model.model_validate(body)
    return registration.handler(parsed, RpcCaller(principal=principal, on_behalf_of=None))


@pytest.mark.parametrize("principal,method,body", [
    ("cli", "get_active_ai_deployments", {}),
    ("strategy", "get_ai_deployment_version", {"version_digest": DIGEST}),
    ("research", "get_ai_deployment_version", {"version_digest": DIGEST}),
    ("strategy", "withdraw_ai_deployment", {"version_digest": DIGEST, "reason": "x"}),
    ("ai_supervisor", "withdraw_ai_deployment", {"version_digest": DIGEST, "reason": "x"})])
def test_each_handler_refuses_a_caller_outside_its_row(served, open_registry, principal, method, body):
    with pytest.raises(_DispatchProblem) as exc:
        call_open(open_registry, served, principal, method, body)
    assert exc.value.code == "PERMISSION_DENIED"
    ledger = served.trader.journal_db.execute(
        "SELECT count(*) FROM command_ledger WHERE action = 'withdraw_ai_deployment'", fetch="one")
    assert ledger[0] == 0


# -- a tampered record is an RPC error, never a reply body ---------------------------------------------

def tamper_version(served, version):
    served.trader.journal_db.execute(
        "UPDATE ai_deployment_versions SET record_json = replace(record_json, '\"INITIAL\"', '\"RENEWAL\"') "
        "WHERE digest = ?", [version])


def test_a_tampered_version_is_an_rpc_error_on_every_method(served):
    _base, version = seed(served)
    tamper_version(served, version)
    for principal, method, body in (
            ("ai_research", "get_ai_deployment_version", {"version_digest": version}),
            ("strategy", "get_active_ai_deployments", {})):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.call(principal, method, body)
        assert exc.value.code == "DEPLOYMENT_VERSION_TAMPERED", (method, exc.value.code)


def test_an_operator_can_still_withdraw_a_tampered_version(served):
    _base, version = seed(served)
    tamper_version(served, version)
    receipt = served.call("cli", "withdraw_ai_deployment", {"version_digest": version, "reason": "tampered"})
    assert receipt["state"] == "RESOLVED"


@pytest.mark.parametrize("refusal,code", [(JudgmentRefused("JUDGMENT_TAMPERED", "d"), "JUDGMENT_TAMPERED"),
                                          (JudgmentRefused("COOLDOWN_CALENDAR_UNAVAILABLE", "d"),
                                           "COOLDOWN_CALENDAR_UNAVAILABLE"),
                                          (DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", "m"),
                                           "DEPLOYMENT_VERSION_TAMPERED"),
                                          (DeploymentRefused("DEPLOYMENT_CALENDAR_UNAVAILABLE", "m"),
                                           "DEPLOYMENT_CALENDAR_UNAVAILABLE"),
                                          (CaseRefused("CASE_NOT_FOUND", "d"), "CASE_NOT_FOUND"),
                                          (CaseRefused("CASE_SIGNATURE_INVALID", "d"), "CASE_SIGNATURE_INVALID")])
def test_registration_turns_a_loud_refusal_into_an_rpc_error(served, monkeypatch, refusal, code):
    def refuse(_judgment_id):
        raise refusal
    monkeypatch.setattr(served.seeded, "get", refuse)
    body = {"judgment_id": "jdg-x", "bundle_digest": "sha256:" + "b" * 64, "deployment": record()}
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_research", "register_ai_deployment", body)
    assert exc.value.code == code
    # The ledger keeps the real code as a clean refusal: never INTERNAL_ERROR, never a parked OUTCOME_UNKNOWN.
    (row,) = served.trader.journal_db.execute(
        "SELECT state, error_code FROM command_ledger WHERE action = 'register_ai_deployment'", fetch="all")
    assert tuple(row) == ("REJECTED", code)
    with pytest.raises(TypedRpcRemoteError) as again:                      # a same-day retry replays the refusal
        served.call("ai_research", "register_ai_deployment", body)
    assert again.value.code == code


def test_deployment_status_clock_must_be_timezone_aware():
    import datetime as dt

    from trader.trading.command_stack import _aware_clock
    assert _aware_clock(lambda: dt.datetime(2026, 10, 9, 15, tzinfo=dt.timezone.utc))().tzinfo is not None
    with pytest.raises(ValueError, match="timezone-aware"):
        _aware_clock(lambda: dt.datetime(2026, 10, 9, 15))()


@pytest.mark.parametrize("code", ["CASE_NOT_FOUND", "CASE_VERIFY_KEYS_MISSING", "CASE_DIGEST_MISMATCH"])
def test_an_unreadable_case_is_an_rpc_error_on_both_reads(served, monkeypatch, code):
    _base, version = seed(served)

    def refuse(_judgment_id):
        raise CaseRefused(code, "d")
    monkeypatch.setattr(served.seeded, "get", refuse)
    for principal, method, body in (("ai_research", "get_ai_deployment_version", {"version_digest": version}),
                                    ("strategy", "get_active_ai_deployments", {})):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.call(principal, method, body)
        assert exc.value.code == code, method


ALL_PRINCIPALS = ("cli", "dashboard", "ai_supervisor", "ai_research", "strategy", "research", "scheduler", "trader")


@pytest.mark.parametrize("method,role,body", [
    ("get_ai_deployment_version", "query", {"version_digest": DIGEST}),
    ("get_active_ai_deployments", "query", {}),
    ("withdraw_ai_deployment", "command", {"version_digest": DIGEST, "reason": "x"})])
def test_each_handlers_own_set_is_its_acl_row(served, open_registry, method, role, body):
    for principal in ALL_PRINCIPALS:
        try:
            call_open(open_registry, served, principal, method, body)
            denied = False
        except _DispatchProblem as problem:
            denied = problem.code == "PERMISSION_DENIED"
        assert denied == (principal not in TRADER_ACL[(role, method)]), (method, principal)
