"""SP1 Plan 4 Task 7: the kill monitor loop in trader_service."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from tests.test_trader_service_loops import _close_loop, worker  # noqa: F401  (fixture)
from trader import trader_service


class _Monitor:
    def __init__(self, log, *, fail_recover=False, fail_ticks=0):
        self.log = log
        self.fail_recover = fail_recover
        self.fail_ticks = fail_ticks
        self.ticks = 0
        self.threads = set()

    def recover(self):
        self.threads.add(threading.current_thread().name)
        self.log.append("experiment_recover")
        if self.fail_recover:
            raise RuntimeError("journal down")

    def tick(self):
        self.threads.add(threading.current_thread().name)
        self.ticks += 1
        if self.ticks <= self.fail_ticks:
            raise RuntimeError("broker down")


def test_monitor_recovers_before_readiness_and_after_session_recovery(monkeypatch):
    order_log = []

    class _StartupTrader:
        def __init__(self):
            self.liquidation_service = SimpleNamespace(rescan=lambda: order_log.append("liquidation_rescan"))
            self.session_controller = SimpleNamespace(
                recover=lambda now: order_log.append("session_recover") or SimpleNamespace(
                    state="FLAT", session_date=None, incident=None),
                run_due=lambda now: SimpleNamespace(state="FLAT", session_date=None, entry_cutoff_reached=True))
            self.kill_line_monitor = _Monitor(order_log)

        def connect(self):
            pass

        def run(self):
            order_log.append("trader_run")

        async def shutdown(self):
            pass

    fake = _StartupTrader()
    from trader.automation.ai_paper_config import AiPaperConfig
    monkeypatch.setattr(trader_service, "Container", SimpleNamespace(
        create=lambda _config: SimpleNamespace(
            resolve=lambda *_a, **_k: fake,
            typed_config=lambda: SimpleNamespace(ai_paper=AiPaperConfig()))))
    monkeypatch.setattr(trader_service, "_seed_trading_control", lambda *_a: None)
    monkeypatch.setattr(trader_service, "_maybe_start_command_reconciliation", lambda *_a: None)
    monkeypatch.setattr(trader_service, "get_network_ip", lambda: "127.0.0.1")
    try:
        trader_service.main.callback(simulation=False, debug=False, config="unused.yaml")
    finally:
        import signal
        loop = asyncio.get_event_loop()
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        _close_loop(loop)
        asyncio.set_event_loop(None)
    assert order_log.index("liquidation_rescan") < order_log.index("session_recover") \
        < order_log.index("experiment_recover") < order_log.index("trader_run")


def test_monitor_ticks_on_the_liquidation_worker(worker):  # noqa: F811
    loop = asyncio.new_event_loop()
    try:
        monitor = _Monitor([])
        trader_service._maybe_start_experiment_monitor(SimpleNamespace(kill_line_monitor=monitor), loop, worker)
        loop.run_until_complete(asyncio.sleep(0.1))
        assert monitor.ticks >= 1
        assert monitor.threads == {name for name in monitor.threads if name.startswith("liquidation-worker")}
    finally:
        _close_loop(loop)


def test_monitor_loop_not_started_while_stopping(worker):  # noqa: F811
    loop = asyncio.new_event_loop()
    try:
        monitor = _Monitor([])
        trader_service._maybe_start_experiment_monitor(
            SimpleNamespace(kill_line_monitor=monitor), loop, worker, stopping=lambda: True)
        assert monitor.log == [] and asyncio.all_tasks(loop) == set()
    finally:
        _close_loop(loop)


def test_monitor_loop_survives_a_failing_recover_and_tick(worker, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trader_service, "_experiment_monitor_loop",
                        lambda monitor, worker: trader_service._watched_ticks(
                            "experiment monitor", lambda: trader_service._on_worker(worker, monitor.tick),
                            interval=0.01, stuck_after=5.0))
    loop = asyncio.new_event_loop()
    try:
        monitor = _Monitor([], fail_recover=True, fail_ticks=2)
        trader_service._maybe_start_experiment_monitor(SimpleNamespace(kill_line_monitor=monitor), loop, worker)
        loop.run_until_complete(asyncio.sleep(0.2))
        assert monitor.ticks >= 3
    finally:
        _close_loop(loop)


def test_no_monitor_no_loop(worker):  # noqa: F811
    loop = asyncio.new_event_loop()
    try:
        trader_service._maybe_start_experiment_monitor(SimpleNamespace(), loop, worker)
        assert asyncio.all_tasks(loop) == set()
    finally:
        _close_loop(loop)


def test_a_failed_startup_recovery_is_retried_by_the_next_tick(tmp_path):
    from tests.automation.test_kill_monitor import World
    from tests.automation.experiment_fixtures import armed_record
    w = World(tmp_path, record=armed_record(), recover=False)
    assert w.monitor.entry_block(w.store.active()) == "EXPERIMENT_MONITOR_NOT_READY"
    w.monitor.tick()
    w.monitor.tick()
    assert w.monitor.entry_block(w.store.active()) is None
