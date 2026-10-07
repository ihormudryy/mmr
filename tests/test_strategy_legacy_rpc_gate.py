"""PR #50 round 1, finding 1: the legacy strategy RPC (42005) is offline-only.

In the production posture nothing binds the dill/msgpack strategy socket, so
unsigned traffic cannot enable, disable or re-param a strategy. Only
``simulation`` plus ``unsafe_legacy_rpc`` (same gate as the trader's 42001)
builds it.
"""
import asyncio
import threading
from unittest.mock import Mock

import pytest

from tests.rpc_identity_fixtures import free_port, write_keyset
from trader.common.exceptions import TraderConnectionException
from trader.messaging.clientserver import RPCClient
from trader.messaging.strategy_service_api import StrategyServiceApi
from trader.strategy.strategy_runtime import StrategyRuntime

CONTROL_METHODS = ("enable_strategy", "disable_strategy", "update_strategy_params")


def _runtime(tmp_path, *, simulation=False, unsafe_legacy_rpc=False,
             paper_trading=True, ib_account="DU1234567"):
    keys_dir = tmp_path / "rpc"
    write_keyset(keys_dir)
    return StrategyRuntime(
        ib_server_address="127.0.0.1", ib_server_port=4002,
        strategy_runtime_ib_client_id=99,
        duckdb_path=str(tmp_path / "x.duckdb"),
        universe_library="u",
        zmq_pubsub_server_address="tcp://127.0.0.1", zmq_pubsub_server_port=free_port(),
        zmq_rpc_server_address="tcp://127.0.0.1", zmq_rpc_server_port=free_port(),
        zmq_strategy_rpc_server_address="tcp://127.0.0.1",
        zmq_strategy_rpc_server_port=free_port(),
        zmq_messagebus_server_address="tcp://127.0.0.1",
        zmq_messagebus_server_port=free_port(),
        strategies_directory=str(tmp_path),
        strategy_config_file=str(tmp_path / "strategy_runtime.yaml"),
        typed_bind_address="tcp://127.0.0.1",
        strategy_typed_command_port=free_port(),
        strategy_typed_query_port=free_port(),
        rpc_keys_dir=str(keys_dir),
        simulation=simulation,
        unsafe_legacy_rpc=unsafe_legacy_rpc,
        paper_trading=paper_trading,
        ib_account=ib_account,
    )


def _spy_on_control(runtime):
    spies = {name: Mock(name=name) for name in CONTROL_METHODS}
    for name, spy in spies.items():
        setattr(runtime, name, spy)
    return spies


class _ServingLoop:
    """Serve the runtime's control sockets on a background loop, as run() does."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.runtime._serve_control_sockets(), self.loop).result(5)
        return self

    def __exit__(self, *exc):
        async def _shutdown():
            for server in (self.runtime.typed_command_server, self.runtime.typed_query_server,
                           self.runtime.zmq_strategy_rpc_server):
                if server is not None:
                    server.close()
            cancelled = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            await asyncio.gather(*cancelled, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(_shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def _legacy_call(port, method, *args):
    client = RPCClient[StrategyServiceApi](
        zmq_server_address="tcp://127.0.0.1", zmq_server_port=port, timeout=1)
    client_loop = asyncio.new_event_loop()
    client_loop.run_until_complete(client.connect())
    try:
        return getattr(client.rpc(), method)(*args)
    finally:
        client.close()
        client_loop.close()


def test_production_posture_never_builds_the_legacy_strategy_server(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.connect()
    assert runtime.zmq_strategy_rpc_server is None


def test_unsigned_legacy_traffic_cannot_change_strategy_control_in_production(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.connect()
    spies = _spy_on_control(runtime)
    legacy_port = runtime.zmq_strategy_rpc_server_port
    typed_command_port = runtime.strategy_typed_command_port

    with _ServingLoop(runtime):
        for port in (legacy_port, typed_command_port):
            for method, args in (("enable_strategy", ("proof-only",)),
                                 ("disable_strategy", ("proof-only",)),
                                 ("update_strategy_params", ("proof-only", {"X": 1}))):
                try:
                    _legacy_call(port, method, *args)
                except Exception:
                    pass

    for name, spy in spies.items():
        spy.assert_not_called()


def test_offline_simulation_with_the_unsafe_flag_keeps_the_legacy_server(tmp_path):
    runtime = _runtime(tmp_path, simulation=True, unsafe_legacy_rpc=True)
    runtime.connect()
    assert runtime.zmq_strategy_rpc_server is not None


@pytest.mark.parametrize("simulation", [False, True])
def test_simulation_alone_or_the_flag_alone_never_builds_it(tmp_path, simulation):
    runtime = _runtime(tmp_path, simulation=simulation, unsafe_legacy_rpc=False)
    runtime.connect()
    assert runtime.zmq_strategy_rpc_server is None


def test_unsafe_flag_outside_simulation_fails_before_serving(tmp_path):
    runtime = _runtime(tmp_path, simulation=False, unsafe_legacy_rpc=True)
    with pytest.raises(TraderConnectionException):
        runtime.connect()
    assert getattr(runtime, "typed_command_server", None) is None


def test_positive_control_the_offline_legacy_server_does_reach_the_runtime(tmp_path):
    """Proves the probe above can see a reachable legacy surface."""
    runtime = _runtime(tmp_path, simulation=True, unsafe_legacy_rpc=True)
    runtime.connect()
    spies = _spy_on_control(runtime)
    spies["update_strategy_params"].return_value = {"X": 1}

    with _ServingLoop(runtime):
        _legacy_call(runtime.zmq_strategy_rpc_server_port, "update_strategy_params", "proof-only", {"X": 1})

    spies["update_strategy_params"].assert_called_once_with("proof-only", {"X": 1})


# --- PR #50 round 2: the flags alone never open 42005 on a live account ---

def _write_effective_config(tmp_path, *, trading_mode, yaml_flags):
    keys_dir = tmp_path / "rpc"
    write_keyset(keys_dir)
    config = tmp_path / "trader.yaml"
    lines = [
        f"duckdb_path: {tmp_path / 'x.duckdb'}",
        "universe_library: Universes",
        "ib_server_address: 127.0.0.1",
        "ib_paper_account: DU1234567",
        "ib_live_account: U7654321",
        "strategy_runtime_ib_client_id: 7",
        "zmq_rpc_server_address: tcp://127.0.0.1", f"zmq_rpc_server_port: {free_port()}",
        "zmq_pubsub_server_address: tcp://127.0.0.1", f"zmq_pubsub_server_port: {free_port()}",
        "zmq_strategy_rpc_server_address: tcp://127.0.0.1",
        f"zmq_strategy_rpc_server_port: {free_port()}",
        "zmq_messagebus_server_address: tcp://127.0.0.1",
        f"zmq_messagebus_server_port: {free_port()}",
        f"strategies_directory: {tmp_path}",
        f"strategy_config_file: {tmp_path / 'strategy_runtime.yaml'}",
        "typed_bind_address: tcp://127.0.0.1",
        f"strategy_typed_command_port: {free_port()}",
        f"strategy_typed_query_port: {free_port()}",
        f"rpc_keys_dir: {keys_dir}",
        f"trading_mode: {trading_mode}",
        *yaml_flags,
    ]
    config.write_text("\n".join(lines) + "\n")
    return str(config)


def _resolve_from_config(config_path):
    from trader.container import Container
    return Container(config_path).resolve(StrategyRuntime)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("SIMULATION", "UNSAFE_LEGACY_RPC", "PAPER_TRADING", "IB_ACCOUNT",
                 "TRADING_MODE", "TRADER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("source", ["yaml", "env"])
def test_config_forced_simulation_and_unsafe_flag_on_a_live_account_refuse_before_binding(
        tmp_path, clean_env, source):
    flags = ["simulation: true", "unsafe_legacy_rpc: true"] if source == "yaml" else []
    if source == "env":
        clean_env.setenv("SIMULATION", "true")
        clean_env.setenv("UNSAFE_LEGACY_RPC", "true")
    runtime = _resolve_from_config(_write_effective_config(tmp_path, trading_mode="live", yaml_flags=flags))
    assert runtime.simulation is True and runtime.unsafe_legacy_rpc is True
    assert runtime.paper_trading is False and runtime.ib_account == "U7654321"

    with pytest.raises(TraderConnectionException) as raised:
        runtime.connect()
    assert "paper" in str(raised.value.__cause__ or raised.value)
    assert runtime.zmq_strategy_rpc_server is None
    assert getattr(runtime, "typed_command_server", None) is None


def test_env_paper_flag_cannot_relabel_a_live_account(tmp_path, clean_env):
    clean_env.setenv("SIMULATION", "true")
    clean_env.setenv("UNSAFE_LEGACY_RPC", "true")
    clean_env.setenv("PAPER_TRADING", "true")
    runtime = _resolve_from_config(_write_effective_config(tmp_path, trading_mode="live", yaml_flags=[]))
    assert runtime.paper_trading is True and runtime.ib_account == "U7654321"
    with pytest.raises(TraderConnectionException):
        runtime.connect()
    assert runtime.zmq_strategy_rpc_server is None


def test_config_forced_flags_on_a_paper_account_still_allow_offline_compat(tmp_path, clean_env):
    runtime = _resolve_from_config(_write_effective_config(
        tmp_path, trading_mode="paper", yaml_flags=["simulation: true", "unsafe_legacy_rpc: true"]))
    runtime.connect()
    assert runtime.zmq_strategy_rpc_server is not None


@pytest.mark.parametrize("paper_trading,ib_account", [
    (False, "DU1234567"), (True, "U7654321"), (True, ""), (False, "U7654321")])
def test_legacy_server_needs_a_proven_paper_account(tmp_path, paper_trading, ib_account):
    runtime = _runtime(tmp_path, simulation=True, unsafe_legacy_rpc=True,
                       paper_trading=paper_trading, ib_account=ib_account)
    with pytest.raises(TraderConnectionException):
        runtime.connect()
    assert runtime.zmq_strategy_rpc_server is None
