"""Fixtures for the SP1 acceptance tests (Plan 6): the composed stack served over typed RPC."""
from __future__ import annotations

import pytest

from tests.sp1_fixtures import Composed, LoopThread, et, served_stack


@pytest.fixture
def loop_thread():
    lt = LoopThread()
    yield lt
    lt.stop()


@pytest.fixture
def composed_sim(tmp_path, loop_thread):
    """Plan 1's composed stack; the test drives its BrokerSim directly."""
    stack = Composed(tmp_path, loop_thread, [et(11, 0)])
    yield stack.sim
    stack.liquidation.worker.shutdown()


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    """Paper, ai_paper enabled, an ARMED experiment started by cli over RPC, Telegram off."""
    stack = served_stack(tmp_path, loop_thread, monkeypatch, acceptance_probe=True)
    yield stack
    stack.close()
