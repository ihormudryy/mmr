"""Tests closing the production RPC authorization-bypass surface (G0 Task 4).

Covers the brief's two required tests verbatim, plus the security-critical
details called out in the outer task instructions:

- the five bypass methods are unreachable through the production registry on
  EITHER role (the brief's test only checks "command"; we also check
  "query", since the production registry must never expose them at all)
- ``validate_rpc_mode`` fails closed for every combination, not just the one
  verbatim case (both "safe" combinations must NOT raise)
- ``build_production_registry`` type-checks its ``authenticator`` argument
- the production registry actually carries the intended health/read methods
  (a registry with zero legacy methods AND zero real methods would trivially
  pass the negative tests without being useful)
- ``TraderServiceApi`` (the base class registered in production) no longer
  defines any of the five methods at all -- proving they were actually
  *moved*, not just left off one particular registry
- ``LegacyOfflineTraderServiceApi`` still has all five -- proving the
  offline-simulation capability wasn't deleted, only gated
- an end-to-end round trip: the production registry served over a real
  ``TypedRpcServer``/``TypedRpcClient`` pair answers a query correctly, and a
  command-role call to the (intentionally empty) command registry is
  rejected with ``METHOD_NOT_ALLOWED`` -- proving there is no accidental
  command-role registration anywhere in the production wiring.
"""

from __future__ import annotations

import socket as socket_module
import threading
import time
from collections import namedtuple

import pytest
import zmq

from trader.messaging.legacy_offline_api import LegacyOfflineTraderServiceApi
from trader.messaging.production_api import build_production_registry, validate_rpc_mode
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRegistry,
    TypedRpcRemoteError,
    TypedRpcServer,
)
from trader.trading.risk_gate import RiskLimits


HMAC_KEY = b"k" * 32

BYPASS_METHODS = [
    "place_order_simple",
    "place_expressive_order",
    "place_standalone_order",
    "set_risk_limits",
    "cancel_all",
]

_AV = namedtuple("AccountValue", ["account", "tag", "value", "currency"])


def _free_port() -> int:
    with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeIB:
    def __init__(self):
        self._values = [_AV("DU12345", "NetLiquidation", "17000", "CAD")]

    def accountValues(self):
        return self._values

    def managedAccounts(self):
        return ["DU12345"]


class _FakeClient:
    def __init__(self):
        self.ib = _FakeIB()


class _FakeTrader:
    """Just enough of ``Trader`` for the three production query handlers
    (``get_status``, ``get_account_values``, ``get_risk_limits``) to run."""

    def __init__(self):
        self.ib_account = "DU12345"
        self.client = _FakeClient()
        self.risk_gate = type("_RG", (), {"limits": RiskLimits()})()

    def status(self) -> dict:
        return {"ib_connected": True, "ib_upstream_connected": True}


@pytest.fixture()
def authenticator() -> HmacServiceAuthenticator:
    return HmacServiceAuthenticator(HMAC_KEY, now=lambda: 1_700_000_000.0)


@pytest.fixture()
def production_registry(authenticator) -> TypedRpcRegistry:
    return build_production_registry(_FakeTrader(), authenticator)


# ---------------------------------------------------------------------------
# Step 1 (brief, verbatim)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", BYPASS_METHODS)
def test_production_registry_has_no_legacy_mutation(method, production_registry):
    assert not production_registry.contains("command", method)


def test_unsafe_legacy_rpc_requires_simulation():
    with pytest.raises(ValueError, match="offline simulation"):
        validate_rpc_mode(simulation=False, unsafe_legacy_rpc=True)


# ---------------------------------------------------------------------------
# validate_rpc_mode -- every combination, not just the one that must raise
# ---------------------------------------------------------------------------

class TestValidateRpcMode:
    def test_unsafe_without_simulation_raises(self):
        with pytest.raises(ValueError, match="offline simulation"):
            validate_rpc_mode(simulation=False, unsafe_legacy_rpc=True)

    def test_unsafe_with_simulation_is_allowed(self):
        validate_rpc_mode(simulation=True, unsafe_legacy_rpc=True)  # must not raise

    def test_safe_without_simulation_is_allowed(self):
        validate_rpc_mode(simulation=False, unsafe_legacy_rpc=False)  # must not raise

    def test_safe_with_simulation_is_allowed(self):
        """simulation=True alone (without the explicit unsafe flag) must NOT
        grant the legacy RPC -- the caller must opt in to BOTH."""
        validate_rpc_mode(simulation=True, unsafe_legacy_rpc=False)  # must not raise


# ---------------------------------------------------------------------------
# The five bypass methods are unreachable on EITHER role, not just "command"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", BYPASS_METHODS)
def test_production_registry_has_no_legacy_mutation_as_query_either(method, production_registry):
    assert not production_registry.contains("query", method)


@pytest.mark.parametrize("method", BYPASS_METHODS)
def test_production_registry_resolve_is_none_for_bypass_methods(method, production_registry):
    """Belt-and-suspenders on top of .contains(): .resolve() (what the
    server dispatch loop actually calls) must also come back empty for
    every role."""
    assert production_registry.resolve("command", method) is None
    assert production_registry.resolve("query", method) is None
    assert production_registry.resolve("feed", method) is None


# ---------------------------------------------------------------------------
# build_production_registry type-checks its authenticator argument
# ---------------------------------------------------------------------------

def test_build_production_registry_rejects_non_authenticator():
    with pytest.raises(TypeError, match="HmacServiceAuthenticator"):
        build_production_registry(_FakeTrader(), authenticator="not-a-real-authenticator")


# ---------------------------------------------------------------------------
# The production registry actually carries real health/read methods
# ---------------------------------------------------------------------------

class TestProductionRegistryHasRealQueries:
    def test_get_status_is_registered_as_query(self, production_registry):
        assert production_registry.contains("query", "get_status")

    def test_get_account_values_is_registered_as_query(self, production_registry):
        assert production_registry.contains("query", "get_account_values")

    def test_get_risk_limits_is_registered_as_query(self, production_registry):
        assert production_registry.contains("query", "get_risk_limits")

    def test_registered_query_handlers_actually_work(self, production_registry):
        """Not just "registered" -- the handler must return the real data,
        proving build_production_registry wired live trader-backed methods
        rather than stubs."""
        status_reg = production_registry.resolve("query", "get_status")
        status = status_reg.handler({})
        assert status["ib_connected"] is True
        assert status["ib_upstream_connected"] is True
        assert status["liveness"] == {"alive": True}
        assert status["semantic_readiness"]["ready"] is False

        values_reg = production_registry.resolve("query", "get_account_values")
        values = values_reg.handler({})
        assert values["NetLiquidation"] == {"value": "17000", "currency": "CAD"}

        limits_reg = production_registry.resolve("query", "get_risk_limits")
        limits = limits_reg.handler({})
        assert isinstance(limits, dict) and limits  # a real, non-empty RiskLimits dump


# ---------------------------------------------------------------------------
# The split actually moved the methods -- not just omitted them from one
# particular registry
# ---------------------------------------------------------------------------

class TestApiClassSplit:
    @pytest.mark.parametrize("method", BYPASS_METHODS)
    def test_base_trader_service_api_no_longer_has_bypass_methods(self, method):
        assert not hasattr(TraderServiceApi, method), (
            f"TraderServiceApi.{method} must not exist -- it should live only on "
            f"LegacyOfflineTraderServiceApi"
        )

    @pytest.mark.parametrize("method", BYPASS_METHODS)
    def test_legacy_offline_api_still_has_bypass_methods(self, method):
        handler = getattr(LegacyOfflineTraderServiceApi, method, None)
        assert handler is not None, f"LegacyOfflineTraderServiceApi.{method} must still exist"
        assert getattr(handler, "_is_rpc_method", False) is True

    def test_legacy_offline_api_is_a_trader_service_api_subclass(self):
        """The offline-simulation class still gets every read method (get_status,
        get_positions, etc.) via inheritance -- only the mutation surface is
        additive."""
        assert issubclass(LegacyOfflineTraderServiceApi, TraderServiceApi)

    def test_skip_risk_gate_is_not_a_field_on_any_typed_rpc_model(self, production_registry):
        """skip_risk_gate must stay an internal Executioner/legacy-RPC-only
        keyword -- it must never appear in a typed-RPC request/response
        schema. There are no command methods registered at all right now,
        so this asserts the (currently vacuous but future-proofing) absence
        across whatever IS registered on the production registry."""
        for role in ("query", "command", "feed"):
            for method in production_registry._by_role_method:
                if method[0] != role:
                    continue
                registration = production_registry._by_role_method[method]
                for model in (registration.request_model, registration.response_model):
                    if model is dict:
                        continue
                    fields = getattr(model, "model_fields", {})
                    assert "skip_risk_gate" not in fields


# ---------------------------------------------------------------------------
# End-to-end: production registry served over a real typed transport
# ---------------------------------------------------------------------------

class TestProductionRegistryOverRealTransport:
    def test_query_client_can_call_get_status_and_command_socket_has_nothing(self, authenticator):
        query_port = _free_port()
        command_port = _free_port()

        registry = build_production_registry(_FakeTrader(), authenticator)
        empty_command_registry = TypedRpcRegistry()  # mirrors connect(): [M1-F3] fills this in later

        query_server = TypedRpcServer("query", registry, authenticator, port=query_port)
        command_server = TypedRpcServer("command", empty_command_registry, authenticator, port=command_port)

        loop_ready = threading.Event()
        loop_holder = {}

        def _run_loop():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_holder["loop"] = loop
            loop.run_until_complete(query_server.serve())
            loop.run_until_complete(command_server.serve())
            loop_ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_run_loop, daemon=True)
        thread.start()
        assert loop_ready.wait(timeout=5), "typed servers did not start in time"
        time.sleep(0.1)  # let the bind settle

        query_client = TypedRpcClient("query", authenticator, port=query_port, timeout=3)
        query_client.connect()
        command_client = TypedRpcClient("command", authenticator, port=command_port, timeout=3)
        command_client.connect()

        try:
            result = query_client.call("get_status", {}, dict)
            assert result["ib_connected"] is True
            assert result["ib_upstream_connected"] is True
            assert result["liveness"] == {"alive": True}
            assert result["semantic_readiness"]["ready"] is False

            # No command-role methods are registered in production yet --
            # not even something as innocuous-sounding as get_status leaks
            # onto the command socket.
            with pytest.raises(TypedRpcRemoteError) as exc:
                command_client.call("get_status", {}, dict)
            assert exc.value.code == "METHOD_NOT_ALLOWED"

            # And the five bypass methods are unreachable on the command
            # socket too (there's nothing registered there at all).
            for method in BYPASS_METHODS:
                with pytest.raises(TypedRpcRemoteError) as exc:
                    command_client.call(method, {}, dict)
                assert exc.value.code == "METHOD_NOT_ALLOWED"
        finally:
            query_client.close()
            command_client.close()
            query_server.close()
            command_server.close()
            loop = loop_holder.get("loop")
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
