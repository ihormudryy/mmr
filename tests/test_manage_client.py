"""Tests for web.manage_client typed-RPC wiring."""
from __future__ import annotations

from web.manage_client import ManageRpcClient


class _FakeTypedClient:
    connected = False

    def __init__(self, role, auth, *, address, port, timeout=15.0):
        self.role = role
        self.address = address
        self.port = port

    def connect(self) -> None:
        type(self).connected = True

    def close(self) -> None:
        pass

    def call(self, method, body, response_model, timeout=None):
        assert type(self).connected, 'call before connect'
        return {'ok': True}


def test_manage_client_connects_before_first_call(monkeypatch):
    monkeypatch.setenv('MMR_TYPED_QUERY_ENDPOINT', 'tcp://trader:42101')
    client = ManageRpcClient(client_factory=lambda role, endpoint: _FakeTypedClient(
        role, None, address='tcp://trader', port=42101))
    client.trader_query('list_universes')
    assert _FakeTypedClient.connected is True
