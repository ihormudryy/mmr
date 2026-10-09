import datetime as dt
import threading
from types import SimpleNamespace

import pytest

from tests.research.case_fixtures import complete_result, pre_holdout_result
from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_alpaca_extended_hours_bar,
                                               write_costs_config, write_trend_bars, write_universe)
from tests.research.service_fakes import AI, CLI, FakeTrader
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection, DuckDBDataStore
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.artifact import ExperimentFamily
from trader.research.cohort import RequestRefused, build_cohort_spec
from trader.research.evaluation import EvaluationError
from trader.research.evaluation_case import load_verified_case
from trader.research.evaluation_service import EvaluationService
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner
from trader.research.trader_port import TraderPort, TraderUnavailable
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
NOW = dt.datetime(2024, 3, 29, 14, tzinfo=dt.timezone.utc)          # 10:00 New York, 2024-03-29


def request(**overrides):
    return {"kind": "INITIAL", "strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 600}], "conids": CONIDS,
            "bar_size": "15 mins", **overrides}


def fake_evaluate(registry):
    def _evaluate(spec):
        fid = registry.create_family(ExperimentFamily(
            strategy_path="strategies/time_of_day.py", class_name="TimeOfDay", repository_commit="unknown",
            source_tree_digest="s", dependency_lock_digest="d", container_digest="unpinned",
            dataset_manifest_digest="m", search_space={"request": spec.request_id}, cost_model={},
            validation_protocol={}), created_at=NOW)
        for key in ("main", "n1", "n2"):
            tid = registry.start_trial(fid, trial_key=key, parameters={}, started_at=NOW)
            registry.finish_trial(tid, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": 0.1})
        return pre_holdout_result(spec.cohort)
    return _evaluate


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0)
    build_spec_file(tmp_path)
    costs = load_execution_costs_config(str(write_costs_config(tmp_path / "execution_costs.yaml")))
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    registry = ExperimentRegistry(db)
    config = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
    judge = BacktestJudgeConfig(strategy_allowlist=(KEY,), max_cohort_points=3)
    trader, signer = FakeTrader(), AttestationSigner.generate()

    def build_spec(body):
        return build_cohort_spec(body, config=config, judge=judge,
                                 universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                                 costs_config=costs, repo_root=tmp_path, registry=registry,
                                 history_db=tmp_duckdb_path)

    def service(evaluate=None, store=None):
        return EvaluationService(store=store or ResearchStore(db), trader=trader, build_spec=build_spec,
                                 evaluate=evaluate or fake_evaluate(registry), signer=signer,
                                 artifacts_root=tmp_path / "artifacts", warmup_sessions=5, order_notional=1900.0,
                                 queue_max=2, now=lambda: NOW)
    return SimpleNamespace(service=service, trader=trader, registry=registry, signer=signer, root=tmp_path, db=db,
                           history_db=tmp_duckdb_path)


def trials(world):
    return world.registry.strategy_trials("strategies/time_of_day.py", "TimeOfDay")


def case_of(world, view):
    return load_verified_case(world.root / "artifacts" / "cases", view["case_digest"],
                              {world.signer.public_key_id: world.signer.public_key})


def test_a_refused_claim_runs_nothing_and_writes_no_trial(world):
    world.trader.cooling.add(KEY)
    service = world.service()
    reply = service.submit(request(), AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "FAMILY_COOLING_DOWN", False)
    assert service.run_next() is False and trials(world) == []


def test_an_unreachable_trader_is_a_retryable_refusal_that_claims_nothing(world, monkeypatch):
    def down(*args, **kwargs):
        raise TraderUnavailable("no route to the trader")
    monkeypatch.setattr(world.trader, "claim", down)
    monkeypatch.setattr(world.trader, "claim_readback", down)
    reply = world.service().submit(request(), AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "CLAIM_UNKNOWN", True)
    assert world.trader.claims == {}


def test_the_daily_limit_is_the_traders(world):
    world.trader.limit = 1
    service = world.service()
    assert service.submit(request(), AI)["status"] == "ACCEPTED"
    second = service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    assert (second["status"], second["code"]) == ("REFUSED", "EVALUATION_LIMIT_REACHED")


def test_a_lost_claim_reply_is_read_back_and_takes_one_slot(world):
    world.trader.lose_next_reply = True
    service = world.service()
    assert service.submit(request(), AI)["state"] == "QUEUED"
    retry = service.submit(request(), AI)
    assert (retry["status"], retry["state"]) == ("DUPLICATE", "QUEUED")      # same id, no second slot
    assert len(world.trader.claims) == 1


def test_restart_resumes_under_the_same_claim_and_an_outage_wrote_no_trial(world):
    request_id = world.service().submit(request(), AI)["request_id"]
    assert trials(world) == []                                            # outage before any trial
    restarted = world.service()
    restarted.recover()
    assert restarted.run_next() is True
    view = restarted.get(request_id, CLI)
    assert view["found"] and view["state"] == "DONE" and len(trials(world)) == 3
    assert len(world.trader.claims) == 1
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE")]
    case = case_of(world, view)
    assert case.stage == "PRE_HOLDOUT_FAILED" and case.evidence["points"][0]["rules"][0]["observed"] is None
    assert "bundle_digest" not in view and view["summary"]["rules_passed"] is False


def test_holdout_not_available_is_refused_before_any_claim(world, monkeypatch):
    monkeypatch.setattr(world.registry, "opened_holdout_windows", lambda path, cls: [
        {"artifact_id": "a", "family_id": "x", "start": dt.date(2024, 3, 20), "end": dt.date(2024, 3, 27)}])
    reply = world.service().submit(request(), AI)
    assert reply["code"] == "HOLDOUT_NOT_AVAILABLE" and world.trader.claims == {}


def test_missing_bars_are_a_retryable_refusal_before_any_claim(world):
    DuckDBDataStore(world.history_db).delete(str(CONIDS[0]))
    service = world.service()
    reply = service.submit(request(), AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "BARS_MISSING", True)
    assert str(CONIDS[0]) in reply["detail"]
    assert world.trader.claims == {} and service.run_next() is False and trials(world) == []


def test_extended_hours_bars_around_complete_sessions_are_accepted(world):
    write_alpaca_extended_hours_bar(world.history_db, "2024-03-28")
    assert world.service().submit(request(), AI)["status"] == "ACCEPTED"


def test_callers_queue_and_renewal(world):
    service = world.service()
    assert service.submit(request(), CLI)["code"] == "PRINCIPAL_FORBIDDEN"
    renewal = service.submit({"kind": "RENEWAL", "prior_version_digest": "sha256:" + "9" * 64}, AI)
    assert renewal["code"] == "RENEWAL_NOT_SUPPORTED" and world.trader.claims == {}
    service.submit(request(), AI)
    service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    third = service.submit(request(cohort=[{"ENTRY_MINUTE": 630}]), AI)
    assert third["code"] == "QUEUE_FULL" and len(world.trader.claims) == 2


def test_a_failed_run_signs_a_failed_case_and_fails_the_claim(world):
    def boom(spec):
        raise EvaluationError("no 15 mins bars for conids [1001]")
    service = world.service(evaluate=boom)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    view = service.get(request_id, AI)
    assert view["state"] == "FAILED" and world.trader.updates[-1] == (request_id, "FAILED")
    assert case_of(world, view).evidence["error"].startswith("EvaluationError")


def test_a_changed_strategy_file_is_a_failed_case(world):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    path = world.root / "strategies" / "time_of_day.py"
    path.write_text(path.read_text() + "\n# changed\n")
    service.run_next()
    assert case_of(world, service.get(request_id, AI)).evidence["error"].startswith("STRATEGY_SOURCE_CHANGED")


def test_a_lost_terminal_report_withholds_the_case_until_the_next_tick_confirms_it(world):  # PR #91 4218218688
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    world.trader.lose_update["DONE"] = "request"                       # the DONE never reaches the trader
    assert service.run_next() is True
    assert world.trader.claims[request_id]["state"] == "RUNNING"
    held = service.get(request_id, AI)
    assert (held["state"], held["case_digest"], held["summary"]) == ("RUNNING", None, None)
    assert service.run_next() is True                                   # the next tick, both services up
    assert world.trader.claims[request_id]["state"] == "DONE"
    view = service.get(request_id, AI)
    assert view["state"] == "DONE" and view["case_digest"] is not None and view["summary"] is not None
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE"), (request_id, "DONE")]
    assert service.run_next() is False                                  # nothing is owed any more


def test_a_lost_terminal_reply_is_confirmed_by_reading_the_claim_back(world):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    world.trader.lose_update["DONE"] = "reply"                          # applied at the trader, the answer lost
    service.run_next()
    assert service.get(request_id, AI)["state"] == "DONE"
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE")]   # read back, not sent again


# -- controller decisions for Task 6 ------------------------------------------------------


@pytest.mark.parametrize("holdout_passed, stage", [(True, "COMPLETE"), (False, "HOLDOUT_FAILED")])
def test_every_initial_stage_but_failed_closes_the_claim_done(world, holdout_passed, stage):
    service = world.service(evaluate=lambda spec: complete_result(spec.cohort, holdout_passed=holdout_passed))
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    view = service.get(request_id, AI)
    assert case_of(world, view).stage == stage and view["state"] == "DONE"
    assert world.trader.updates[-1] == (request_id, "DONE") and world.trader.claims[request_id]["state"] == "DONE"


def test_a_failed_case_closes_the_claim_failed(world):
    def boom(spec):
        raise EvaluationError("no 15 mins bars for conids [1001]")
    service = world.service(evaluate=boom)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    assert case_of(world, service.get(request_id, AI)).stage == "FAILED"
    assert world.trader.claims[request_id]["state"] == "FAILED"


def test_a_refusal_after_the_claim_is_a_failed_case_and_a_failed_claim(world):
    def holdout_taken(spec):
        raise RequestRefused("HOLDOUT_NOT_AVAILABLE", "another family opened 2024-03-20..2024-03-27")
    service = world.service(evaluate=holdout_taken)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    view = service.get(request_id, AI)
    assert view["state"] == "FAILED" and world.trader.claims[request_id]["state"] == "FAILED"
    assert case_of(world, view).evidence["error"] == (
        "HOLDOUT_NOT_AVAILABLE: another family opened 2024-03-20..2024-03-27")


def test_a_short_safe_error_message_is_kept(world):
    def boom(spec):
        raise EvaluationError("no 15 mins bars for conids [1001]")
    service = world.service(evaluate=boom)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    assert case_of(world, service.get(request_id, AI)).evidence["error"] == (
        "EvaluationError: no 15 mins bars for conids [1001]")


@pytest.mark.parametrize("error, expected", [
    (OSError("cannot open /var/lib/mmr/research.duckdb"), "OSError"),
    (OSError(r"cannot open C:\mmr\research.duckdb"), "OSError"),
    (RuntimeError("Binder Error: SELECT state FROM research_requests WHERE x"), "RuntimeError"),
    (ValueError("bad -----BEGIN PRIVATE KEY----- MC4CAQ"), "ValueError"),
    (RuntimeError("first line\nsecond line with /etc/passwd"), "RuntimeError: first line"),
    (RequestRefused("SPEC_INVALID", "no strategy file /app/strategies/x.py"), "SPEC_INVALID"),
])
def test_a_failed_case_never_carries_a_path_sql_or_key_text(world, error, expected):
    def boom(spec):
        raise error
    service = world.service(evaluate=boom)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    assert case_of(world, service.get(request_id, AI)).evidence["error"] == expected


def test_a_long_error_message_is_cut_short(world):
    def boom(spec):
        raise EvaluationError("too few bars " * 40)
    service = world.service(evaluate=boom)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    assert len(case_of(world, service.get(request_id, AI)).evidence["error"]) <= len("EvaluationError: ") + 120


def test_only_one_evaluation_runs_at_a_time(world):
    started, release = threading.Event(), threading.Event()

    def slow(spec):
        started.set()
        assert release.wait(10), "the test never released the first evaluation"
        return pre_holdout_result(spec.cohort)
    service = world.service(evaluate=slow)
    first = service.submit(request(), AI)["request_id"]
    second = service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)["request_id"]
    worker = threading.Thread(target=service.run_next)
    worker.start()
    try:
        assert started.wait(10)
        assert service.run_next() is False                              # busy: the second tick does nothing
        assert service.get(second, AI)["state"] == "QUEUED"
        assert world.trader.updates == [(first, "RUNNING")]
    finally:
        release.set()
        worker.join(10)
    assert service.run_next() is True
    assert service.get(second, AI)["state"] == "DONE"


def test_a_retried_claim_still_counts_against_the_queue(world):
    service = world.service()
    service.submit(request(), AI)

    def down(*args, **kwargs):
        raise TraderUnavailable("no route to the trader")
    world.trader.claim = down
    world.trader.claim_readback = down
    assert service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)["code"] == "CLAIM_UNKNOWN"
    del world.trader.claim, world.trader.claim_readback
    assert service.submit(request(cohort=[{"ENTRY_MINUTE": 630}]), AI)["status"] == "ACCEPTED"
    retry = service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    assert (retry["code"], retry["retryable"]) == ("QUEUE_FULL", True) and len(world.trader.claims) == 2


def test_a_claim_the_trader_already_holds_is_queued_even_when_the_queue_is_full(world):
    service = world.service()
    world.trader.lose_next_reply = True                                 # the claim lands, its reply is lost

    def down(request_id):
        raise TraderUnavailable("read back lost")
    world.trader.claim_readback = down
    stranded = service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    assert stranded["code"] == "CLAIM_UNKNOWN" and len(world.trader.claims) == 1
    del world.trader.claim_readback
    service.submit(request(), AI)
    service.submit(request(cohort=[{"ENTRY_MINUTE": 630}]), AI)        # the queue is full now
    retry = service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    assert (retry["status"], retry["state"]) == ("ACCEPTED", "QUEUED")  # its slot is spent: it must run
    assert len(world.trader.claims) == 3


# -- the trader port -----------------------------------------------------------------------


class _Client:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def call(self, method, body, model, timeout=None):
        self.calls.append((method, body))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize("error", [ConnectionError("no route"), TimeoutError("late")])
def test_the_port_reports_an_unknown_outcome_as_trader_unavailable(error):
    with pytest.raises(TraderUnavailable):
        TraderPort(_Client(error), _Client(error)).update_claim("sha256:" + "1" * 64, "DONE")


def test_a_busy_trader_is_unavailable_because_no_handler_ran():
    error = TypedRpcRemoteError("SERVER_BUSY", "server command capacity is temporarily exhausted")
    with pytest.raises(TraderUnavailable, match="SERVER_BUSY"):
        TraderPort(_Client(error), _Client(error)).claim("sha256:" + "1" * 64, {})


def test_the_port_lets_a_typed_refusal_fail_loudly():
    error = TypedRpcRemoteError("PERMISSION_DENIED", "no")
    with pytest.raises(TypedRpcRemoteError):
        TraderPort(_Client(error), _Client(error)).claim("sha256:" + "1" * 64, {})


def test_the_port_reads_the_claim_and_asks_for_exactly_one_judgment_key():
    query = _Client({"found": False, "claim": None})
    port = TraderPort(query, _Client({}))
    assert port.claim_readback("sha256:" + "1" * 64) is None
    assert query.calls == [("get_evaluation_claim", {"request_id": "sha256:" + "1" * 64})]
    with pytest.raises(ValueError):
        port.judgment()
    with pytest.raises(ValueError):
        port.judgment(judgment_id="j", case_digest="c")


# -- fix round 1: retried submits and restarts ---------------------------------------------


def test_a_retried_submit_after_its_holdout_opened_is_a_duplicate(world, monkeypatch):
    service = world.service()
    first = service.submit(request(), AI)
    service.run_next()
    monkeypatch.setattr(world.registry, "opened_holdout_windows", lambda path, cls: [
        {"artifact_id": "a", "family_id": "x", "start": dt.date(2024, 3, 22), "end": dt.date(2024, 4, 30)}])
    retry = service.submit(request(), AI)                               # the ACCEPTED reply was lost
    assert (retry["status"], retry["request_id"], retry["state"]) == ("DUPLICATE", first["request_id"], "DONE")


def test_a_retried_submit_after_the_allowlist_changed_is_a_duplicate(world, monkeypatch):
    service = world.service()
    first = service.submit(request(), AI)
    monkeypatch.setattr(BacktestJudgeConfig, "allows", lambda self, key: False)    # the owner removed it
    retry = service.submit(request(), AI)
    assert (retry["status"], retry["request_id"], retry["state"]) == ("DUPLICATE", first["request_id"], "QUEUED")
    assert service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)["code"] == "STRATEGY_NOT_ALLOWED"


class _HoldFailsOnce(ResearchStore):
    def __init__(self, db):
        super().__init__(db)
        self.failed = False

    def hold_report(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("crash after the case was signed")
        return super().hold_report(*args, **kwargs)


def test_a_crash_after_signing_resumes_with_the_same_case_and_never_evaluates_again(world):
    calls = []
    evaluate = fake_evaluate(world.registry)

    def counted(spec):
        calls.append(spec.request_id)
        return evaluate(spec)
    crashing = world.service(evaluate=counted, store=_HoldFailsOnce(world.db))
    request_id = crashing.submit(request(), AI)["request_id"]
    with pytest.raises(RuntimeError):
        crashing.run_next()
    signed = ResearchStore(world.db).case(request_id=request_id)
    assert signed is not None and len(trials(world)) == 3

    restarted = world.service(evaluate=counted)
    restarted.recover()
    assert restarted.run_next() is True
    view = restarted.get(request_id, AI)
    assert (view["state"], view["case_digest"]) == ("DONE", signed["case_digest"])
    assert calls == [request_id] and len(trials(world)) == 3
    assert world.trader.claims[request_id]["state"] == "DONE"
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE")]


def test_a_held_report_is_sent_again_by_recover_after_a_restart(world):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    world.trader.lose_update["DONE"] = "request"
    service.run_next()
    assert service.get(request_id, AI)["case_digest"] is None

    restarted = world.service()
    restarted.recover()
    view = restarted.get(request_id, AI)
    assert view["state"] == "DONE" and view["case_digest"] is not None
    assert world.trader.claims[request_id]["state"] == "DONE"
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE"), (request_id, "DONE")]
    assert restarted.run_next() is False and len(trials(world)) == 3


OTHER = {"cohort": [{"ENTRY_MINUTE": 630}]}


def test_a_signed_case_that_fails_verification_is_parked_and_the_worker_goes_on(world, caplog):
    service = world.service()
    stuck = service.submit(request(), AI)["request_id"]
    ResearchStore(world.db).record_case("sha256:" + "0" * 64, stuck, "COMPLETE", NOW)    # no such case file
    other = service.submit(request(**OTHER), AI)["request_id"]
    with caplog.at_level("ERROR", logger="trader.research.evaluation_service"):
        assert service.run_next() is True                                       # parks, does not raise
    assert [r.getMessage() for r in caplog.records if stuck in r.getMessage() and "CASE_NOT_FOUND" in r.getMessage()]
    row = ResearchStore(world.db).get(stuck)
    assert row["state"] == "PARKED" and row["parked_reason"].startswith("CASE_NOT_FOUND")
    assert service.run_next() is True and service.get(other, AI)["state"] == "DONE"
    view = service.get(stuck, AI)
    assert (view["found"], view["state"], view["case_digest"], view["summary"]) == (True, "FAILED", None, None)
    assert service.submit(request(), AI)["state"] == "FAILED"                   # a retry is a duplicate, wire FAILED


def test_a_parked_request_takes_no_queue_slot_and_is_not_requeued_by_a_restart(world):
    service = world.service()
    first = service.submit(request(), AI)["request_id"]
    ResearchStore(world.db).record_case("sha256:" + "0" * 64, first, "COMPLETE", NOW)
    service.run_next()
    assert ResearchStore(world.db).count(("QUEUED", "RUNNING")) == 0
    restarted = world.service()
    restarted.recover()
    assert restarted.run_next() is False
    assert ResearchStore(world.db).get(first)["state"] == "PARKED"


def test_a_queued_request_the_trader_has_no_claim_for_is_parked_at_recover(world, caplog):
    stuck = world.service().submit(request(), AI)["request_id"]
    other = world.service().submit(request(**OTHER), AI)["request_id"]
    del world.trader.claims[stuck]
    restarted = world.service()
    with caplog.at_level("ERROR", logger="trader.research.evaluation_service"):
        restarted.recover()                                                      # does not raise
    assert ResearchStore(world.db).get(stuck)["state"] == "PARKED"
    assert any(stuck in r.getMessage() and "SERVICE_STATE_MISMATCH" in r.getMessage() for r in caplog.records)
    assert restarted.run_next() is True and restarted.get(other, AI)["state"] == "DONE"
    assert restarted.get(stuck, AI)["state"] == "FAILED"


@pytest.mark.parametrize("code", ["INTERNAL_ERROR", "PERMISSION_DENIED", "AUTHENTICATION_ERROR"])
def test_an_infrastructure_error_from_the_trader_is_not_parked_it_stops_the_worker(world, monkeypatch, code):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]

    def refuse(request_id, state):
        raise TypedRpcRemoteError(code, "boom")
    monkeypatch.setattr(world.trader, "update_claim", refuse)
    with pytest.raises(TypedRpcRemoteError):
        service.run_next()
    assert ResearchStore(world.db).get(request_id)["state"] != "PARKED"


def test_a_case_refusal_from_the_trader_on_the_end_report_parks_the_request(world, monkeypatch):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    real = world.trader.update_claim

    def refuse_end(request_id, state):
        if state == "RUNNING":
            return real(request_id, state)
        raise TypedRpcRemoteError("CASE_CLAIM_MISMATCH", "the case and the claim disagree")
    monkeypatch.setattr(world.trader, "update_claim", refuse_end)
    assert service.run_next() is True
    assert service.get(request_id, AI) == {"found": True, "request_id": request_id, "state": "FAILED",
                                           "case_digest": None, "summary": None}
    assert ResearchStore(world.db).pending_reports() == []
