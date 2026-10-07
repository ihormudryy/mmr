"""SP1 Plan 5 Task 10: the scoreboard recovery and loop in trader_service."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from tests.test_trader_service_loops import _close_loop
from trader import trader_service


class _Scoreboard:
    def __init__(self, *, fail_recover=False, fail_ticks=0):
        self.log = []
        self.fail_recover = fail_recover
        self.fail_ticks = fail_ticks
        self.ticks = 0
        self.threads = set()

    def recover(self):
        self.log.append("recover")
        if self.fail_recover:
            raise RuntimeError("journal down")

    def tick(self):
        self.threads.add(threading.current_thread().name)
        self.ticks += 1
        self.log.append("tick")
        if self.ticks <= self.fail_ticks:
            raise RuntimeError("tick failed")


def _fast_loop(monkeypatch):
    monkeypatch.setattr(trader_service, "_scoreboard_loop",
                        lambda scoreboard: trader_service._watched_ticks(
                            "scoreboard", lambda: asyncio.to_thread(scoreboard.tick), interval=0.01,
                            stuck_after=5.0))


def test_recover_runs_before_the_first_tick_off_the_liquidation_worker(monkeypatch):
    _fast_loop(monkeypatch)
    loop = asyncio.new_event_loop()
    try:
        scoreboard = _Scoreboard()
        trader_service._maybe_start_scoreboard(SimpleNamespace(scoreboard=scoreboard), loop)
        loop.run_until_complete(asyncio.sleep(0.1))
        assert scoreboard.log[0] == "recover" and scoreboard.ticks >= 1
        assert not any(name.startswith("liquidation-worker") for name in scoreboard.threads)
    finally:
        _close_loop(loop)


def test_a_failing_recover_and_tick_never_stop_the_loop(monkeypatch):
    _fast_loop(monkeypatch)
    loop = asyncio.new_event_loop()
    try:
        scoreboard = _Scoreboard(fail_recover=True, fail_ticks=2)
        trader_service._maybe_start_scoreboard(SimpleNamespace(scoreboard=scoreboard), loop)
        loop.run_until_complete(asyncio.sleep(0.2))
        assert scoreboard.ticks >= 3
    finally:
        _close_loop(loop)


def test_no_loop_while_stopping_or_without_a_scoreboard():
    loop = asyncio.new_event_loop()
    try:
        scoreboard = _Scoreboard()
        trader_service._maybe_start_scoreboard(SimpleNamespace(scoreboard=scoreboard), loop, stopping=lambda: True)
        trader_service._maybe_start_scoreboard(SimpleNamespace(), loop)
        assert scoreboard.log == [] and asyncio.all_tasks(loop) == set()
    finally:
        _close_loop(loop)
