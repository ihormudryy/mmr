"""Issue #198: the trader notices an IB Gateway that stays connected but answers nothing."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from trader.trading import trading_runtime
from trader.trading.trading_runtime import Trader


class _Ib:
    def __init__(self):
        self.silent = False
        self.connected = True

    def isConnected(self):
        return self.connected

    def reqCurrentTimeAsync(self):
        future = asyncio.get_running_loop().create_future()
        if not self.silent:
            future.set_result(1_700_000_000)
        return future


def _trader():
    ib = _Ib()
    trader = SimpleNamespace(client=SimpleNamespace(ib=ib), data=object(), _ib_upstream_connected=True,
                             _ib_upstream_error="", _ib_responding=True, _ib_probe_misses=0, _ib_probe_error="")
    return trader, ib


async def _probe(trader):
    return await Trader.probe_ib(trader)


@pytest.mark.asyncio
async def test_two_missed_probes_mark_ib_as_not_responding_and_status_says_why(monkeypatch):
    monkeypatch.setattr(trading_runtime, "IB_PROBE_TIMEOUT_SECONDS", 0.05)
    trader, ib = _trader()
    ib.silent = True
    assert await _probe(trader) is True                      # one miss is not enough: IB may be busy
    assert await _probe(trader) is False
    status = Trader.status(SimpleNamespace(**vars(trader), _status_cache_ts=0.0))
    assert status["ib_responding"] is False and status["ib_upstream_connected"] is False
    assert "./docker.sh -r" in status["ib_upstream_error"]


@pytest.mark.asyncio
async def test_an_answer_restores_ib_responding_at_once(monkeypatch):
    monkeypatch.setattr(trading_runtime, "IB_PROBE_TIMEOUT_SECONDS", 0.05)
    trader, ib = _trader()
    ib.silent = True
    await _probe(trader)
    await _probe(trader)
    ib.silent = False
    assert await _probe(trader) is True
    assert trader._ib_probe_misses == 0
    assert Trader.status(SimpleNamespace(**vars(trader), _status_cache_ts=0.0))["ib_upstream_connected"] is True


@pytest.mark.asyncio
async def test_a_disconnected_client_is_not_probed():
    trader, ib = _trader()
    ib.connected = False
    ib.silent = True
    assert await _probe(trader) is True                      # disconnects have their own path (1100/1102)
    assert trader._ib_probe_misses == 0


def test_the_readiness_gate_treats_a_silent_ib_as_not_connected():
    from trader.trading.command_stack import _ib_ready
    assert _ib_ready(SimpleNamespace(is_ib_connected=lambda: True, ib_responding=True)) is True
    assert _ib_ready(SimpleNamespace(is_ib_connected=lambda: True, ib_responding=False)) is False
    assert _ib_ready(SimpleNamespace(is_ib_connected=lambda: False, ib_responding=True)) is False


def test_the_trader_starts_the_probe_loop():
    from trader import trader_service
    started = []

    class Loop:
        def create_task(self, coro):
            started.append(coro)
            coro.close()

    trader_service._maybe_start_ib_probe(SimpleNamespace(probe_ib=lambda: None), Loop())
    assert len(started) == 1
