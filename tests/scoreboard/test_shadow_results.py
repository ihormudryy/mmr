import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, build_full_production_registry, make_identities
from tests.scoreboard.test_report import empty_inputs
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.backtest_judgments import JudgmentRefused
from trader.messaging.principals import SERVER_ACCEPTS, TRADER_ACL
from trader.messaging.shadow_surface import register_shadow_surface
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError
from trader.research.shadow_window import shadow_window
from trader.scoreboard.books import build_shadow_books
from trader.scoreboard.report import build_report
from trader.scoreboard.shadow_ingest import RecordShadowResultRequest, ShadowIngest, verified_shadow_rows

RESEARCH, CLI = SimpleNamespace(principal="research"), SimpleNamespace(principal="cli")
CASE = "sha256:" + "c" * 64
RECORDED = dt.datetime(2024, 3, 28, 21, tzinfo=dt.timezone.utc)      # after the 2024-03-28 close
CONFIG = SimpleNamespace(deploy_expiry_sessions=20, family_cooldown_sessions=10)


def body(**overrides):
    values = {"judgment_id": "j1", "case_digest": CASE, "verdict": "DEPLOY", "session_date": "2024-04-01",
              "status": "COMPLETE", "reason": None, "pnl_usd": 12.5, "fees_usd": 1.0, "trades": 2,
              "end_equity_usd": 100_012.5, "bar_size": "15 mins"}
    values.update(overrides)
    return RecordShadowResultRequest(**values)


@pytest.fixture
def ingest(store):
    judgments = {"j1": SimpleNamespace(judgment_id="j1", case_digest=CASE, verdict="DEPLOY", kind="INITIAL",
                                       recorded_at=RECORDED, cooldown_until_session=None,
                                       binding={"bar_size": "15 mins"})}
    versions = {"j1": None}
    service = ShadowIngest(store=store, judgments=SimpleNamespace(get=judgments.get),
                           versions=SimpleNamespace(version_for_judgment=versions.get), config=CONFIG,
                           now=lambda: dt.datetime(2024, 4, 1, 22, tzinfo=dt.timezone.utc))
    service.versions_map = versions
    service.judgments_map = judgments
    return service


def test_window_is_fixed_at_the_decision():
    first, last = shadow_window(RECORDED, "DEPLOY", deploy_expiry_sessions=20)
    assert first == dt.date(2024, 4, 1)                                             # 03-29 is Good Friday
    assert last == dt.date(2024, 4, 26)                                             # 20 sessions inclusive
    first, last = shadow_window(RECORDED, "REJECT", deploy_expiry_sessions=20, cooldown_until_session=dt.date(2024, 4, 12))
    assert first == dt.date(2024, 4, 1) and last == dt.date(2024, 4, 26)            # cooldown end + 10 sessions


def test_a_reject_window_follows_the_sealed_cooldown_even_for_a_pre_open_record():
    from trader.automation.backtest_judgments import nth_session_after
    from trader.automation.calendar_policy import XNYSCalendarPolicy
    calendar = XNYSCalendarPolicy()
    recorded = dt.datetime(2024, 4, 4, 12, tzinfo=dt.timezone.utc)                 # 08:00 New York, before the open
    cooldown_until = nth_session_after(calendar, dt.date(2024, 4, 4), 10)
    first, last = shadow_window(recorded, "REJECT", deploy_expiry_sessions=20, cooldown_until_session=cooldown_until)
    assert first == dt.date(2024, 4, 5)
    assert last == nth_session_after(calendar, cooldown_until, 10)


def test_window_refuses_a_naive_time_an_untracked_verdict_and_a_reject_without_cooldown():
    with pytest.raises(ValueError):
        shadow_window(dt.datetime(2024, 3, 28, 21), "DEPLOY", deploy_expiry_sessions=20)
    with pytest.raises(ValueError):
        shadow_window(RECORDED, "NO_VERDICT", deploy_expiry_sessions=20)
    with pytest.raises(ValueError):
        shadow_window(RECORDED, "REJECT", deploy_expiry_sessions=20)


def test_a_repeat_is_a_no_op_and_a_changed_row_is_refused(ingest, store):
    assert ingest.record(body(), RESEARCH)["status"] == "INSERTED"
    assert ingest.record(body(), RESEARCH)["status"] == "DUPLICATE"
    refused = ingest.record(body(pnl_usd=13.0, end_equity_usd=100_013.0), RESEARCH)
    assert (refused["status"], refused["code"]) == ("REFUSED", "CONFLICTING_DUPLICATE")
    assert len(store.fetch("shadow_results", {})) == 1 and store.verify_seals() == []


def test_a_registration_between_two_sends_does_not_make_a_second_row(ingest, store):
    ingest._now = lambda: dt.datetime(2024, 4, 3, 22, tzinfo=dt.timezone.utc)
    ingest.record(body(), RESEARCH)
    ingest.versions_map["j1"] = "sha256:" + "v" * 64
    assert ingest.record(body(), RESEARCH)["status"] == "DUPLICATE"
    ingest.record(body(session_date="2024-04-02", end_equity_usd=100_020.0, pnl_usd=7.5), RESEARCH)
    rows = store.fetch("shadow_results", {})
    assert [r["deployment_version"] for r in rows] == [None, "sha256:" + "v" * 64]


@pytest.mark.parametrize("request_body,caller,code", [
    (body(), CLI, "PRINCIPAL_FORBIDDEN"),
    (body(judgment_id="j9"), RESEARCH, "JUDGMENT_UNKNOWN"),
    (body(verdict="SHADOW"), RESEARCH, "SHADOW_JUDGMENT_MISMATCH"),
    (body(case_digest="sha256:" + "d" * 64), RESEARCH, "SHADOW_JUDGMENT_MISMATCH"),
    (body(session_date="2024-03-28"), RESEARCH, "SHADOW_SESSION_OUTSIDE_WINDOW"),
    (body(session_date="2024-04-30"), RESEARCH, "SHADOW_SESSION_OUTSIDE_WINDOW"),
    (body(session_date="2024-04-06"), RESEARCH, "SHADOW_SESSION_OUTSIDE_WINDOW"),      # a Saturday
    (body(session_date="2024-04-02"), RESEARCH, "SHADOW_SESSION_NOT_CLOSED"),          # the clock is 04-01 22:00Z
    (body(bar_size="1 min"), RESEARCH, "SHADOW_BAR_SIZE_MISMATCH"),
])
def test_refusals(ingest, store, request_body, caller, code):
    assert ingest.record(request_body, caller)["code"] == code
    assert store.fetch("shadow_results", {}) == []


def test_an_unknown_judgment_or_an_open_session_may_be_retried_a_mismatch_may_not(ingest):
    assert ingest.record(body(judgment_id="j9"), RESEARCH)["retryable"] is True
    assert ingest.record(body(session_date="2024-04-02"), RESEARCH)["retryable"] is True
    assert ingest.record(body(verdict="SHADOW"), RESEARCH)["retryable"] is False


def test_a_tampered_judgment_fails_loudly_and_writes_nothing(ingest, store):
    def tampered(judgment_id):
        raise JudgmentRefused("JUDGMENT_TAMPERED", judgment_id)
    ingest._judgments = SimpleNamespace(get=tampered)
    with pytest.raises(JudgmentRefused) as refused:
        ingest.record(body(), RESEARCH)
    assert refused.value.code == "JUDGMENT_TAMPERED"
    assert store.fetch("shadow_results", {}) == []


def test_rows_are_strict():
    with pytest.raises(ValidationError):
        body(pnl_usd=None)                                         # COMPLETE needs P&L
    with pytest.raises(ValidationError):
        body(status="INCOMPLETE", reason=None, pnl_usd=None, fees_usd=None, trades=None, end_equity_usd=None)
    with pytest.raises(ValidationError):
        body(trades=True)
    with pytest.raises(ValidationError):
        body(pnl_usd=float("nan"))


def test_an_incomplete_row_is_stored_with_its_reason(ingest, store):
    incomplete = body(status="INCOMPLETE", reason="BARS_MISSING", pnl_usd=None, fees_usd=None, trades=None,
                      end_equity_usd=None)
    assert ingest.record(incomplete, RESEARCH)["status"] == "INSERTED"
    [row] = store.fetch("shadow_results", {})
    assert (row["status"], row["reason"], row["pnl_usd"]) == ("INCOMPLETE", "BARS_MISSING", None)


def test_a_stored_row_that_was_edited_reads_as_incomplete_and_fails_the_seal_check(ingest, store, db):
    ingest.record(body(), RESEARCH)
    db.execute("UPDATE shadow_results SET pnl_usd = 99.0")
    assert [m["check"] for m in store.verify_seals()] == ["ROW_EDITED"]
    [row], tampered = verified_shadow_rows(store)
    assert (row["status"], row["reason"], row["pnl_usd"]) == ("INCOMPLETE", "ROW_TAMPERED", None)
    assert [t["record_id"] for t in tampered] == [row["record_id"]]
    [book] = build_shadow_books([row])
    assert book["status"] == "INCOMPLETE" and book["pnl_usd"] is None


def test_an_untouched_row_reads_back_unchanged(ingest, store):
    ingest.record(body(), RESEARCH)
    rows = store.fetch("shadow_results", {})
    checked, tampered = verified_shadow_rows(store)
    assert checked == rows and tampered == []


@pytest.mark.parametrize("column,value", [("deployment_version", "'sha256:" + "e" * 64 + "'"),
                                          ("recorded_at", "TIMESTAMPTZ '2030-01-01 00:00:00+00'")])
def test_an_edit_outside_the_body_also_reads_as_tampered(ingest, store, db, column, value):
    ingest.record(body(), RESEARCH)
    db.execute(f"UPDATE shadow_results SET {column} = {value}")
    [row], tampered = verified_shadow_rows(store)
    assert row["reason"] == "ROW_TAMPERED" and len(tampered) == 1


def test_one_book_per_verdict_never_summed_and_incomplete_shown():
    rows = [{"verdict": "DEPLOY", "judgment_id": "a", "status": "COMPLETE", "pnl_usd": 10.0, "fees_usd": 1.0,
             "trades": 2, "reason": None},
            {"verdict": "REJECT", "judgment_id": "b", "status": "COMPLETE", "pnl_usd": -5.0, "fees_usd": 1.0,
             "trades": 1, "reason": None},
            {"verdict": "REJECT", "judgment_id": "b", "status": "INCOMPLETE", "pnl_usd": None, "fees_usd": None,
             "trades": None, "reason": "BARS_MISSING"}]
    books = {b["verdict"]: b for b in build_shadow_books(rows)}
    assert set(books) == {"DEPLOY", "REJECT"}
    assert books["DEPLOY"]["pnl_usd"] == 10.0 and books["DEPLOY"]["status"] == "COMPLETE"
    assert books["REJECT"]["status"] == "INCOMPLETE" and books["REJECT"]["pnl_usd"] is None
    only_incomplete = build_shadow_books([rows[2]])[0]
    assert (only_incomplete["pnl_usd"], only_incomplete["known_pnl_usd"], only_incomplete["fees_usd"],
            only_incomplete["trades"]) == (None, None, None, None)
    assert books["REJECT"]["known_pnl_usd"] == -5.0 and books["REJECT"]["incomplete_reasons"] == {"BARS_MISSING": 1}
    assert all(book["label"] == "SHADOW" for book in books.values())
    assert build_report(empty_inputs(shadow_rows=rows))["shadow_books"] == build_shadow_books(rows)


def test_shadow_rows_never_move_the_real_account_or_the_baseline_books():
    rows = [{"verdict": "DEPLOY", "judgment_id": "a", "status": "COMPLETE", "pnl_usd": 10.0, "fees_usd": 1.0,
             "trades": 2, "reason": None}]
    without, with_rows = build_report(empty_inputs()), build_report(empty_inputs(shadow_rows=rows))
    assert without["shadow_books"] == [] and with_rows["label"] == "PAPER"
    assert {k: v for k, v in with_rows.items() if k != "shadow_books"} == {
        k: v for k, v in without.items() if k != "shadow_books"}


def test_the_service_report_carries_the_verified_shadow_books(ingest, scoreboard):
    ingest._store = scoreboard.store
    ingest._now = lambda: dt.datetime(2024, 4, 3, 22, tzinfo=dt.timezone.utc)
    ingest.record(body(), RESEARCH)
    ingest.record(body(session_date="2024-04-02", pnl_usd=7.5, end_equity_usd=100_020.0), RESEARCH)
    report = scoreboard.report()
    assert report["shadow_books"][0]["verdict"] == "DEPLOY" and report["shadow_books"][0]["pnl_usd"] == 20.0
    assert report["label"] == "PAPER" and report["warnings"] == []
    scoreboard.db.execute("UPDATE shadow_results SET pnl_usd = 99.0 WHERE session_date = '2024-04-02'")
    report = scoreboard.report()
    assert report["shadow_books"][0]["status"] == "INCOMPLETE" and report["shadow_books"][0]["pnl_usd"] is None
    assert [w["code"] for w in report["warnings"]] == ["SHADOW_ROW_TAMPERED"]
    assert scoreboard.verify()["ok"] is False


# -- the typed RPC surface ---------------------------------------------------------------------------------


def serve(ingest, acl=TRADER_ACL) -> ServedStack:
    registry = TypedRpcRegistry(acl=acl, default_execution="thread")
    register_shadow_surface(registry, ingest)
    return ServedStack({("trader", "command"): registry}, make_identities())


@pytest.fixture
def served(ingest):
    stack = serve(ingest)
    yield stack
    stack.close()


def test_research_records_and_repeats_over_the_signed_surface(served, store):
    client = served.client("research", role="command")
    assert client.call("record_shadow_result", body().model_dump(), dict)["status"] == "INSERTED"
    assert client.call("record_shadow_result", body().model_dump(), dict)["status"] == "DUPLICATE"
    reply = client.call("record_shadow_result", body(pnl_usd=1.0).model_dump(), dict)
    assert (reply["status"], reply["code"]) == ("REFUSED", "CONFLICTING_DUPLICATE")
    assert len(store.fetch("shadow_results", {})) == 1


def test_only_research_may_call_it_and_the_handler_checks_again_behind_an_open_allow_list(ingest):
    assert TRADER_ACL[("command", "record_shadow_result")] == {"research"}
    for acl in (TRADER_ACL, ALLOW_ALL):
        stack = serve(ingest, acl=acl)
        try:
            for principal in sorted(SERVER_ACCEPTS["trader"] - {"research"}):
                with pytest.raises(TypedRpcRemoteError) as exc:
                    stack.client(principal, role="command").call("record_shadow_result", body().model_dump(), dict)
                assert exc.value.code == "PERMISSION_DENIED", principal
        finally:
            stack.close()


def test_a_tampered_judgment_or_version_is_an_rpc_error_not_a_reply_body(ingest, store):
    def tampered_judgment(judgment_id):
        raise JudgmentRefused("JUDGMENT_TAMPERED", judgment_id)

    def tampered_version(judgment_id):
        raise DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", judgment_id)
    judgments, versions = ingest._judgments, ingest._versions
    for broken, code in ((dict(_judgments=SimpleNamespace(get=tampered_judgment)), "JUDGMENT_TAMPERED"),
                         (dict(_versions=SimpleNamespace(version_for_judgment=tampered_version)),
                          "DEPLOYMENT_VERSION_TAMPERED")):
        ingest._judgments, ingest._versions = judgments, versions
        for attribute, value in broken.items():
            setattr(ingest, attribute, value)
        stack = serve(ingest)
        try:
            with pytest.raises(TypedRpcRemoteError) as exc:
                stack.client("research", role="command").call("record_shadow_result", body().model_dump(), dict)
            assert exc.value.code == code
        finally:
            stack.close()
    assert store.fetch("shadow_results", {}) == []


def test_a_malformed_body_is_a_validation_error(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("research", role="command").call("record_shadow_result", body().model_dump() | {"trades": True},
                                                       dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_nothing_is_registered_without_a_shadow_ingest():
    registry = TypedRpcRegistry(acl=TRADER_ACL)
    register_shadow_surface(registry, None)
    assert list(registry.registrations()) == []


def test_the_full_production_registry_registers_the_command():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert ("command", "record_shadow_result") in registered
