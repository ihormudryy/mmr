from __future__ import annotations

import asyncio
import threading

import pytest

from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcRegistry,
    TypedRpcServer,
    canonical_json,
)


def _raw(auth, method: str, request_id: str):
    request = auth.sign(method, request_id, f"nonce-{request_id}", {})
    return canonical_json(request.model_dump(mode="json"))


def test_registry_records_explicit_thread_execution():
    registry = TypedRpcRegistry()
    registry.register("command", "slow", dict, dict, lambda _body: {}, execution="thread")

    assert registry.resolve("command", "slow").execution == "thread"


def test_registry_rejects_unknown_execution_mode():
    registry = TypedRpcRegistry()

    with pytest.raises(ValueError, match="execution"):
        registry.register("query", "bad", dict, dict, lambda _body: {}, execution="process")


def test_registry_rejects_empty_execution_mode_instead_of_using_default():
    registry = TypedRpcRegistry(default_execution="thread")

    with pytest.raises(ValueError, match="execution"):
        registry.register("query", "bad", dict, dict, lambda _body: {}, execution="")


@pytest.mark.parametrize("configured", ["0", "-1", "many"])
def test_server_rejects_invalid_environment_capacity(monkeypatch, configured):
    monkeypatch.setenv("TYPED_RPC_MAX_IN_FLIGHT", configured)
    auth = HmacServiceAuthenticator(b"k" * 32)

    with pytest.raises(ValueError, match="positive integer"):
        TypedRpcServer("query", TypedRpcRegistry(), auth)


@pytest.mark.asyncio
async def test_thread_handler_does_not_block_second_request():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    started = threading.Event()
    release = threading.Event()
    registry = TypedRpcRegistry()

    def slow(_body):
        started.set()
        release.wait(timeout=2)
        return {"kind": "slow"}

    registry.register("query", "slow", dict, dict, slow, execution="thread")
    registry.register("query", "quick", dict, dict, lambda _body: {"kind": "quick"})
    server = TypedRpcServer("query", registry, auth)
    replies = {}

    async def capture(_client_id, request_id, ok, body, problem):
        replies[request_id] = (ok, body, problem)

    server._reply = capture
    slow_task = asyncio.create_task(server._handle_request(b"slow-client", _raw(auth, "slow", "slow")))
    assert await asyncio.to_thread(started.wait, 1)

    await asyncio.wait_for(
        server._handle_request(b"quick-client", _raw(auth, "quick", "quick")),
        timeout=0.25,
    )

    assert replies["quick"] == (True, {"kind": "quick"}, None)
    release.set()
    await slow_task
    server.close()


@pytest.mark.asyncio
async def test_saturated_server_replies_busy_without_invoking_handler():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    started = threading.Event()
    release = threading.Event()
    quick_calls = 0
    registry = TypedRpcRegistry(default_execution="thread")

    def slow(_body):
        started.set()
        release.wait(timeout=2)
        return {}

    def quick(_body):
        nonlocal quick_calls
        quick_calls += 1
        return {}

    registry.register("command", "slow", dict, dict, slow)
    registry.register("command", "quick", dict, dict, quick)
    server = TypedRpcServer("command", registry, auth, max_in_flight=1)
    replies = {}

    async def capture(_client_id, request_id, ok, body, problem):
        replies[request_id] = (ok, body, problem)

    server._reply = capture
    slow_task = asyncio.create_task(server._handle_request(b"one", _raw(auth, "slow", "one")))
    assert await asyncio.to_thread(started.wait, 1)
    await server._handle_request(b"two", _raw(auth, "quick", "two"))

    ok, _body, problem = replies["two"]
    assert ok is False
    assert problem.code == "SERVER_BUSY"
    assert quick_calls == 0
    release.set()
    await slow_task
    server.close()


@pytest.mark.asyncio
async def test_aclose_stops_replies_after_bounded_drain():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    started = threading.Event()
    release = threading.Event()
    registry = TypedRpcRegistry(default_execution="thread")

    def slow(_body):
        started.set()
        release.wait(timeout=2)
        return {}

    registry.register("command", "slow", dict, dict, slow)
    server = TypedRpcServer("command", registry, auth)
    replies = []

    async def capture(*args):
        replies.append(args)

    server._reply = capture
    task = asyncio.create_task(server._handle_request(b"one", _raw(auth, "slow", "one")))
    server._handler_tasks.add(task)
    assert await asyncio.to_thread(started.wait, 1)

    await asyncio.wait_for(server.aclose(drain_timeout=0.01), timeout=0.25)
    release.set()
    await asyncio.sleep(0.05)

    assert replies == []


@pytest.mark.asyncio
async def test_aclose_waits_for_accept_loop_cancellation():
    auth = HmacServiceAuthenticator(b"k" * 32)
    server = TypedRpcServer("query", TypedRpcRegistry(), auth)
    accept_task = asyncio.create_task(asyncio.sleep(60))
    server._serve_task = accept_task

    await server.aclose(drain_timeout=0.01)

    assert accept_task.done()
    assert accept_task.cancelled()
