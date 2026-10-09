"""The research process entry: start order, recovery retry, and a dead worker (SP2c Plan 3 Task 8)."""
import datetime as dt
import logging
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from tests.research.evaluation_fixtures import write_costs_config
from tests.research.service_fakes import FakeTrader
from tests.rpc_identity_fixtures import free_port, make_identities, write_keyset
from trader import research_service
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcClient, TypedRpcRemoteError
from trader.research.schema import apply_research_migrations
from trader.research.service_store import ResearchStore
from trader.research.shadow_replay import ShadowReplay
from trader.research.signing import AttestationSigner
from trader.research.evaluation_service import EvaluationService
from trader.research.signing import generate_private_key_pem
from trader.research.trader_port import TraderPort, TraderUnavailable
from trader.research_service import ResearchRuntime, recover_until_reachable, run_service

NOW = dt.datetime(2024, 3, 29, 14, tzinfo=dt.timezone.utc)
WAIT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(research_service, "RECOVER_FIRST_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(research_service, "RECOVER_MAX_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(research_service, "WATCH_INTERVAL_SECONDS", 0.01)


class FakeServer:
    def __init__(self, events, name):
        self._events, self._name = events, name
        self.closed = threading.Event()

    async def serve(self):
        self._events.append(f"serve:{self._name}")

    async def aclose(self):
        self._events.append(f"close:{self._name}")
        self.closed.set()


class FakeEvaluations:
    def __init__(self, events, failures=0):
        self._events, self.failures, self.recover_calls = events, failures, 0
        self.recovered = threading.Event()

    def recover(self):
        self.recover_calls += 1
        if self.failures:
            self.failures -= 1
            raise TraderUnavailable("get_evaluation_claim: TimeoutError")
        self._events.append("recover")
        self.recovered.set()


def run_in_thread(runtime, stop):
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("code", run_service(runtime, stop)), daemon=True)
    thread.start()
    return thread, outcome


def wait_for(condition):
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_recovery_finishes_before_the_worker_starts_and_before_any_socket_opens():
    events = []
    evaluations = FakeEvaluations(events)
    servers = [FakeServer(events, "query"), FakeServer(events, "command")]

    def worker(stop):
        events.append("worker")
        stop.wait()

    stop = threading.Event()
    thread, outcome = run_in_thread(ResearchRuntime(evaluations, servers, [worker]), stop)
    wait_for(lambda: "serve:command" in events and "worker" in events)
    stop.set()
    thread.join(WAIT_SECONDS)
    assert events[0] == "recover"
    assert events.index("recover") < events.index("worker")
    assert events.index("recover") < events.index("serve:query")
    assert outcome["code"] == 0
    assert all(server.closed.is_set() for server in servers)


def test_an_unreachable_trader_holds_back_the_worker_and_the_sockets_and_warns_each_time(caplog):
    events = []
    evaluations = FakeEvaluations(events, failures=3)
    server = FakeServer(events, "command")
    stop = threading.Event()
    with caplog.at_level(logging.WARNING, logger="trader.research_service"):
        thread, outcome = run_in_thread(ResearchRuntime(evaluations, [server], [lambda s: s.wait()]), stop)
        wait_for(lambda: "serve:command" in events)
    stop.set()
    thread.join(WAIT_SECONDS)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "unreachable" in r.getMessage()]
    assert evaluations.recover_calls == 4 and len(warnings) == 3
    assert events == ["recover", "serve:command", "close:command"]


def test_while_the_trader_is_unreachable_nothing_is_served():
    events = []
    evaluations = FakeEvaluations(events, failures=10_000)
    server = FakeServer(events, "command")
    stop = threading.Event()
    thread, outcome = run_in_thread(ResearchRuntime(evaluations, [server], [lambda s: s.wait()]), stop)
    wait_for(lambda: evaluations.recover_calls >= 3)
    assert events == []
    stop.set()
    thread.join(WAIT_SECONDS)
    assert outcome["code"] == 0 and events == []


def test_recovery_failures_other_than_an_unreachable_trader_end_the_process_loudly():
    class Disagreeing:
        def recover(self):
            raise TypedRpcRemoteError("INTERNAL_ERROR", "no")

    with pytest.raises(TypedRpcRemoteError):
        recover_until_reachable(Disagreeing(), threading.Event())


def test_a_worker_that_raises_stops_the_service_with_an_error_log_and_a_non_zero_exit(caplog):
    events = []
    server = FakeServer(events, "command")

    def worker(stop):
        raise TypedRpcRemoteError("INTERNAL_ERROR", "the trader failed")

    with caplog.at_level(logging.ERROR, logger="trader.research_service"):
        code = run_service(ResearchRuntime(FakeEvaluations(events), [server], [worker]), threading.Event())
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert code == 1
    assert len(errors) == 1 and errors[0].exc_info and errors[0].exc_info[0] is TypedRpcRemoteError
    assert server.closed.is_set()


def test_a_worker_that_returns_while_the_service_runs_is_a_death_too(caplog):
    server = FakeServer([], "command")
    with caplog.at_level(logging.ERROR, logger="trader.research_service"):
        code = run_service(ResearchRuntime(FakeEvaluations([]), [server], [lambda stop: None]), threading.Event())
    assert code == 1 and "returned early" in caplog.text


def test_a_dead_worker_lets_the_running_evaluation_finish_before_the_exit(monkeypatch, caplog):
    monkeypatch.setattr(research_service, "WORKER_JOIN_SECONDS", 0.05)
    events, started, finished = [], threading.Event(), threading.Event()

    def evaluation_worker(stop):
        while not stop.is_set():
            started.set()
            events.append("evaluation started")
            time.sleep(0.5)                                  # an evaluation with an open holdout ignores stop
            events.append("evaluation finished")
            finished.set()

    def shadow_worker(stop):
        started.wait(WAIT_SECONDS)
        raise TypedRpcRemoteError("INTERNAL_ERROR", "the trader failed")

    server = FakeServer(events, "command")
    runtime = ResearchRuntime(FakeEvaluations(events), [server], [evaluation_worker, shadow_worker],
                              holdout_jobs=(evaluation_worker,))
    with caplog.at_level(logging.WARNING, logger="trader.research_service"):
        code = run_service(runtime, threading.Event())
    assert code == 1 and finished.is_set() and server.closed.is_set()
    assert events.count("evaluation started") == 1                       # no new evaluation after the death
    assert events.index("close:command") < events.index("evaluation finished")
    assert any(r.levelno == logging.WARNING and "evaluation" in r.getMessage() for r in caplog.records)


def test_the_wait_for_the_running_evaluation_after_a_death_is_bounded(monkeypatch, caplog):
    monkeypatch.setattr(research_service, "WORKER_JOIN_SECONDS", 0.05)
    monkeypatch.setattr(research_service, "HOLDOUT_FINISH_SECONDS", 0.2)
    never = threading.Event()

    def stuck_evaluation(stop):
        never.wait(WAIT_SECONDS * 2)

    def dying(stop):
        raise TypedRpcRemoteError("INTERNAL_ERROR", "the trader failed")

    runtime = ResearchRuntime(FakeEvaluations([]), [FakeServer([], "command")], [stuck_evaluation, dying],
                              holdout_jobs=(stuck_evaluation,))
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger="trader.research_service"):
        code = run_service(runtime, threading.Event())
    assert code == 1 and time.monotonic() - started < WAIT_SECONDS
    assert any("still running" in r.getMessage() for r in caplog.records)


class OwedReportStore:
    """One signed case whose end report the trader refuses once the service is up."""

    def __init__(self):
        self.owed = False

    def pending(self):
        return []

    def pending_reports(self):
        return [{"request_id": "sha256:" + "1" * 64, "pending_report": "DONE"}] if self.owed else []


class RefusingTrader:
    def __init__(self, code):
        self.code = code

    def update_claim(self, request_id, state):
        raise TypedRpcRemoteError(self.code, "refused by the fake trader")


@pytest.mark.parametrize("code", ["INTERNAL_ERROR", "PERMISSION_DENIED", "AUTHENTICATION_ERROR"])
def test_an_infrastructure_error_from_the_trader_ends_the_service_with_exit_1(caplog, code):
    store = OwedReportStore()
    service = EvaluationService(store=store, trader=RefusingTrader(code), build_spec=None, evaluate=None,
                                signer=SimpleNamespace(), artifacts_root="/nonexistent", warmup_sessions=5,
                                order_notional=1900.0, queue_max=1, now=lambda: NOW)
    server = FakeServer([], "command")
    real_recover = service.recover
    service.recover = lambda: (real_recover(), setattr(store, "owed", True))[0]
    runtime = ResearchRuntime(service, [server], [lambda stop: service.serve_forever(stop, idle_seconds=0.01)])
    with caplog.at_level(logging.ERROR, logger="trader.research_service"):
        code_returned = run_service(runtime, threading.Event())
    assert code_returned == 1 and server.closed.is_set()
    assert any(r.exc_info and r.exc_info[0] is TypedRpcRemoteError for r in caplog.records)


@pytest.fixture
def real_store(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    return ResearchStore(db)


def queued_row(store, request_id, *, claimed_by):
    body = {"strategy_key": "strategies/x.py:X", "cohort": [{"A": 1}], "conids": [1, 2], "bar_size": "15 mins",
            "research_day": "2024-03-29"}
    store.begin(request_id, body, "strategies/x.py:X", "sha256:" + "f" * 64, NOW)
    store.set_state(request_id, "QUEUED", now=NOW, ny_day=dt.date(2024, 3, 29))
    if claimed_by is not None:
        claimed_by.claims[request_id] = {"request_id": request_id, "strategy_key": "strategies/x.py:X",
                                         "ny_day": "2024-03-29", "state": "QUEUED", "body": body,
                                         "claimed_at": NOW.isoformat(), "updated_at": NOW.isoformat()}


def real_service(store, trader):
    return EvaluationService(store=store, trader=trader, build_spec=None, evaluate=None, signer=AttestationSigner.generate(),
                             artifacts_root="/nonexistent", warmup_sessions=5, order_notional=1900.0, queue_max=5,
                             now=lambda: NOW)


def serve_until_parked(runtime, store, request_id):
    stop = threading.Event()
    thread, outcome = run_in_thread(runtime, stop)
    wait_for(lambda: store.get(request_id)["state"] == "PARKED")
    return stop, thread, outcome


def test_a_signed_case_that_fails_verification_is_parked_and_the_service_keeps_serving(real_store, caplog):
    trader, events = FakeTrader(), []
    request_id = "sha256:" + "1" * 64
    queued_row(real_store, request_id, claimed_by=trader)
    real_store.record_case("sha256:" + "0" * 64, request_id, "COMPLETE", NOW)          # the file is gone
    service = real_service(real_store, trader)
    runtime = ResearchRuntime(service, [FakeServer(events, "command")],
                              [lambda stop: service.serve_forever(stop, idle_seconds=0.01)])
    with caplog.at_level(logging.ERROR):
        stop, thread, outcome = serve_until_parked(runtime, real_store, request_id)
        assert "serve:command" in events and thread.is_alive()                 # still serving
        stop.set()
        thread.join(WAIT_SECONDS)
    assert outcome["code"] == 0
    assert any(request_id in r.getMessage() and "CASE_NOT_FOUND" in r.getMessage() for r in caplog.records)
    assert service.get(request_id, SimpleNamespace(principal="ai_research"))["state"] == "FAILED"


def test_a_queued_request_with_no_trader_claim_is_parked_and_the_service_starts(real_store, caplog):
    trader, events = FakeTrader(), []
    request_id = "sha256:" + "2" * 64
    queued_row(real_store, request_id, claimed_by=None)
    service = real_service(real_store, trader)
    runtime = ResearchRuntime(service, [FakeServer(events, "command")],
                              [lambda stop: service.serve_forever(stop, idle_seconds=0.01)])
    with caplog.at_level(logging.ERROR):
        stop, thread, outcome = serve_until_parked(runtime, real_store, request_id)
        wait_for(lambda: "serve:command" in events)
        stop.set()
        thread.join(WAIT_SECONDS)
    assert outcome["code"] == 0
    assert any(request_id in r.getMessage() and "SERVICE_STATE_MISMATCH" in r.getMessage() for r in caplog.records)


class UnreachableTrader:
    """The real port over real typed clients that nobody answers (the trader process is still starting)."""

    def __init__(self):
        identity = make_identities()["research"]
        self.clients = [TypedRpcClient(role, identity, server="trader", port=free_port(), timeout=0.2)
                        for role in ("query", "command")]
        for client in self.clients:
            client.connect()
        self.port = TraderPort(*self.clients)

    def close(self):
        for client in self.clients:
            client.close()


def test_a_trader_that_is_still_starting_is_unavailable_not_a_failure():
    trader = UnreachableTrader()
    try:
        with pytest.raises(TraderUnavailable):
            trader.port.claim_readback("sha256:" + "1" * 64)
        with pytest.raises(TraderUnavailable):
            trader.port.claim("sha256:" + "1" * 64, {})
    finally:
        trader.close()


def test_recovery_against_a_starting_trader_retries_and_stops_cleanly_on_a_signal(real_store, caplog):
    trader = UnreachableTrader()
    try:
        queued_row(real_store, "sha256:" + "3" * 64, claimed_by=None)
        service = real_service(real_store, trader.port)
        stop = threading.Event()
        with caplog.at_level(logging.WARNING, logger="trader.research_service"):
            thread, outcome = run_in_thread(ResearchRuntime(service, [FakeServer([], "q")], []), stop)
            wait_for(lambda: sum("unreachable" in r.getMessage() for r in caplog.records) >= 2)
            stop.set()
            thread.join(WAIT_SECONDS)
        assert outcome["code"] == 0
        assert real_store.get("sha256:" + "3" * 64)["state"] == "QUEUED"        # nothing parked, nothing lost
    finally:
        trader.close()


def test_run_service_waits_for_a_running_tick_after_stop_but_not_forever(monkeypatch):
    monkeypatch.setattr(research_service, "WORKER_JOIN_SECONDS", 0.3)
    finished = threading.Event()

    def slow_worker(stop):
        stop.wait()
        time.sleep(0.15)
        finished.set()

    stop = threading.Event()
    thread, outcome = run_in_thread(ResearchRuntime(FakeEvaluations([]), [FakeServer([], "q")], [slow_worker]), stop)
    time.sleep(0.1)
    stop.set()
    thread.join(WAIT_SECONDS)
    assert finished.is_set() and outcome["code"] == 0

    never = threading.Event()
    stop = threading.Event()
    started = time.monotonic()
    thread, outcome = run_in_thread(ResearchRuntime(FakeEvaluations([]), [FakeServer([], "q")],
                                                    [lambda s: (s.wait(), never.wait(WAIT_SECONDS * 2))]), stop)
    time.sleep(0.1)
    stop.set()
    thread.join(WAIT_SECONDS)
    assert outcome["code"] == 0 and time.monotonic() - started < WAIT_SECONDS


def test_a_stop_signal_during_the_retry_wait_ends_the_service_cleanly():
    events = []
    evaluations = FakeEvaluations(events, failures=10_000)
    stop = threading.Event()
    stop.set()
    assert run_service(ResearchRuntime(evaluations, [FakeServer(events, "q")], []), stop) == 0
    assert events == [] and evaluations.recover_calls == 0


@pytest.fixture
def config_world(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    write_keyset(keys / "rpc")
    (keys / "private").mkdir()
    signing = keys / "private" / "signing.pem"
    signing.write_bytes(generate_private_key_pem())
    signing.chmod(0o600)
    write_costs_config(tmp_path / "execution_costs.yaml")
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(keys / "rpc"))
    environ = {"MMR_RESEARCH_DUCKDB": str(tmp_path / "research.duckdb"), "TRADER_TYPED_ADDRESS": "tcp://127.0.0.1"}

    def write_config(**extra):
        config = tmp_path / "trader.yaml"
        config.write_text(yaml.safe_dump({"duckdb_path": str(tmp_path / "mmr.duckdb"),
                                          "history_duckdb_path": str(tmp_path / "history.duckdb"), **extra}))
        return str(config)
    return SimpleNamespace(environ=environ, write_config=write_config)


def test_build_runtime_wires_one_service_and_the_default_ports(config_world):
    runtime = research_service.build_runtime(config_path=config_world.write_config(),
                                             environ=config_world.environ, now=lambda: NOW)
    try:
        assert [(s.socket_role, s.address) for s in runtime.servers] == [
            ("query", "tcp://127.0.0.1:42106"), ("command", "tcp://127.0.0.1:42107")]
        evaluations_job, shadow_job = runtime.background
        assert evaluations_job == runtime.evaluations.serve_forever
        assert runtime.holdout_jobs == (evaluations_job,)                   # only evaluations open a holdout
        assert isinstance(shadow_job.__self__, ShadowReplay) and shadow_job.__name__ == "serve_forever"
        registry = runtime.servers[0].registry
        assert registry is runtime.servers[1].registry
    finally:
        for server in runtime.servers:
            server.close()


def test_build_runtime_takes_ports_and_bind_address_from_the_config_and_environment(config_world):
    config = config_world.write_config(research_typed_query_port=43106, research_typed_command_port=43107)
    environ = {**config_world.environ, "RESEARCH_TYPED_BIND_ADDRESS": "tcp://0.0.0.0"}
    runtime = research_service.build_runtime(config_path=config, environ=environ, now=lambda: NOW)
    try:
        assert [s.address for s in runtime.servers] == ["tcp://0.0.0.0:43106", "tcp://0.0.0.0:43107"]
    finally:
        for server in runtime.servers:
            server.close()


def test_build_runtime_refuses_a_config_without_the_data_paths(config_world, tmp_path):
    config = tmp_path / "bare.yaml"
    config.write_text("{}")
    with pytest.raises(ValueError, match="duckdb_path"):
        research_service.build_runtime(config_path=str(config), environ=config_world.environ, now=lambda: NOW)
