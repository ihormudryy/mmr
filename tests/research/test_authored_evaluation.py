"""SP3 spec 7.6 and 8: STARTED, the smoke stage and the end of a sandbox error in an authored evaluation."""
import pandas as pd
import pytest

from tests.authored.fake_sandbox import IMAGE_DIGEST, caller_source, fault
from tests.research.authored_fixtures import (CATALOGUE_KEY, NOW, install_fake_sandbox, research_world,
                                             time_of_day_key)
from tests.research.service_fakes import FakeTrader
from trader.authored.keys import authored_key
from trader.authored.sandbox_client import SandboxProtocolError, SandboxUnavailable
from trader.automation.authored_wire import ReportAuthoredEvaluationRequest
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.research.artifact import ExperimentFamily
from trader.research.authored_backtest import SandboxOpenFault
from trader.research.authored_evaluation import SMOKE_SESSIONS, AuthoredEvaluator
from trader.research.authored_outcome import (AUTHORED_END_CODE, AuthoredEnd, AuthoredEvaluationEnded, end_for,
                                             started_body)
from trader.research.evaluation import EvaluationError
from trader.research.evaluation_jobs import run_window_job
from trader.research.evaluation_service import ServiceStateError
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.trader_port import TraderUnavailable
from trader.sandbox.protocol import SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR

BARS_PER_SESSION = 26                                   # 15-minute bars, 09:30-16:00 New York


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    return research_world(tmp_path, tmp_duckdb_path)


@pytest.fixture
def sandbox(monkeypatch):
    return install_fake_sandbox(monkeypatch)


def evaluator(world, trader=None, **overrides):
    return AuthoredEvaluator(research_db=world.db, paths=world.paths, now=lambda: NOW,
                             trader=trader or FakeTrader(), **overrides)


def ended(world, spec, **overrides):
    with pytest.raises(AuthoredEvaluationEnded) as raised:
        evaluator(world, **overrides).evaluate(spec)
    return raised.value.end


def nothing_spent(world, spec):
    base = spec.base
    return (world.registry.strategy_trials(base.strategy_path, base.class_name) == []
            and world.registry.opened_holdout_windows(base.strategy_path, base.class_name) == [])


def recording(calls):
    def run_cohort(spec, **kwargs):
        calls.append(kwargs)
    return run_cohort


def opens_a_holdout_then(error):
    def run_cohort(spec, *, research_db, **_):
        registry = ExperimentRegistry(research_db)
        family_id = registry.create_family(ExperimentFamily(
            strategy_path=spec.base.strategy_path, class_name=spec.base.class_name, repository_commit="authored",
            source_tree_digest="s", dependency_lock_digest="d", container_digest="unpinned",
            dataset_manifest_digest="m", search_space={}, cost_model={}, validation_protocol={}),
            created_at=NOW, validation_folds=[{"kind": "holdout", "start": "2024-03-21", "end": "2024-03-28"}])
        trial_id = registry.start_trial(family_id, trial_key="t", parameters={}, started_at=NOW)
        registry.finish_trial(trial_id, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": 0.1})
        artifact = registry.seal_artifact(family_id, selected_trial_id=trial_id, selected_parameters={},
                                          sealed_at=NOW)
        registry.begin_holdout(artifact, opened_at=NOW)
        raise EvaluationError("backtest failed") from error
    return run_cohort


@pytest.mark.timeout(300)
def test_an_authored_cohort_scores_like_the_catalogue_strategy(world, sandbox):
    catalogue = evaluator(world).evaluate(world.build(CATALOGUE_KEY))
    authored = evaluator(world).evaluate(world.build(time_of_day_key(sandbox)))
    assert authored.stage == catalogue.stage
    assert ([v.point.metrics["n_round_trips"] for v in authored.verdicts]
            == [v.point.metrics["n_round_trips"] for v in catalogue.verdicts])
    assert ([v.point.metrics["oos_net_pnl"] for v in authored.verdicts]
            == pytest.approx([v.point.metrics["oos_net_pnl"] for v in catalogue.verdicts]))
    assert sandbox.most_open == 1                        # Review Focus 3


def test_a_catalogue_key_reports_nothing_and_runs_no_smoke_stage(world, sandbox):
    calls, trader = [], FakeTrader()
    evaluator(world, trader, run_cohort=recording(calls)).evaluate(world.build(CATALOGUE_KEY))
    assert sandbox.sessions == [] and trader.authored_reports == [] and len(calls) == 1


def test_started_names_the_research_image_and_carries_the_judged_count(world, sandbox):
    calls, trader = [], FakeTrader()
    trader.judged_count = 2
    spec = world.build(time_of_day_key(sandbox))
    evaluator(world, trader, run_cohort=recording(calls)).evaluate(spec)
    assert trader.authored_reports == [started_body(spec.request_id, spec.base.authored_digest, IMAGE_DIGEST)]
    ReportAuthoredEvaluationRequest.model_validate(trader.authored_reports[0])     # a real request id and digest
    assert (calls[0]["authored_judged_count"], calls[0]["max_workers"]) == (2, 1)


def test_smoke_streams_the_first_sessions_of_the_smallest_conid(world, sandbox):
    evaluator(world, run_cohort=recording([])).evaluate(world.build(time_of_day_key(sandbox)))
    smoke = sandbox.sessions[0]
    assert len(sandbox.sessions) == 1
    assert len(smoke.stamps) == SMOKE_SESSIONS * BARS_PER_SESSION
    assert smoke.stamps[0] == pd.Timestamp("2024-02-05 14:30", tz="UTC")    # the first of the 38-session period
    assert smoke.first_close < 110.0                     # conid 1001 trades near 100, the next conid near 110


def test_an_unreachable_sandbox_at_start_returns_the_file_to_stored_and_starts_nothing(world, sandbox):
    sandbox.unavailable = True
    trader = FakeTrader()
    spec = world.build(time_of_day_key(sandbox))
    end = ended(world, spec, trader=trader)
    assert end.event == "SANDBOX_UNAVAILABLE"
    assert trader.authored_reports == [] and nothing_spent(world, spec)


def test_a_refused_start_runs_nothing(world, sandbox):
    trader = FakeTrader()
    trader.refuse_authored_report = ("AUTHORED_STATE_CONFLICT", False)
    with pytest.raises(ServiceStateError):
        evaluator(world, trader).evaluate(world.build(time_of_day_key(sandbox)))
    assert sandbox.sessions == []


def test_a_wire_error_at_start_parks_instead_of_signing_a_case(world, sandbox):
    trader = FakeTrader()

    def wire_error(body):
        raise TypedRpcRemoteError("VALIDATION_ERROR", "invalid request body")
    trader.report_authored_evaluation = wire_error
    with pytest.raises(ServiceStateError):
        evaluator(world, trader).evaluate(world.build(time_of_day_key(sandbox)))


def test_an_unreachable_trader_at_start_ends_the_worker(world, sandbox):
    trader = FakeTrader()
    trader.lose_authored_report = True
    with pytest.raises(TraderUnavailable):
        evaluator(world, trader).evaluate(world.build(time_of_day_key(sandbox)))
    assert sandbox.sessions == []


def test_a_fault_in_the_smoke_stage_rejects_without_a_trial_or_a_holdout(world, sandbox):
    spec = world.build(authored_key(sandbox.add(caller_source("faults_above_limit")), "FaultsAboveLimit"),
                       cohort=({"LIMIT": 1.0},))
    end = ended(world, spec)
    assert (end.event, end.detail) == ("SANDBOX_ERROR", SANDBOX_ERROR)
    assert nothing_spent(world, spec) and len(sandbox.sessions) == 1


def test_an_unreachable_sandbox_at_the_smoke_stage_returns_the_file_to_stored(world, sandbox):
    sandbox.refuse_opens_after = 0                       # PING answers, no session opens
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec).event == "SANDBOX_UNAVAILABLE"
    assert nothing_spent(world, spec)


@pytest.mark.timeout(240)
def test_an_unreachable_sandbox_before_the_holdout_returns_the_file_to_stored(world, sandbox):
    sandbox.refuse_opens_after = 1                       # the smoke session opens, no fold session does
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec).event == "SANDBOX_UNAVAILABLE"
    assert world.registry.opened_holdout_windows(spec.base.strategy_path, spec.base.class_name) == []


def test_an_unreachable_sandbox_after_the_holdout_opened_is_the_sp2c_failure(world, sandbox):
    with pytest.raises(EvaluationError):
        evaluator(world, run_cohort=opens_a_holdout_then(SandboxUnavailable("gone"))).evaluate(
            world.build(time_of_day_key(sandbox)))


def test_a_fault_after_the_holdout_opened_still_rejects(world, sandbox):
    end = ended(world, world.build(time_of_day_key(sandbox)), run_cohort=opens_a_holdout_then(fault(SANDBOX_ERROR)))
    assert end.event == "SANDBOX_ERROR"


def test_an_error_outside_the_sandbox_passes_through(world, sandbox):
    def broken(spec, **_):
        raise EvaluationError("market context unavailable")
    with pytest.raises(EvaluationError):
        evaluator(world, run_cohort=broken).evaluate(world.build(time_of_day_key(sandbox)))


REBUILT_IMAGE = "b9c8d7e-20261012T010000Z"


def rebuild_before_window(sandbox):
    """The sandbox restarts onto another build between STARTED and the first session (pre-flight F7)."""
    def run_window(env, job):
        sandbox.image_digest = REBUILT_IMAGE
        return run_window_job(env, job)
    return run_window


def rebuild_after_window(sandbox):
    def run_window(env, job):
        outcome = run_window_job(env, job)
        sandbox.image_digest = REBUILT_IMAGE
        return outcome
    return run_window


def test_a_smoke_session_on_another_build_than_started_is_unavailable(world, sandbox):
    spec = world.build(time_of_day_key(sandbox))
    end = ended(world, spec, run_window=rebuild_before_window(sandbox))
    assert end == AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")
    assert nothing_spent(world, spec) and sandbox.open_sessions == []


@pytest.mark.timeout(240)
def test_a_fold_session_on_another_build_than_started_is_unavailable(world, sandbox):
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec, run_window=rebuild_after_window(sandbox)).event == "SANDBOX_UNAVAILABLE"
    assert world.registry.opened_holdout_windows(spec.base.strategy_path, spec.base.class_name) == []
    assert sandbox.open_sessions == []

def test_the_reports_match_the_trader_wire_model():
    digest, request_id = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    ReportAuthoredEvaluationRequest.model_validate(started_body(request_id, digest, IMAGE_DIGEST))
    for end in (AuthoredEnd("SANDBOX_ERROR", SANDBOX_ERROR), AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")):
        ReportAuthoredEvaluationRequest.model_validate(end.report_body(request_id, digest))
        assert AuthoredEnd.from_row({"code": AUTHORED_END_CODE, "detail": end.marker()}) == end


def test_only_a_strategy_fault_rejects_the_file():
    assert end_for(fault(SANDBOX_DIGEST_MISMATCH)) == AuthoredEnd("SANDBOX_ERROR", SANDBOX_DIGEST_MISMATCH)
    assert end_for(fault("SANDBOX_SOMETHING_NEW")) == AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxFault")
    assert end_for(SandboxUnavailable("busy")) == AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")


@pytest.mark.parametrize("error", [fault(SANDBOX_ERROR), SandboxProtocolError("bad frame")])
def test_a_fault_on_ping_is_unavailable_and_reports_nothing(world, sandbox, error):    # Task 6 review (a)
    def ping():
        raise error
    sandbox.ping = ping
    trader = FakeTrader()
    end = ended(world, world.build(time_of_day_key(sandbox)), trader=trader)
    assert end == AuthoredEnd("SANDBOX_UNAVAILABLE", type(error).__name__)
    assert trader.authored_reports == [] and sandbox.sessions == []


def faulting_open(sandbox, *, rebuilt_to=None, gone=False):
    """OPEN answers SANDBOX_ERROR (the module did not load); the sandbox may have restarted meanwhile."""
    def open_session(*args, **kwargs):
        if rebuilt_to is not None:
            sandbox.image_digest = rebuilt_to
        sandbox.unavailable = gone
        raise fault(SANDBOX_ERROR)
    sandbox.open_session = open_session


def test_an_open_fault_on_a_rebuilt_sandbox_is_unavailable(world, sandbox):          # ruling F7b
    faulting_open(sandbox, rebuilt_to=REBUILT_IMAGE)
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec) == AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")
    assert nothing_spent(world, spec)


def test_an_open_fault_on_the_started_build_rejects_the_file(world, sandbox):
    faulting_open(sandbox)
    assert ended(world, world.build(time_of_day_key(sandbox))) == AuthoredEnd("SANDBOX_ERROR", SANDBOX_ERROR)


def test_an_open_fault_with_the_sandbox_gone_is_unavailable(world, sandbox):
    faulting_open(sandbox, gone=True)
    end = ended(world, world.build(time_of_day_key(sandbox)))
    assert end == AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")


def test_a_bar_fault_is_the_strategy_even_if_the_sandbox_restarts_after_it(world, sandbox):
    def run_window(env, job):
        try:
            return run_window_job(env, job)
        finally:
            sandbox.image_digest = REBUILT_IMAGE
    spec = world.build(authored_key(sandbox.add(caller_source("faults_above_limit")), "FaultsAboveLimit"),
                       cohort=({"LIMIT": 1.0},))
    assert ended(world, spec, run_window=run_window) == AuthoredEnd("SANDBOX_ERROR", SANDBOX_ERROR)


def test_an_open_fault_in_the_folds_on_a_rebuilt_sandbox_is_unavailable(world, sandbox):
    def run_cohort(spec, **_):
        sandbox.image_digest = REBUILT_IMAGE
        raise EvaluationError("backtest failed") from SandboxOpenFault("did not load", code=SANDBOX_ERROR)
    spec = world.build(time_of_day_key(sandbox))
    assert ended(world, spec, run_cohort=run_cohort) == AuthoredEnd("SANDBOX_UNAVAILABLE", "SandboxUnavailable")
