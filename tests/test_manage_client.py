"""Tests for web.manage_client typed-RPC wiring."""
from __future__ import annotations

from web.manage_client import ManageRpcClient


class _FakeTypedClient:
    connected = False
    last_timeout = None

    def __init__(self, role, auth, *, address, port, timeout=15.0):
        self.role = role
        self.address = address
        self.port = port
        self.timeout = timeout

    def connect(self) -> None:
        type(self).connected = True

    def close(self) -> None:
        pass

    def call(self, method, body, response_model, timeout=None):
        assert type(self).connected, 'call before connect'
        type(self).last_timeout = timeout
        return {'ok': True}


def test_manage_client_connects_before_first_call(monkeypatch):
    _FakeTypedClient.connected = False
    monkeypatch.setenv('MMR_TYPED_QUERY_ENDPOINT', 'tcp://trader:42101')
    client = ManageRpcClient(client_factory=lambda role, endpoint: _FakeTypedClient(
        role, None, address='tcp://trader', port=42101), timeout_s=10.0)
    client.trader_query('list_universes')
    assert _FakeTypedClient.connected is True


def test_ib_heavy_commands_floor_timeout_at_45s():
    """Watchlist add resolves via IB; a 10s client budget is too tight."""
    _FakeTypedClient.connected = False
    _FakeTypedClient.last_timeout = None
    client = ManageRpcClient(
        client_factory=lambda role, endpoint: _FakeTypedClient(
            role, None, address='tcp://trader', port=42102),
        timeout_s=10.0,
    )
    client.trader_command('add_universe_symbols', {
        'name': 'tech', 'symbols': ['AAPL'],
    })
    assert _FakeTypedClient.last_timeout == 45.0
