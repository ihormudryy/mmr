"""Unit tests for typed /manage RPC surface on trader + strategy services."""
from __future__ import annotations

from types import SimpleNamespace

from trader.messaging.manage_surface import register_manage_surface
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import TypedRpcRegistry


class _StubTrader:
    def __init__(self):
        self.universe_accessor = SimpleNamespace(
            list_universes_count=lambda: {'alpha': 2},
            get=lambda name: SimpleNamespace(
                security_definitions=[
                    SimpleNamespace(symbol='AAPL'),
                    SimpleNamespace(symbol='MSFT'),
                ],
            ),
        )


def test_list_universes_typed_handler():
    registry = TypedRpcRegistry(default_execution='inline')
    api = TraderServiceApi(_StubTrader())  # type: ignore[arg-type]
    register_manage_surface(registry, api)
    handler = registry.resolve('query', 'list_universes').handler
    payload = handler({})
    assert payload['universes'] == [{'name': 'alpha', 'count': 2}]
