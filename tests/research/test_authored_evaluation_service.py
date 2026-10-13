"""Plan 4 rulings 6, 10 and 11: an authored end reaches the trader, then the claim closes FAILED without a case."""
import datetime as dt
import logging

import pytest

from tests.authored.fake_sandbox import FakeSandbox, fault
from tests.research.authored_fixtures import (CATALOGUE_KEY, NOW, POINT, install_fake_sandbox,
                                             research_world, time_of_day_key)
from tests.research.evaluation_fixtures import CONIDS
from tests.research.service_fakes import AI, FakeTrader
from trader.authored.keys import parse_authored_key
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.authored_evaluation import AuthoredEvaluator
from trader.research.authored_outcome import AUTHORED_END_CODE, AuthoredEnd, AuthoredEvaluationEnded
from trader.research.evaluation_service import (AUTHORED_OUTCOME_UNKNOWN, EvaluationService, ServiceStateError,
                                               holdout_opened_in)
from trader.research.experiment_registry import ExperimentFamily
from trader.research.service_store import CLAIM_CLOSE_OWED, CLAIM_CLOSE_REFUSED, CLAIM_CLOSED, ResearchStore
from trader.research.signing import AttestationSigner
from trader.research.strategy_key import split_evaluation_key
from trader.research.trader_port import TraderUnavailable

SIGNER = AttestationSigner.generate()
KEY = time_of_day_key(FakeSandbox())
DIGEST = parse_authored_key(KEY)[0]
UNAVAILABLE_END = AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")
ERROR_END = AuthoredEnd("SANDBOX_ERROR", "SANDBOX_ERROR")


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    return research_world(tmp_path, tmp_duckdb_path)


def raising(error):
    def evaluate(spec):
        raise error
    return evaluate


def never(spec):
    raise AssertionError("an authored end was evaluated again")


def service(world, trader, evaluate, store=None):
    return EvaluationService(store=store or ResearchStore(world.db), trader=trader, build_spec=world.build_body,
                             evaluate=evaluate, signer=SIGNER, artifacts_root=world.root / "artifacts",
                             warmup_sessions=5, order_notional=1900.0, queue_max=2, now=lambda: NOW,
                             holdout_opened=holdout_opened_in(world.registry))


def submit(svc):
    reply = svc.submit({"kind": "INITIAL", "strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}],
                        "conids": CONIDS, "bar_size": "15 mins"}, AI)
    assert reply["status"] == "ACCEPTED", reply
    return reply["request_id"]


def no_case_signed(world):
    cases = world.root / "artifacts" / "cases"
    return not cases.exists() or not any(cases.iterdir())


def test_an_authored_end_reaches_the_trader_then_the_claim_fails_without_a_case(world):
    trader = FakeTrader()
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    assert svc.run_next()
    assert trader.authored_reports == [ERROR_END.report_body(request_id, DIGEST)]
    assert trader.claims[request_id]["state"] == "FAILED" and not trader.reported_after_close
    assert svc.get(request_id, AI) == {"found": True, "request_id": request_id, "state": "FAILED",
                                       "case_digest": None, "summary": None}
    assert no_case_signed(world)


def test_a_lost_report_holds_the_claim_open_until_the_next_tick(world):
    trader = FakeTrader()
    trader.lose_authored_report = True
    svc = service(world, trader, raising(AuthoredEvaluationEnded(UNAVAILABLE_END)))
    request_id = submit(svc)
    svc.run_next()
    assert trader.authored_reports == [] and trader.claims[request_id]["state"] != "FAILED"
    assert svc.get(request_id, AI)["state"] == "RUNNING"
    svc.run_next()
    assert [r["event"] for r in trader.authored_reports] == ["SANDBOX_UNAVAILABLE"]
    assert trader.claims[request_id]["state"] == "FAILED" and not trader.reported_after_close


def test_an_end_decided_before_a_crash_is_never_evaluated_again(world):
    trader, store = FakeTrader(), ResearchStore(world.db)
    request_id = submit(service(world, trader, never, store=store))
    store.set_state(request_id, "RUNNING", now=NOW, code=AUTHORED_END_CODE, detail=UNAVAILABLE_END.marker())
    restarted = service(world, trader, never, store=store)
    restarted.recover()
    restarted.run_next()
    assert [r["event"] for r in trader.authored_reports] == ["SANDBOX_UNAVAILABLE"]
    assert trader.claims[request_id]["state"] == "FAILED"


def test_a_final_refusal_parks_the_claim_visibly_and_reconciles_later(world, caplog, monkeypatch):  # review r1 #7
    trader = FakeTrader()
    trader.refuse_authored_report = ("AUTHORED_STATE_CONFLICT", False)
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    with caplog.at_level(logging.ERROR):
        svc.run_next()
        svc.run_next()                                        # parked: not resent on every tick
    assert trader.claims[request_id]["state"] != "FAILED"     # the claim is not closed on a refusal
    assert svc.get(request_id, AI)["state"] == "RUNNING"      # visible: the request stays open, no case
    assert caplog.text.count("AUTHORED_REPORT_REFUSED") == 1 and "AUTHORED_STATE_CONFLICT" in caplog.text
    assert no_case_signed(world)
    monkeypatch.setattr("trader.research.evaluation_service.PARKED_RESEND_SECONDS", 0)
    trader.refuse_authored_report = None                      # the operator repaired the trader record
    svc.run_next()
    assert [r["event"] for r in trader.authored_reports] == ["SANDBOX_ERROR"]
    assert trader.claims[request_id]["state"] == "FAILED"


def test_a_retryable_refusal_is_sent_again(world):
    trader = FakeTrader()
    trader.refuse_authored_report = ("TRADER_BUSY", True)
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    svc.run_next()
    assert trader.claims[request_id]["state"] != "FAILED"
    trader.refuse_authored_report = None
    svc.run_next()
    assert trader.claims[request_id]["state"] == "FAILED" and len(trader.authored_reports) == 1


def test_a_refused_start_parks_the_request_without_a_case(world):
    svc = service(world, FakeTrader(), raising(ServiceStateError("the trader refused STARTED")))
    request_id = submit(svc)
    svc.run_next()
    view = svc.get(request_id, AI)
    assert (view["state"], view["case_digest"]) == ("FAILED", None) and no_case_signed(world)


def test_an_unreachable_trader_while_starting_ends_the_worker_and_signs_nothing(world):
    svc = service(world, FakeTrader(), raising(TraderUnavailable("report_authored_evaluation: no route")))
    submit(svc)
    with pytest.raises(TraderUnavailable):
        svc.run_next()
    assert no_case_signed(world)


def test_a_refused_start_closes_the_trader_claim_failed(world, monkeypatch):    # Plan 4 F5
    sandbox = install_fake_sandbox(monkeypatch)
    trader = FakeTrader()
    trader.refuse_authored_report = ("AUTHORED_STATE_CONFLICT", False)
    evaluator = AuthoredEvaluator(research_db=world.db, paths=world.paths, now=lambda: NOW, trader=trader)
    svc = service(world, trader, evaluator.evaluate)
    assert time_of_day_key(sandbox) == KEY
    request_id = submit(svc)
    svc.run_next()
    assert trader.claims[request_id]["state"] == "FAILED"
    assert svc.get(request_id, AI)["state"] == "FAILED" and no_case_signed(world) and sandbox.sessions == []


def test_a_parked_claim_close_that_fails_is_tried_again_next_tick(world):
    trader = FakeTrader()
    trader.lose_update = {"FAILED": "request"}
    svc = service(world, trader, raising(ServiceStateError("the trader refused STARTED")))
    request_id = submit(svc)
    svc.run_next()
    assert trader.claims[request_id]["state"] != "FAILED"
    assert svc.get(request_id, AI)["state"] == "FAILED"         # parked: never judged, whatever the claim says
    svc.run_next()
    assert trader.claims[request_id]["state"] == "FAILED"
    svc.run_next()
    assert [u for u in trader.updates if u == (request_id, "FAILED")] == [(request_id, "FAILED")] * 2


def test_a_parked_catalogue_request_keeps_its_claim(world):
    trader = FakeTrader()
    svc = service(world, trader, raising(ServiceStateError("a catalogue mismatch")))
    reply = svc.submit({"kind": "INITIAL", "strategy_key": CATALOGUE_KEY, "cohort": [POINT], "conids": CONIDS,
                        "bar_size": "15 mins"}, AI)
    svc.run_next()
    assert trader.claims[reply["request_id"]]["state"] == "RUNNING"


def remote_error(code):
    def report(body):
        raise TypedRpcRemoteError(code, "raised by the test")
    return report


@pytest.mark.parametrize("code", ["AUTHORED_STORE_UNAVAILABLE", "PERMISSION_DENIED", "AUTHENTICATION_ERROR"])
def test_a_remote_error_on_the_end_report_stops_the_service_and_the_restart_resends(world, code):   # M1, M4
    trader, store = FakeTrader(), ResearchStore(world.db)
    real_report = trader.report_authored_evaluation
    trader.report_authored_evaluation = remote_error(code)
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)), store=store)
    request_id = submit(svc)
    with pytest.raises(TypedRpcRemoteError):
        svc.run_next()
    assert svc.get(request_id, AI)["state"] == "RUNNING" and store.get(request_id)["state"] != "PARKED"
    assert trader.claims[request_id]["state"] != "FAILED" and no_case_signed(world)
    trader.report_authored_evaluation = real_report
    service(world, trader, never, store=store).recover()
    assert trader.authored_reports == [ERROR_END.report_body(request_id, DIGEST)]
    assert trader.claims[request_id]["state"] == "FAILED" and not trader.reported_after_close


def test_a_loud_remote_error_on_the_end_report_parks_the_end(world, monkeypatch):
    """Hypothetical code: the authored surface answers only AUTHORED_STORE_UNAVAILABLE today."""
    trader = FakeTrader()
    real_report = trader.report_authored_evaluation
    trader.report_authored_evaluation = remote_error("AUTHORED_RECORD_TAMPERED")
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    svc.run_next()
    assert svc.get(request_id, AI)["state"] == "RUNNING" and trader.claims[request_id]["state"] != "FAILED"
    monkeypatch.setattr("trader.research.evaluation_service.PARKED_RESEND_SECONDS", 0)
    trader.report_authored_evaluation = real_report
    svc.run_next()
    assert trader.claims[request_id]["state"] == "FAILED" and not trader.reported_after_close


def test_a_refused_claim_close_is_not_tried_again_and_logs_one_error(world, caplog):         # M2
    trader, store = FakeTrader(), ResearchStore(world.db)

    def refusing_update(request_id, state):
        trader.updates.append((request_id, state))
        if state == "RUNNING":
            return {"status": "UPDATED", "code": None, "detail": None, "retryable": False}
        return {"status": "REFUSED", "code": "CLAIM_UNKNOWN", "detail": "refused by the test", "retryable": False}
    trader.update_claim = refusing_update
    svc = service(world, trader, raising(ServiceStateError("the trader refused STARTED")), store=store)
    request_id = submit(svc)
    with caplog.at_level(logging.ERROR):
        svc.run_next()
        svc.run_next()
    assert store.get(request_id)["code"] == CLAIM_CLOSE_REFUSED
    assert trader.updates.count((request_id, "FAILED")) == 1
    assert len([r for r in caplog.records if "refused to close" in r.getMessage()]) == 1


def test_a_restart_closes_an_owed_claim(world):
    trader, store = FakeTrader(), ResearchStore(world.db)
    trader.lose_update = {"FAILED": "request"}
    svc = service(world, trader, raising(ServiceStateError("refused STARTED")), store=store)
    request_id = submit(svc)
    svc.run_next()
    assert store.get(request_id)["code"] == CLAIM_CLOSE_OWED
    service(world, trader, never, store=store).recover()
    assert trader.claims[request_id]["state"] == "FAILED" and store.get(request_id)["code"] == CLAIM_CLOSED


def test_an_end_applied_with_its_reply_lost_closes_the_claim_once(world):
    trader = FakeTrader()
    trader.lose_authored_reply = True
    svc = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)))
    request_id = submit(svc)
    svc.run_next()
    assert trader.claims[request_id]["state"] != "FAILED" and svc.get(request_id, AI)["state"] == "RUNNING"
    svc.run_next()
    assert len(trader.authored_reports) == 1 and trader.claims[request_id]["state"] == "FAILED"
    assert trader.updates.count((request_id, "FAILED")) == 1 and not trader.reported_after_close


class Crash(BaseException):
    """The research process dies here."""


def test_a_crash_before_the_fault_is_recorded_after_the_holdout_opened_signs_no_case(world, monkeypatch):  # PR #161
    from tests.research.test_authored_evaluation import opens_a_holdout_then
    assert time_of_day_key(install_fake_sandbox(monkeypatch)) == KEY        # the stored file
    trader, store = FakeTrader(), ResearchStore(world.db)
    evaluator = AuthoredEvaluator(research_db=world.db, paths=world.paths, now=lambda: NOW, trader=trader,
                                  run_cohort=opens_a_holdout_then(fault("SANDBOX_ERROR")))
    first = service(world, trader, evaluator.evaluate, store=store)
    request_id = submit(first)
    real_set_state = store.set_state

    def crash_at_the_end_marker(request_id, state, **fields):
        if fields.get("code") == AUTHORED_END_CODE:
            raise Crash()
        real_set_state(request_id, state, **fields)
    store.set_state = crash_at_the_end_marker
    with pytest.raises(Crash):
        first.run_next()
    del store.set_state
    restarted = service(world, trader, never, store=store)
    restarted.recover()
    restarted.run_next()
    assert no_case_signed(world)
    assert store.get(request_id)["state"] == "PARKED" and AUTHORED_OUTCOME_UNKNOWN in store.get(request_id)["parked_reason"]
    assert trader.claims[request_id]["state"] == "FAILED"
    assert [report["event"] for report in trader.authored_reports] == ["STARTED"]


def test_a_crash_before_the_holdout_opened_runs_the_evaluation_again(world):
    trader, store = FakeTrader(), ResearchStore(world.db)
    request_id = submit(service(world, trader, never, store=store))
    store.set_state(request_id, "RUNNING", now=NOW)                  # crashed before any holdout opened
    restarted = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)), store=store)
    restarted.recover()
    restarted.run_next()
    assert [report["event"] for report in trader.authored_reports] == ["SANDBOX_ERROR"]


def open_an_earlier_holdout(world, opened_at):
    strategy_path, class_name = split_evaluation_key(KEY)
    family_id = world.registry.create_family(ExperimentFamily(
        strategy_path=strategy_path, class_name=class_name, repository_commit="authored", source_tree_digest="s",
        dependency_lock_digest="d", container_digest="unpinned", dataset_manifest_digest="m", search_space={},
        cost_model={}, validation_protocol={}),
        created_at=opened_at, validation_folds=[{"kind": "holdout", "start": "2024-01-02", "end": "2024-01-09"}])
    trial_id = world.registry.start_trial(family_id, trial_key="t", parameters={}, started_at=opened_at)
    world.registry.finish_trial(trial_id, status="SUCCEEDED", finished_at=opened_at, metrics={"daily_sharpe": 0.1})
    artifact = world.registry.seal_artifact(family_id, selected_trial_id=trial_id, selected_parameters={},
                                            sealed_at=opened_at)
    world.registry.open_holdout(artifact, opened_at=opened_at, passed=False, detail="an earlier request")


def test_a_crash_before_its_own_holdout_runs_again_despite_an_earlier_window(world):  # #165
    open_an_earlier_holdout(world, NOW - dt.timedelta(days=1))
    trader, store = FakeTrader(), ResearchStore(world.db)
    request_id = submit(service(world, trader, never, store=store))
    store.set_state(request_id, "RUNNING", now=NOW)                  # crashed before its own holdout opened
    restarted = service(world, trader, raising(AuthoredEvaluationEnded(ERROR_END)), store=store)
    restarted.recover()
    restarted.run_next()
    assert store.get(request_id)["parked_reason"] is None
    assert [report["event"] for report in trader.authored_reports] == ["SANDBOX_ERROR"]
