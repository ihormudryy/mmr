"""Plan 4 rulings 6, 10 and 11: an authored end reaches the trader, then the claim closes FAILED without a case."""
import logging

import pytest

from tests.authored.fake_sandbox import FakeSandbox
from tests.research.authored_fixtures import (CATALOGUE_KEY, NOW, POINT, install_fake_sandbox,
                                             research_world, time_of_day_key)
from tests.research.evaluation_fixtures import CONIDS
from tests.research.service_fakes import AI, FakeTrader
from trader.authored.keys import parse_authored_key
from trader.research.authored_evaluation import AuthoredEvaluator
from trader.research.authored_outcome import AUTHORED_END_CODE, AuthoredEnd, AuthoredEvaluationEnded
from trader.research.evaluation_service import EvaluationService, ServiceStateError
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner
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
                             warmup_sessions=5, order_notional=1900.0, queue_max=2, now=lambda: NOW)


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
