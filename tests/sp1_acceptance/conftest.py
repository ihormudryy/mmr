"""Fixtures for the SP1 acceptance tests (Plan 6): the composed stack served over typed RPC."""
from __future__ import annotations

import pytest

from tests.sp1_acceptance.judged import judged_served_stack
from tests.sp1_fixtures import Composed, LoopThread, et


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
    """Paper, ai_paper enabled, an ARMED experiment started by cli over RPC, Telegram off, a judged version."""
    stack = judged_served_stack(tmp_path, loop_thread, monkeypatch, acceptance_probe=True)
    yield stack
    stack.close()


@pytest.fixture
def fake_port():
    from tests.sp1_acceptance.fakes import FakePort
    return FakePort()


@pytest.fixture
def settings():
    from trader.acceptance.scenario import AcceptanceSettings
    return AcceptanceSettings(run_id="acc-20260717-abcdef", account_id="DU111111",
                              strategy_bytes=b"class OpeningRangeBreakout: pass\n",
                              deployment_version="sha256:" + "f" * 64)


@pytest.fixture
def journal(tmp_path, settings):
    from trader.acceptance.journal import RunJournal
    return RunJournal(tmp_path / settings.run_id)
