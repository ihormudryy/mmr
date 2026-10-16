"""Issue #197: a read that never answers must not hold a handler slot for ever.

An IB request without a timeout hung every `resolve_instrument` call while the gateway answered nothing; the
trader's 32 query slots filled and every read got SERVER_BUSY for hours.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.rpc_identity_fixtures import ALLOW_ALL
from tests.test_typed_rpc_async_handlers import _Ids, _raw
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcServer


def _server(role, *, handler_timeout=None, max_in_flight=1):
    auth = _Ids(now=lambda: 1_700_000_000.0)
    auth.role = role
    never = asyncio.Event()
    registry = TypedRpcRegistry(acl=ALLOW_ALL)

    async def hangs(_body):
        await never.wait()
        return {}

    async def quick(_body):
        return {"ok": True}

    registry.register(role, "hangs", dict, dict, hangs)
    registry.register(role, "quick", dict, dict, quick)
    kwargs = {} if handler_timeout is None else {"handler_timeout": handler_timeout}
    server = TypedRpcServer(role, registry, auth.server, max_in_flight=max_in_flight, **kwargs)
    replies = {}

    async def capture(_client_id, request_id, _digest, ok, body, problem):
        replies[request_id] = (ok, body, problem)

    server._reply = capture
    return auth, server, replies


@pytest.mark.asyncio
async def test_a_query_that_never_answers_times_out_and_frees_its_slot():
    auth, server, replies = _server("query", handler_timeout=0.2)
    await server._handle_request(b"a", _raw(auth, "hangs", "one"))
    ok, _body, problem = replies["one"]
    assert ok is False and problem.code == "HANDLER_TIMEOUT" and "hangs" in problem.message
    await server._handle_request(b"b", _raw(auth, "quick", "two"))          # the only slot is free again
    assert replies["two"][0] is True
    server.close()


def test_the_query_socket_has_a_default_deadline_below_the_client_timeout(monkeypatch):
    monkeypatch.delenv("TYPED_RPC_QUERY_HANDLER_TIMEOUT_SECONDS", raising=False)
    _auth, server, _replies = _server("query")
    assert 0 < server.handler_timeout < 30
    server.close()


def test_the_query_deadline_can_be_configured(monkeypatch):
    monkeypatch.setenv("TYPED_RPC_QUERY_HANDLER_TIMEOUT_SECONDS", "5")
    _auth, server, _replies = _server("query")
    assert server.handler_timeout == 5.0
    server.close()


def test_a_command_is_never_cut_off_half_way():
    """Cancelling an approve or a cancel mid-way could leave a half-run saga: commands keep no deadline."""
    _auth, server, _replies = _server("command")
    assert server.handler_timeout is None
    server.close()


@pytest.mark.asyncio
async def test_resolve_contract_gives_up_when_ib_never_answers(monkeypatch):
    from trader.trading import trading_runtime

    monkeypatch.setattr(trading_runtime, "IB_REQUEST_TIMEOUT_SECONDS", 0.1)
    never = asyncio.get_running_loop().create_future()
    runtime = SimpleNamespace(client=SimpleNamespace(ib=SimpleNamespace(reqContractDetailsAsync=lambda c: never)))
    with pytest.raises(trading_runtime.IbRequestTimeout, match="contract details"):
        await trading_runtime.Trader.resolve_contract(runtime, object())
