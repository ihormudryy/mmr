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


def _runtime(tmp_path, *, simulation=False, unsafe_legacy_rpc=False):
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
        def _close():
            for server in (self.runtime.typed_command_server, self.runtime.typed_query_server,
                           self.runtime.zmq_strategy_rpc_server):
                if server is not None:
                    server.close()
            self.loop.stop()
        self.loop.call_soon_threadsafe(_close)
        self.thread.join(5)


def _legacy_call(port, method, *args):
    client = RPCClient[StrategyServiceApi](
        zmq_server_address="tcp://127.0.0.1", zmq_server_port=port, timeout=1)
    asyncio.new_event_loop().run_until_complete(client.connect())
    try:
        return getattr(client.rpc(), method)(*args)
    finally:
        client.close()


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
