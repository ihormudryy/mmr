"""SP2c Plan 2 Task 9: the strategy service loads active AI deployments from the exact judged bytes."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from tests.strategy.ai_deployment_fixtures import StrategyNode
from tests.test_strategy_artifact_soft_load import _make_runtime, _write_strategy
from tests.test_strategy_runtime import _make_ticker
from trader.acceptance.scenario import AcceptanceSettings, deployment_record
from trader.data.backtest_store import compute_strategy_hash
from trader.data.duckdb_store import DuckDBConnection, DuckDBDataStore
from trader.data.strategy_signal_record import StrategySignalRecord
from trader.listeners.ib_history_worker import IBNoDataError
from trader.messaging.ai_deployment_wire import ActiveAiDeployment
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.objects import Action, BarSize
from trader.strategy.ai_deployment_source import AiDeploymentSource, ai_instance_name
from trader.strategy.trader_gateway import StrategyInstrument
from trader.trading.strategy import Signal, StrategyState

VERSION = "sha256:" + "d" * 64
BASE = "sha256:" + "b" * 64
CONID = 265598


def _frame():
    index = pd.date_range("2026-11-02 14:31", periods=1, freq="1min", tz="UTC", name="date")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}, index=index)


def active(path, *, digest=None, version=VERSION):
    return ActiveAiDeployment(
        version_digest=version, base_digest=BASE,
        strategy_path="strategies/" + os.path.basename(path),
        strategy_digest=digest or "sha256:" + compute_strategy_hash(path), class_name="VwapReclaimCat",
        params={}, conids=[CONID], bar_size="1 min", expiry_session="2026-11-06")


def source(rt, deployments, *, paper=True):
    return AiDeploymentSource(runtime=rt, read_active=lambda: deployments, paper=paper)


def failing(error):
    def read_active():
        raise error
    return read_active


def signal_of(instance, action=Action.BUY):
    return Signal(source_name=instance.name, action=action, probability=0.5, risk=0.0)


def replace_file(path):
    with open(path, "a") as f:
        f.write("# replaced\n")


@pytest.fixture
def rt(tmp_path, tmp_duckdb_path):
    runtime = _make_runtime(tmp_path, tmp_duckdb_path, automation_enabled=False)
    runtime._load_enabled = lambda name: None
    runtime._last_dispatched_bar = {}
    runtime.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(tmp_duckdb_path))
    runtime.event_store = SimpleNamespace(append=lambda event: None)
    runtime.zmq_messagebus_client = SimpleNamespace(write=lambda *args: None)
    runtime._load_ai_history = lambda instance: None      # no IB here; the history tests below use the real step
    return runtime


@pytest.fixture
def path(rt):
    return _write_strategy(rt.strategies_directory)


def instance_of(rt, version=VERSION):
    return rt.get_strategy(ai_instance_name(version))


def test_an_active_deployment_loads_under_its_version_name(rt, path):
    source(rt, [active(path)]).reconcile()
    instance = instance_of(rt)
    assert instance is not None and instance.ai_deployment_version == VERSION
    assert instance.state == StrategyState.RUNNING
    assert instance.paper_only is True


def test_a_changed_file_is_refused_at_load(rt, path):
    source(rt, [active(path, digest="sha256:" + "0" * 64)]).reconcile()
    assert rt.ai_instances() == {}


def test_a_file_replaced_after_load_drops_signals_and_unloads(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    instance = instance_of(rt)
    replace_file(path)
    rt._dispatch_signal(instance, signal_of(instance), conId=CONID, frame=_frame())
    assert rt.signal_record.read(0, 10).signals == ()
    assert instance.state == StrategyState.DISABLED
    src.reconcile()
    assert rt.ai_instances() == {}


def test_a_sell_from_a_replaced_file_is_dropped_too(rt, path):
    source(rt, [active(path)]).reconcile()
    instance = instance_of(rt)
    replace_file(path)
    rt._dispatch_signal(instance, signal_of(instance, Action.SELL), conId=CONID, frame=_frame())
    assert rt.signal_record.read(0, 10).signals == ()


def test_a_replaced_file_is_not_reloaded_by_later_reconciles(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    replace_file(path)
    src.reconcile()
    src.reconcile()
    assert rt.ai_instances() == {}


def test_a_deleted_file_unloads_the_instance(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    os.remove(path)
    src.reconcile()
    assert rt.ai_instances() == {}


def test_withdrawn_or_expired_deployments_unload(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    src._read_active = lambda: []
    src.reconcile()
    assert rt.ai_instances() == {}


def test_nothing_loads_on_live(rt, path):
    source(rt, [active(path)], paper=False).reconcile()
    assert rt.ai_instances() == {}


def test_the_runtime_itself_refuses_an_ai_load_on_live(rt, path):
    rt.paper_trading = False
    assert rt.load_ai_deployment(active(path)) is False


def test_a_live_reconcile_unloads_what_is_loaded(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    src._paper = False
    src.reconcile()
    assert rt.ai_instances() == {}


def test_signals_carry_the_binding(rt, path):
    deployment = active(path)
    source(rt, [deployment]).reconcile()
    instance = instance_of(rt)
    rt._dispatch_signal(instance, signal_of(instance), conId=CONID, frame=_frame())
    (recorded,) = rt.signal_record.read(0, 10).signals
    assert (recorded.entry.deployment_version, recorded.entry.source_digest, recorded.entry.deployment_digest) == (
        deployment.version_digest, deployment.strategy_digest, deployment.base_digest)


def test_a_config_strategy_signal_carries_no_binding(rt, path):
    rt.load_strategy(name="plain", bar_size_str="1 min", conids=[CONID], universe=None, historical_days_prior=1,
                     module=path, class_name="VwapReclaimCat", description="x")
    instance = rt.get_strategy("plain")
    rt._dispatch_signal(instance, signal_of(instance), conId=CONID, frame=_frame())
    (recorded,) = rt.signal_record.read(0, 10).signals
    assert (recorded.entry.deployment_version, recorded.entry.source_digest, recorded.entry.deployment_digest) == (
        None, None, None)


def test_a_config_strategy_cannot_take_an_ai_name(rt, path):
    rt.load_strategy(name="aidv-0123456789abcdef", bar_size_str="1 min", conids=[CONID], universe=None,
                     historical_days_prior=1, module=path, class_name="VwapReclaimCat", description="x")
    assert rt.get_strategy("aidv-0123456789abcdef") is None


@pytest.mark.parametrize("error", [ConnectionError("trader down"), TimeoutError("no reply")])
def test_an_unreachable_trader_keeps_the_loaded_set(rt, path, caplog, error):
    src = source(rt, [active(path)])
    src.reconcile()
    src._read_active = failing(error)
    src.reconcile()
    assert list(rt.ai_instances()) == [ai_instance_name(VERSION)]
    assert "keeping the loaded set" in caplog.text


def test_method_not_allowed_means_no_active_deployment(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    src._read_active = failing(TypedRpcRemoteError("METHOD_NOT_ALLOWED", "ai_paper is off"))
    src.reconcile()
    assert rt.ai_instances() == {}


def test_any_other_remote_refusal_is_not_hidden(rt):
    src = AiDeploymentSource(runtime=rt, read_active=failing(TypedRpcRemoteError("VALIDATION_ERROR", "bad")),
                             paper=True)
    with pytest.raises(TypedRpcRemoteError):
        src.reconcile()


def test_unload_strategy_removes_every_trace(rt, path):
    source(rt, [active(path)]).reconcile()
    instance = instance_of(rt)
    rt.strategies[CONID] = [instance]
    assert rt.unload_strategy(instance.name) is True
    assert rt.strategies[CONID] == [] and rt.get_strategy(instance.name) is None
    assert rt.unload_strategy(instance.name) is False


def test_a_failing_ai_reconcile_does_not_stop_the_runtime_reconcile(rt, path, caplog):
    rt._ai_deployment_source = AiDeploymentSource(runtime=rt, read_active=failing(RuntimeError("boom")), paper=True)
    rt._config_mtime = 0.0
    rt.strategy_config_file = os.path.join(os.path.dirname(path), "missing.yaml")
    rt._trader_gateway = SimpleNamespace(resolve_instrument=lambda conid: None)
    rt._revisions = None
    rt._drain_ack_outbox = lambda: None
    rt._reconcile_sync()
    assert "AI deployment reconcile failed" in caplog.text


def test_a_runtime_reconcile_subscribes_the_ai_instance_to_its_conid(rt, path):
    """The real path: the runtime reconcile loads the instance, resolves its conid and routes that conid's bars."""
    published = []
    rt._ai_deployment_source = source(rt, [active(path)])
    rt._config_mtime = 0.0
    rt.strategy_config_file = os.path.join(os.path.dirname(path), "missing.yaml")
    rt._trader_gateway = SimpleNamespace(
        resolve_instrument=lambda conid: StrategyInstrument(conid, "AAPL", "SMART", "NASDAQ", "USD", "STK",
                                                            "America/New_York") if conid == CONID else None,
        publish_instrument=lambda conid, delayed: published.append(conid))
    rt._revisions = None
    rt._drain_ack_outbox = lambda: None
    rt._reconcile_sync()
    assert rt.strategies[CONID] == [instance_of(rt)] and published == [CONID]


class BuyingStrategyFile:
    """Source of a strategy that buys on every bar, so a fed bar always yields a signal."""
    BYTES = (b"from trader.objects import Action\n"
             b"from trader.trading.strategy import Signal, Strategy\n\n"
             b"class VwapReclaimCat(Strategy):\n"
             b"    def on_prices(self, prices):\n"
             b"        return Signal(source_name=self.name, action=Action.BUY, probability=0.5, risk=0.0)\n")


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    seeded = install_seeded_judgments(monkeypatch)
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    stack.seeded = seeded
    yield stack
    stack.close()


def test_a_node_follows_the_trader_active_set_over_signed_rpc(served, tmp_path):
    strategies_dir = tmp_path / "node" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "vwap_reclaim_cat.py").write_bytes(BuyingStrategyFile.BYTES)
    settings = AcceptanceSettings(run_id="r", account_id="DU1", strategy_path="strategies/vwap_reclaim_cat.py",
                                  strategy_class="VwapReclaimCat", strategy_bytes=BuyingStrategyFile.BYTES)
    base, version = seed_judged_deployment(served.composed.stack.ai_paper, served.seeded,
                                           deployment_record(settings), today=served.now().date())
    node = StrategyNode(served, strategies_dir=strategies_dir)
    node.reconcile()
    assert list(node.instances()) == [version]

    node.feed_bar(settings.conid_a, _frame())
    (recorded,) = node.runtime.signal_record.read(0, 10).signals
    assert (recorded.entry.deployment_version, recorded.entry.deployment_digest) == (version, base)
    assert recorded.entry.source_digest == settings.strategy_digest

    served.call("cli", "withdraw_ai_deployment", {"version_digest": version, "reason": "operator"})
    node.reconcile()
    assert node.instances() == {}


APPLE = StrategyInstrument(CONID, "AAPL", "SMART", "NASDAQ", "USD", "STK", "America/New_York")
ONE_MIN = BarSize.parse_str("1 min")


def backfilled_bars():
    """Three 1-min IB bars two days back: older than any live tick, inside the priming window."""
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(second=0, microsecond=0)
    index = pd.date_range(start, periods=3, freq="1min", name="date").tz_convert("America/New_York")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": [100.1, 100.2, 100.3],
                         "volume": 1000.0, "average": 100.0, "bar_count": 10, "bar_size": "1 min",
                         "what_to_show": 1}, index=index)


class FakeHistoryClient:
    """Stands in for the IB history worker; records each request and fails while ``error`` is set."""
    def __init__(self, error=None):
        self.error, self.requests, self.bars = error, [], backfilled_bars()

    async def get_contract_history(self, *, security, what_to_show, bar_size, start_date, end_date):
        self.requests.append((security.conId, str(bar_size)))
        if self.error is not None:
            raise self.error
        return self.bars


@pytest.fixture
def history_rt(rt, loop_thread, tmp_duckdb_path):
    del rt._load_ai_history
    rt._loop = loop_thread.loop
    rt.history_duckdb_path = tmp_duckdb_path
    rt._hist_bars = {}
    rt._tick_retention_days = 2
    rt._trader_gateway = SimpleNamespace(resolve_instrument=lambda conid: APPLE if conid == CONID else None)
    return rt


def test_a_reconciled_instance_gets_its_history_before_its_first_bar(history_rt, path):
    history_rt.historical_data_client = FakeHistoryClient()
    history_rt._hist_bars[(CONID, ONE_MIN)] = pd.DataFrame()   # primed empty earlier, e.g. by a config strategy
    source(history_rt, [active(path)]).reconcile()
    assert instance_of(history_rt) is not None
    assert set(history_rt.historical_data_client.requests) == {(CONID, "1 min")}
    frame = history_rt._strategy_frame(CONID, ONE_MIN)
    backfilled = history_rt.historical_data_client.bars.index.tz_convert("UTC")
    assert frame is not None and set(backfilled) <= set(frame.index)


def test_a_history_failure_keeps_the_instance_out_and_the_next_reconcile_retries(history_rt, path, caplog):
    history = FakeHistoryClient(error=IBNoDataError("error_code: 162, error_string: No market data permissions"))
    history_rt.historical_data_client = history
    src = source(history_rt, [active(path)])
    with caplog.at_level(logging.ERROR):
        src.reconcile()
    assert history_rt.ai_instances() == {} and history_rt.strategies == {}
    assert any(r.levelno == logging.ERROR and "AI_HISTORY_BACKFILL_FAILED" in r.getMessage() for r in caplog.records)
    history.error = None
    src.reconcile()
    assert instance_of(history_rt) is not None and len(history.requests) == 2


def test_an_unresolved_conid_fails_the_backfill(history_rt, path, caplog):
    history_rt.historical_data_client = FakeHistoryClient()
    history_rt._trader_gateway = SimpleNamespace(resolve_instrument=lambda conid: None)
    source(history_rt, [active(path)]).reconcile()
    assert history_rt.ai_instances() == {}
    assert "AI_HISTORY_BACKFILL_FAILED" in caplog.text


def test_no_history_client_yet_fails_the_backfill(history_rt, path, caplog):
    source(history_rt, [active(path)]).reconcile()
    assert history_rt.ai_instances() == {}
    assert "AI_HISTORY_BACKFILL_FAILED" in caplog.text


def test_config_strategies_keep_the_lenient_startup_history_step(history_rt, path, loop_thread):
    history = FakeHistoryClient(error=IBNoDataError("error_code: 162, error_string: HMDS query returned no data"))
    history_rt.historical_data_client = history
    history_rt.load_strategy(name="plain", bar_size_str="1 min", conids=[CONID], universe=None,
                             historical_days_prior=1, module=path, class_name="VwapReclaimCat", description="x")
    assert history.requests == []
    loop_thread.run(history_rt.get_historical_data())
    assert history.requests and history_rt.get_strategy("plain") is not None


def test_the_startup_history_step_stores_a_config_strategy_bars(history_rt, path, loop_thread):
    """resolve_instrument returns a StrategyInstrument, which the tick store used to refuse on write."""
    history_rt.historical_data_client = FakeHistoryClient()
    history_rt.load_strategy(name="plain", bar_size_str="1 min", conids=[CONID], universe=None,
                             historical_days_prior=1, module=path, class_name="VwapReclaimCat", description="x")
    loop_thread.run(history_rt.get_historical_data())
    frame = history_rt._strategy_frame(CONID, ONE_MIN)
    backfilled = history_rt.historical_data_client.bars.index.tz_convert("UTC")
    assert frame is not None and set(backfilled) <= set(frame.index)


def test_the_runtime_reconcile_feeds_the_instance_only_after_its_history(history_rt, path):
    """While its history loads, the instance is in no dispatch bucket, so no bar can reach it."""
    dispatchable_during_history = []

    class OrderCheckingHistory(FakeHistoryClient):
        async def get_contract_history(self, **request):
            dispatchable_during_history.append(any(history_rt.strategies.values()))
            return await super().get_contract_history(**request)

    history_rt.historical_data_client = OrderCheckingHistory()
    history_rt._ai_deployment_source = source(history_rt, [active(path)])
    history_rt._trader_gateway.publish_instrument = lambda conid, delayed: None
    history_rt._config_mtime = 0.0
    history_rt.strategy_config_file = os.path.join(os.path.dirname(path), "missing.yaml")
    history_rt._revisions = None
    history_rt._drain_ack_outbox = lambda: None
    history_rt._reconcile_sync()
    assert dispatchable_during_history and not any(dispatchable_during_history)
    assert history_rt.strategies[CONID] == [instance_of(history_rt)]


def wire_runtime_reconcile(rt, path, deployments):
    rt._ai_deployment_source = source(rt, deployments)
    rt._trader_gateway.publish_instrument = lambda conid, delayed: None
    rt._config_mtime = 0.0
    rt.strategy_config_file = os.path.join(os.path.dirname(path), "missing.yaml")
    rt._revisions = None
    rt._drain_ack_outbox = lambda: None


def bar_recording_strategy(rt, record):
    """A strategy file whose on_prices appends to ``record``, so a bar reaching it leaves a trace."""
    path = os.path.join(rt.strategies_directory, "vwap_reclaim_cat.py")
    with open(path, "w") as f:
        f.write("from trader.trading.strategy import Strategy\n\n"
                "class VwapReclaimCat(Strategy):\n"
                "    def on_prices(self, prices):\n"
                f"        open({str(record)!r}, 'a').write('bar\\n')\n"
                "        return None\n")
    return path


class HeldHistoryClient(FakeHistoryClient):
    """Holds every request until ``release`` is set, so a second reconcile can run meanwhile."""
    def __init__(self):
        super().__init__()
        self.entered, self.requested, self.release = 0, threading.Event(), threading.Event()

    async def get_contract_history(self, **request):
        self.entered += 1
        self.requested.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return await super().get_contract_history(**request)


def test_an_overlapping_reconcile_neither_feeds_nor_backfills_an_instance_still_loading(history_rt, tmp_path):
    record = tmp_path / "on_prices.log"
    path = bar_recording_strategy(history_rt, record)
    history = HeldHistoryClient()
    history_rt.historical_data_client = history
    history_rt._signal_hold = history_rt._new_signal_hold()
    history_rt._hist_bars[(CONID, ONE_MIN)] = backfilled_bars().tz_convert("UTC")   # a bar would reach on_prices
    wire_runtime_reconcile(history_rt, path, [active(path)])
    reconcile_a = threading.Thread(target=history_rt._reconcile_sync)
    reconcile_a.start()
    try:
        assert history.requested.wait(5)
        history_rt._reconcile_sync()                                   # reconcile B, while A waits on IB
        history_rt.on_ticker_next(_make_ticker(conid=CONID, symbol="AAPL"))
        assert not record.exists()
        assert history_rt.strategies.get(CONID, []) == [] and history_rt.ai_instances() == {}
        assert history.entered == 1
    finally:
        history.release.set()
        reconcile_a.join(10)
    assert history_rt.strategies[CONID] == [instance_of(history_rt)]


class FailedReadBack:
    """Fails every DuckDB read made after a write, i.e. the read-back of freshly backfilled bars."""
    def __init__(self, monkeypatch):
        self.armed, self.written = True, False
        real_read, real_write = DuckDBDataStore.read, DuckDBDataStore.write

        def write(store, *args, **kwargs):
            self.written = True
            return real_write(store, *args, **kwargs)

        def read(store, *args, **kwargs):
            if self.armed and self.written:
                raise RuntimeError("IO Error: database is locked")
            return real_read(store, *args, **kwargs)

        monkeypatch.setattr(DuckDBDataStore, "write", write)
        monkeypatch.setattr(DuckDBDataStore, "read", read)


def test_a_failed_history_read_back_fails_the_load_and_the_next_reconcile_retries(
        history_rt, path, caplog, monkeypatch):
    history_rt.historical_data_client = FakeHistoryClient()
    read_back = FailedReadBack(monkeypatch)
    wire_runtime_reconcile(history_rt, path, [active(path)])
    with caplog.at_level(logging.ERROR):
        history_rt._reconcile_sync()
    assert history_rt.ai_instances() == {} and history_rt.strategies.get(CONID, []) == []
    assert any(r.levelno == logging.ERROR and "AI_HISTORY_BACKFILL_FAILED" in r.getMessage()
               and "database is locked" in r.getMessage() for r in caplog.records)
    read_back.armed = False
    history_rt._reconcile_sync()
    assert history_rt.strategies[CONID] == [instance_of(history_rt)]
