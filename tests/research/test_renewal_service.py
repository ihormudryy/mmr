"""SP2c Plan 5 Task 6: submit_evaluation kind RENEWAL: no claim, no queue, no trial; refused before any case."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.research.evaluation_fixtures import TIME_OF_DAY_STRATEGY
from tests.research.renewal_fixtures import KEY, V1, forward_view
from tests.research.service_fakes import AI, CLI, FakeTrader, judgment_view
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.evaluation_case import load_verified_case
from trader.research.evaluation_service import EvaluationService
from trader.research.judgment_attest import JudgmentAttest
from trader.research.renewal_case import renewal_request_id
from trader.research.renewal_service import RenewalRequests
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.shadow_replay import ShadowReplay
from trader.research.signing import AttestationSigner
from trader.research.trader_port import TraderUnavailable

NOW = dt.datetime(2024, 4, 4, 21, 0, tzinfo=dt.timezone.utc)
RENEWAL = {"kind": "RENEWAL", "prior_version_digest": V1}


@pytest.fixture
def world(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    store, trader, signer = ResearchStore(db), FakeTrader(), AttestationSigner.generate()
    clock = {"now": NOW}
    trader.forward[V1] = forward_view()

    def service(*, allowlist=(KEY,)):
        renewals = RenewalRequests(store=store, trader=trader, signer=signer, artifacts_root=tmp_path / "artifacts",
                                   repo_root=tmp_path, judge=BacktestJudgeConfig(strategy_allowlist=allowlist),
                                   warmup_sessions=5, incomplete_after_hours=16, now=lambda: clock["now"])
        return EvaluationService(store=store, trader=trader, build_spec=_never, evaluate=_never, signer=signer,
                                 artifacts_root=tmp_path / "artifacts", warmup_sessions=5, order_notional=1900.0,
                                 queue_max=2, now=lambda: clock["now"], renewals=renewals)
    return SimpleNamespace(service=service, trader=trader, store=store, signer=signer, root=tmp_path, clock=clock,
                           db=db)


def _never(*args):
    raise AssertionError("a renewal builds no cohort spec and runs no evaluation")


def _no_case_files(world) -> bool:
    cases = world.root / "artifacts" / "cases"
    return not cases.exists() or not any(cases.iterdir())


def test_a_renewal_case_is_built_once_from_the_traders_forward_evidence(world):
    service = world.service()
    reply = service.submit(RENEWAL, AI)
    assert (reply["status"], reply["request_id"], reply["state"]) == ("ACCEPTED", renewal_request_id(V1), "DONE")
    view = service.get(reply["request_id"], CLI)
    assert view["found"] and view["state"] == "DONE" and view["summary"]["kind"] == "RENEWAL"
    case = load_verified_case(world.root / "artifacts" / "cases", view["case_digest"],
                              {world.signer.public_key_id: world.signer.public_key})
    assert (case.stage, case.renewal.prior_deployment_version) == ("FORWARD_COMPLETE", V1)
    assert world.store.case(case_digest=view["case_digest"])["request_id"] == reply["request_id"]
    again = service.submit(RENEWAL, AI)
    assert (again["status"], again["request_id"]) == ("DUPLICATE", reply["request_id"])
    assert world.trader.claims == {} and world.trader.updates == [] and world.trader.forward_reads == [V1]


@pytest.mark.parametrize("change,code,retryable", [
    ({"renewable": (False, "BUNDLE_EXPIRED")}, "BUNDLE_EXPIRED", False),
    ({"renewable": (False, "RENEWAL_NOT_DUE")}, "RENEWAL_NOT_DUE", False),
    ({"renewable": (False, "FAMILY_COOLING_DOWN")}, "FAMILY_COOLING_DOWN", False),
    ({"file_hash": "sha256:" + "c" * 64}, "STRATEGY_SOURCE_CHANGED", False),
    ({"states": ("COMPLETE", "COMPLETE", "MISSING")}, "FORWARD_EVIDENCE_PENDING", True),
])
def test_a_renewal_that_cannot_be_renewed_is_refused_before_any_case(world, change, code, retryable):  # focus 3
    world.trader.forward[V1] = forward_view(**change)
    if code == "FORWARD_EVIDENCE_PENDING":
        world.clock["now"] = dt.datetime(2024, 4, 4, 12, 0, tzinfo=dt.timezone.utc)
    reply = world.service().submit(RENEWAL, AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", code, retryable)
    assert world.store.get(renewal_request_id(V1)) is None                     # a later resend asks again
    assert _no_case_files(world)


def test_a_missing_session_past_its_deadline_is_judged_as_incomplete(world):
    world.trader.forward[V1] = forward_view(states=("COMPLETE", "COMPLETE", "MISSING"))
    service = world.service()
    reply = service.submit(RENEWAL, AI)
    assert (reply["status"], reply["state"]) == ("ACCEPTED", "DONE")
    assert service.get(reply["request_id"], CLI)["summary"]["stage"] == "FORWARD_INCOMPLETE"


def test_unknown_versions_off_allowlist_keys_and_an_unreachable_trader_are_refused(world):
    other = {"kind": "RENEWAL", "prior_version_digest": "sha256:" + "2" * 64}
    assert world.service().submit(other, AI)["code"] == "DEPLOYMENT_VERSION_UNKNOWN"
    assert world.service(allowlist=()).submit(RENEWAL, AI)["code"] == "STRATEGY_NOT_ALLOWED"

    def down(version):
        raise TraderUnavailable("no route")
    world.trader.forward_evidence = down
    reply = world.service().submit(RENEWAL, AI)
    assert (reply["code"], reply["retryable"]) == ("TRADER_UNAVAILABLE", True)


def test_only_ai_research_submits_a_renewal(world):
    assert world.service().submit(RENEWAL, CLI)["code"] == "PRINCIPAL_FORBIDDEN"


def test_the_strategy_file_is_read_by_its_key_not_by_the_stored_path(world):
    evidence = forward_view()
    evidence["binding"]["strategy_path"] = "/app/strategies/time_of_day.py"   # sweeps store absolute paths
    world.trader.forward[V1] = evidence
    assert world.service().submit(RENEWAL, AI)["status"] == "ACCEPTED"


@pytest.mark.parametrize("code", ["FORWARD_EVIDENCE_TAMPERED", "DEPLOYMENT_VERSION_TAMPERED"])
def test_tampered_forward_evidence_ends_the_request_visibly_and_is_never_asked_again(world, code, caplog):
    def tampered(version):
        world.trader.forward_reads.append(version)
        raise TypedRpcRemoteError(code, "shadow row 2024-04-02 fails its seal")
    world.trader.forward_evidence = tampered
    service = world.service()
    reply = service.submit(RENEWAL, AI)
    assert (reply["status"], reply["request_id"], reply["state"], reply["code"], reply["retryable"]) == (
        "ACCEPTED", renewal_request_id(V1), "FAILED", code, False)       # not a business refusal
    assert any(record.levelname == "ERROR" and code in record.getMessage() for record in caplog.records)
    row = world.store.get(renewal_request_id(V1))
    assert row["state"] == "PARKED" and row["parked_reason"].startswith(code)
    view = service.get(reply["request_id"], AI)
    assert (view["found"], view["state"], view["case_digest"], view["summary"]) == (True, "FAILED", None, None)
    again = service.submit(RENEWAL, AI)
    assert (again["status"], again["request_id"], again["state"]) == ("DUPLICATE", reply["request_id"], "FAILED")
    assert world.trader.forward_reads == [V1]                                  # no silent retry loop
    assert _no_case_files(world)


@pytest.mark.parametrize("code", ["VALIDATION_ERROR", "DEPLOYMENT_CALENDAR_UNAVAILABLE", "CASE_SIGNATURE_INVALID",
                                  "INTERNAL_ERROR"])
def test_any_other_remote_error_from_the_trader_is_a_retryable_refusal_that_parks_nothing(world, code, caplog):
    def broken(version):
        world.trader.forward_reads.append(version)
        raise TypedRpcRemoteError(code, "not a tampered record")
    world.trader.forward_evidence = broken
    service = world.service()
    reply = service.submit(RENEWAL, AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "TRADER_ERROR", True)
    assert code in reply["detail"]
    assert any(record.levelname == "ERROR" and code in record.getMessage() for record in caplog.records)
    assert world.store.get(renewal_request_id(V1)) is None and _no_case_files(world)
    del world.trader.forward_evidence                                        # the trader answers again
    assert service.submit(RENEWAL, AI)["status"] == "ACCEPTED"             # a resend asks the trader again
    assert world.trader.forward_reads == [V1, V1]


def test_a_refusal_without_a_code_gets_a_named_code(world):
    world.trader.forward_evidence = lambda version: {"status": "REFUSED", "code": None, "detail": "no reason",
                                                     "evidence": None}
    reply = world.service().submit(RENEWAL, AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "FORWARD_EVIDENCE_REFUSED", False)


def test_a_resend_after_new_york_midnight_is_the_same_request(world):        # pins: the id names the version only
    service = world.service()
    first = service.submit(RENEWAL, AI)
    world.clock["now"] += dt.timedelta(days=1)
    again = service.submit(RENEWAL, AI)
    assert (first["status"], again["status"], again["request_id"]) == ("ACCEPTED", "DUPLICATE", first["request_id"])
    assert world.trader.forward_reads == [V1]


def test_recover_skips_renewal_rows_which_have_no_claim(world, monkeypatch):
    service = world.service()
    service.submit(RENEWAL, AI)

    def readback(request_id):
        assert request_id != renewal_request_id(V1), "a renewal has no claim to read back"
        return None
    monkeypatch.setattr(world.trader, "claim_readback", readback)
    world.service().recover()


def test_attest_refuses_a_renewal_judgment(world):
    world.trader.judgments["jdg-renewal-1"] = judgment_view("jdg-renewal-1", "sha256:" + "9" * 64, "DEPLOY",
                                                            kind="RENEWAL")
    attest = JudgmentAttest(research_db=world.db, store=world.store, trader=world.trader, signer=world.signer,
                            artifacts_root=world.root / "artifacts", repo_root=world.root, is_paper=lambda: True,
                            now=lambda: NOW)
    reply = attest.attest({"judgment_id": "jdg-renewal-1"}, AI)
    assert (reply["status"], reply["code"]) == ("REFUSED", "JUDGMENT_NOT_DEPLOY")


def test_a_judged_renewal_case_joins_the_shadow_cohort(world):
    reply = world.service().submit(RENEWAL, AI)
    digest = world.store.get(reply["request_id"])["case_digest"]
    world.trader.judgments["jdg-renewal-1"] = judgment_view("jdg-renewal-1", digest, "DEPLOY", kind="RENEWAL",
                                                            decided_at="2024-04-04T21:30:00+00:00",
                                                            binding={"bar_size": "15 mins"})
    judge = SimpleNamespace(deploy_expiry_sessions=3, family_cooldown_sessions=10, shadow_warmup_sessions=5)
    replay = ShadowReplay(store=world.store, trader=world.trader, signer=world.signer,
                          artifacts_root=world.root / "artifacts", paths=None, registry=None,
                          config=ResearchServiceConfig(), judge=judge, now=lambda: NOW)
    replay.tick()                                                   # no session is due yet: it only joins
    (member,) = world.store.shadow_members()
    assert (member["judgment_id"], member["verdict"], str(member["first_session"])) == (
        "jdg-renewal-1", "DEPLOY", "2024-04-05")
