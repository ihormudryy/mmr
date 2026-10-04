"""Exercise the external LLMVM helper without a model SDK or live services."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def loop_engine(monkeypatch):
    # Only the external runtime boundary is stubbed; load the real helper fresh
    # per test so its mutable class config/state cannot leak between tests.
    llmvm = ModuleType("llmvm_lite")
    llm = ModuleType("llmvm_lite.llm")
    runtime = ModuleType("llmvm_lite.llmvm_runtime")

    class User(str):
        pass

    class HookResult:
        def __init__(self, inject=None, continue_loop=False):
            self.inject = inject or []
            self.continue_loop = continue_loop

    setattr(llm, "User", User)
    setattr(runtime, "HookResult", HookResult)
    for name, module in (
        ("llmvm_lite", llmvm),
        ("llmvm_lite.llm", llm),
        ("llmvm_lite.llmvm_runtime", runtime),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).resolve().parents[1] / "skills/mmr-loop-skill/scripts/loop_engine.py"
    spec = importlib.util.spec_from_file_location("isolated_llm_trading_loop", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "register_hook", Mock())
    setattr(module, "unregister_hook", Mock())
    setattr(module, "time", SimpleNamespace(time=lambda: 1000.0))
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["class", "wrapper"])
@pytest.mark.parametrize("enabled", [True, 1, "true", "false"])
async def test_start_rejects_auto_approval_before_registering_hooks(loop_engine, entry_point, enabled):
    loop = loop_engine.TradingLoop
    with pytest.raises(ValueError, match="auto_approve.*proposal-only"):
        if entry_point == "class":
            loop.config["auto_approve"] = enabled
            await loop.start()
        else:
            await loop_engine.start_trading_loop(auto_approve=enabled)

    assert loop._state["running"] is False
    assert loop._state["phase"] == "IDLE"
    assert loop._state["cycle"] == 0
    loop_engine.register_hook.assert_not_called()


@pytest.mark.asyncio
async def test_wrapper_rejects_auto_approval_when_config_key_was_removed(loop_engine):
    loop = loop_engine.TradingLoop
    del loop.config["auto_approve"]

    with pytest.raises(ValueError, match="auto_approve.*proposal-only"):
        await loop_engine.start_trading_loop(auto_approve=True)

    assert loop._state["running"] is False
    loop_engine.register_hook.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["cycle", "user", "monitor", "sleep", "class", "wrapper"])
async def test_enabling_auto_approval_stops_running_loop(loop_engine, entry_point):
    loop = loop_engine.TradingLoop
    await loop.start()
    ctx = SimpleNamespace(messages=[])
    loop_engine.asyncio = SimpleNamespace(sleep=AsyncMock())
    loop_engine.MMRHelpers = SimpleNamespace(snapshots_batch=AsyncMock(return_value={"data": []}))
    if entry_point == "user":
        ctx.messages.append(loop_engine.User("Check my portfolio"))
    if entry_point in {"monitor", "sleep"}:
        loop._state["last_cycle_time"] = 1000.0
    if entry_point == "monitor":
        await loop.track_position("AAPL", "LONG", 100, 1)

    loop.config["auto_approve"] = True
    with pytest.raises(ValueError, match="auto_approve.*proposal-only"):
        if entry_point == "class":
            await loop.start()
        elif entry_point == "wrapper":
            await loop_engine.start_trading_loop(auto_approve=True)
        else:
            await loop._loop_hook(ctx)

    assert (await loop.status())["running"] is False
    assert loop._state["phase"] == "STOPPED"
    assert loop._state["cycle"] == 0
    loop_engine.unregister_hook.assert_called_once_with("mmr_trading_loop")
    loop_engine.register_hook.assert_called_once()
    loop_engine.asyncio.sleep.assert_not_awaited()
    loop_engine.MMRHelpers.snapshots_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for", ["snapshot", "sleep"])
async def test_auto_approval_enabled_during_await_stops_before_resuming(loop_engine, wait_for):
    loop = loop_engine.TradingLoop
    await loop.start()
    loop._state["last_cycle_time"] = 1000.0

    async def enable_auto_approval(*args, **kwargs):
        loop.config["auto_approve"] = True
        return {"data": [{"symbol": "AAPL", "last": 90}]}

    loop_engine.asyncio = SimpleNamespace(sleep=AsyncMock(side_effect=enable_auto_approval))
    loop_engine.MMRHelpers = SimpleNamespace(
        snapshots_batch=AsyncMock(side_effect=enable_auto_approval),
    )
    if wait_for == "snapshot":
        await loop.track_position("AAPL", "LONG", 100, 1)

    with pytest.raises(ValueError, match="auto_approve.*proposal-only"):
        await loop._loop_hook(SimpleNamespace(messages=[]))

    assert loop._state["running"] is False
    assert loop._state["phase"] == "STOPPED"
    assert loop._state["cycle"] == 0
    loop_engine.unregister_hook.assert_called_once_with("mmr_trading_loop")
    if wait_for == "snapshot":
        loop_engine.MMRHelpers.snapshots_batch.assert_awaited_once()
        loop_engine.asyncio.sleep.assert_not_awaited()
    else:
        loop_engine.asyncio.sleep.assert_awaited_once()
        loop_engine.MMRHelpers.snapshots_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["default", "explicit_false", "missing"])
async def test_disabled_auto_approval_preserves_proposal_only_cycle(loop_engine, configuration):
    loop = loop_engine.TradingLoop
    if configuration == "missing":
        del loop.config["auto_approve"]
    if configuration == "explicit_false":
        await loop_engine.start_trading_loop(auto_approve=False, scan_interval_seconds=300)
        assert loop.config["scan_interval_seconds"] == 300
    else:
        await loop.start()

    # The hook still schedules proposal instructions, never approval/execution.
    result = await loop._loop_hook(SimpleNamespace(messages=[]))
    assert result.continue_loop is True
    assert len(result.inject) == 1
    assert "await MMRHelpers.propose(" in str(result.inject[0])
    for forbidden in ("MMRHelpers.approve(", "MMRHelpers.buy(", "MMRHelpers.sell("):
        assert forbidden not in str(result.inject[0])
    assert (await loop_engine.trading_loop_status())["cycle"] == 1
    loop_engine.register_hook.assert_called_once_with(
        "mmr_trading_loop", loop._loop_hook,
        "MMR trading loop — schedules cycles, sleeps between them",
        triggers={"pre_llm_call", "on_complete"},
    )

    await loop_engine.stop_trading_loop()
    stopped = await loop._loop_hook(SimpleNamespace(messages=[]))
    assert stopped.inject == []
    assert stopped.continue_loop is False
    assert (await loop_engine.trading_loop_status())["running"] is False


@pytest.mark.asyncio
async def test_invalid_config_remains_stopped_if_runtime_cannot_unregister(loop_engine):
    loop = loop_engine.TradingLoop
    await loop.start()
    loop_engine.unregister_hook.side_effect = RuntimeError("runtime teardown")
    loop.config["auto_approve"] = True

    with pytest.raises(ValueError, match="auto_approve.*proposal-only"):
        await loop._loop_hook(SimpleNamespace(messages=[]))

    loop.config["auto_approve"] = False
    result = await loop._loop_hook(SimpleNamespace(messages=[]))
    assert result.continue_loop is False
    assert result.inject == []
    assert loop._state["running"] is False

    # Repairing config alone does not silently resume the stopped loop.
    await loop.start()
    assert loop._state["running"] is True
