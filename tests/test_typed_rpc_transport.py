"""Tests for the dedicated typed ZeroMQ transport (G0 Task 3).

Covers the brief's two required tests verbatim (allowlist rejection on the
wrong socket role; unknown/dotted/private methods rejected), plus the
transport-hygiene and response-signing behaviour called out in the outer
task instructions:

- role-based allowlisting (``TypedRpcRegistry``): exactly one role per
  method, invalid roles/names rejected at registration time
- pure raw-JSON wire format (json.loads/json.dumps only -- no pack/unpack)
- successful round trips on each of the three roles (query/command/feed)
- request-body and handler-response schema validation
- replay rejection at the full transport level (not just the authenticator
  unit tests in test_typed_rpc.py)
- transport hygiene: LINGER=0, IMMEDIATE=1, MAXMSGSIZE=1 MiB, request-id
  reply matching, fresh DEALER identity (and dropped late reply) after a
  timeout
- feed client isolation: a slow feed call must never block a concurrent
  command call (separate instance + lock, proven by actually timing it)
- response signing: the server signs every reply and the client refuses an
  unsigned/mis-signed one
- the service HMAC key file's four production hardening checks
"""

from __future__ import annotations

import json
import socket as socket_module
import threading
import time
from typing import Optional

import pytest
import zmq
from pydantic import BaseModel, ConfigDict, ValidationError

from trader.messaging.typed_rpc import (
    MAX_REQUEST_BYTES,
    MIN_KEY_BYTES,
    AuthenticationError,
    HmacServiceAuthenticator,
    ReplayError,
    RpcProblem,
    ServiceHmacKeyError,
    TypedRpcClient,
    TypedRpcRegistry,
    TypedRpcRemoteError,
    TypedRpcResponse,
    TypedRpcServer,
    VALID_SOCKET_ROLES,
    canonical_json,
    load_service_hmac_key,
)


HMAC_KEY = b"k" * 32


def _free_port() -> int:
    with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Test-only request/response schemas + handlers
# ---------------------------------------------------------------------------

class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: int


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


def _handle_get_status(_body: EmptyBody) -> dict:
    return {"ok": True}


def _handle_approve_proposal(body: ApproveProposalRequest) -> dict:
    return {"proposal_id": body.proposal_id, "status": "EXECUTED"}


def _handle_slow_query(_body: EmptyBody) -> dict:
    import asyncio
    # Deliberately slow (but non-blocking to the event loop) so the
    # "fresh DEALER identity after timeout" test can prove the client times
    # out while the handler is still in flight, and the eventual late reply
    # from this handler is dropped rather than misdelivered to a later call.
    async def _later():
        await asyncio.sleep(0.5)
        return {"stale": True}
    return _later()


def _handle_slow_feed(_body: EmptyBody) -> dict:
    import asyncio

    async def _later():
        await asyncio.sleep(0.4)
        return {"feed": "done"}
    return _later()


def _handle_bad_response(_body: EmptyBody) -> dict:
    # Deliberately violates its declared response_model (StatusResponse
    # requires `ok: bool`) to exercise the server-side VALIDATION_ERROR path.
    return {"unexpected": "shape"}


def _build_registry() -> TypedRpcRegistry:
    registry = TypedRpcRegistry()
    registry.register("query", "get_status", EmptyBody, dict, _handle_get_status)
    registry.register("query", "slow_query", EmptyBody, dict, _handle_slow_query)
    registry.register("query", "bad_response", EmptyBody, StatusResponse, _handle_bad_response)
    registry.register("command", "approve_proposal", ApproveProposalRequest, dict, _handle_approve_proposal)
    registry.register("feed", "slow_feed", EmptyBody, dict, _handle_slow_feed)
    return registry


# ---------------------------------------------------------------------------
# Fixtures: three servers (query/command/feed) on one shared asyncio loop in
# a background thread, and one TypedRpcClient per role in the main thread.
# ---------------------------------------------------------------------------

@pytest.fixture
def server_authenticator():
    return HmacServiceAuthenticator(HMAC_KEY, now=time.time)


@pytest.fixture
def client_authenticator():
    # A SEPARATE authenticator instance from the server's, constructed from
    # the same shared secret -- mirrors the real deployment (client and
    # server are different processes) rather than accidentally passing
    # because they share one Python object's state.
    return HmacServiceAuthenticator(HMAC_KEY, now=time.time)


@pytest.fixture
def typed_servers(server_authenticator):
    import asyncio

    registry = _build_registry()
    ports = {role: _free_port() for role in VALID_SOCKET_ROLES}
    servers = {
        role: TypedRpcServer(role, registry, server_authenticator, port=ports[role])
        for role in VALID_SOCKET_ROLES
    }

    ready = threading.Event()
    state = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state["loop"] = loop

        async def _start_all():
            await asyncio.gather(*(s.serve() for s in servers.values()))

        loop.run_until_complete(_start_all())
        ready.set()
        try:
            loop.run_forever()
        finally:
            # Drain any still-in-flight handler tasks (e.g. slow_query/
            # slow_feed's asyncio.sleep) before closing -- otherwise asyncio
            # logs a spurious "Task was destroyed but it is pending!" for
            # every test that deliberately lets a slow handler run past the
            # client's timeout.
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(timeout=3.0), "typed RPC servers did not signal ready"
    time.sleep(0.1)  # let ROUTER sockets finish binding

    yield {"servers": servers, "ports": ports, "registry": registry}

    loop = state.get("loop")
    if loop:
        for s in servers.values():
            loop.call_soon_threadsafe(s.close)
        loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=3.0)


def _make_client(role: str, ports: dict, authenticator, timeout: float = 3.0) -> TypedRpcClient:
    client = TypedRpcClient(role, authenticator, port=ports[role], timeout=timeout)
    client.connect()
    return client


@pytest.fixture
def query_client(typed_servers, client_authenticator):
    client = _make_client("query", typed_servers["ports"], client_authenticator)
    yield client
    client.close()


@pytest.fixture
def command_client(typed_servers, client_authenticator):
    client = _make_client("command", typed_servers["ports"], client_authenticator)
    yield client
    client.close()


@pytest.fixture
def feed_client(typed_servers, client_authenticator):
    client = _make_client("feed", typed_servers["ports"], client_authenticator)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Brief Step 1 -- verbatim
# ---------------------------------------------------------------------------

def test_command_is_rejected_on_query_socket(typed_servers, query_client):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query_client.call("approve_proposal", {"proposal_id": 7}, dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"


def test_unknown_and_dotted_methods_are_rejected(query_client):
    for method in ("trader.client.ib.reqGlobalCancel", "_private", "missing"):
        with pytest.raises(TypedRpcRemoteError):
            query_client.call(method, {}, dict)


# ---------------------------------------------------------------------------
# TypedRpcRegistry -- allowlist unit tests (no sockets)
# ---------------------------------------------------------------------------

class TestTypedRpcRegistry:
    def test_register_rejects_invalid_role(self):
        registry = TypedRpcRegistry()
        with pytest.raises(ValueError, match="socket_role"):
            registry.register("admin", "get_status", EmptyBody, dict, _handle_get_status)

    @pytest.mark.parametrize("method", ["", "_private", "trader.client.ib.reqGlobalCancel"])
    def test_register_rejects_invalid_method_names(self, method):
        registry = TypedRpcRegistry()
        with pytest.raises(ValueError):
            registry.register("query", method, EmptyBody, dict, _handle_get_status)

    def test_method_can_only_be_registered_on_one_role(self):
        registry = TypedRpcRegistry()
        registry.register("query", "get_status", EmptyBody, dict, _handle_get_status)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("command", "get_status", EmptyBody, dict, _handle_get_status)

    def test_duplicate_registration_on_same_role_rejected(self):
        registry = TypedRpcRegistry()
        registry.register("query", "get_status", EmptyBody, dict, _handle_get_status)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("query", "get_status", EmptyBody, dict, _handle_get_status)

    def test_resolve_is_scoped_to_the_exact_role(self):
        registry = TypedRpcRegistry()
        registry.register("command", "approve_proposal", ApproveProposalRequest, dict, _handle_approve_proposal)
        assert registry.resolve("command", "approve_proposal") is not None
        assert registry.resolve("query", "approve_proposal") is None
        assert registry.resolve("command", "missing") is None

    def test_contains(self):
        registry = TypedRpcRegistry()
        registry.register("query", "get_status", EmptyBody, dict, _handle_get_status)
        assert registry.contains("query", "get_status") is True
        assert registry.contains("command", "get_status") is False
        assert registry.contains("query", "missing") is False


# ---------------------------------------------------------------------------
# Successful round trips on each role
# ---------------------------------------------------------------------------

def test_query_round_trip_returns_dict_body(query_client):
    result = query_client.call("get_status", {}, dict)
    assert result == {"ok": True}


def test_command_round_trip_via_command_client(command_client):
    result = command_client.call("approve_proposal", {"proposal_id": 42}, dict)
    assert result == {"proposal_id": 42, "status": "EXECUTED"}


def test_feed_round_trip_via_feed_client(feed_client):
    # slow_feed sleeps 0.4s server-side (async, non-blocking) before replying.
    result = feed_client.call("slow_feed", {}, dict, timeout=2.0)
    assert result == {"feed": "done"}


# ---------------------------------------------------------------------------
# Schema validation: request body and handler response
# ---------------------------------------------------------------------------

def test_invalid_request_body_is_rejected(command_client):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command_client.call("approve_proposal", {"proposal_id": "not-an-int"}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_extra_field_in_request_body_is_rejected(command_client):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command_client.call("approve_proposal", {"proposal_id": 1, "extra": "nope"}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_handler_response_violating_its_schema_is_rejected(query_client):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query_client.call("bad_response", {}, StatusResponse)
    assert exc.value.code == "VALIDATION_ERROR"


def test_response_model_validates_successful_body(query_client):
    class OkModel(BaseModel):
        model_config = ConfigDict(extra="forbid")
        ok: bool

    result = query_client.call("get_status", {}, OkModel)
    assert isinstance(result, OkModel)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Replay rejection at the full transport level
# ---------------------------------------------------------------------------

def test_replayed_request_is_rejected_over_the_wire(typed_servers, client_authenticator):
    ports = typed_servers["ports"]
    ctx = zmq.Context()
    sock = ctx.socket(zmq.DEALER)
    sock.connect(f"tcp://127.0.0.1:{ports['query']}")

    request = client_authenticator.sign("get_status", "replay-req-1", "replay-nonce-1", {})
    raw = canonical_json(request.model_dump(mode="json"))

    sock.send(raw)
    first = json.loads(sock.recv_multipart()[-1])
    assert first["ok"] is True

    sock.send(raw)  # exact same signed envelope again
    second = json.loads(sock.recv_multipart()[-1])
    assert second["ok"] is False
    assert second["problem"]["code"] == "REPLAY_ERROR"

    sock.close()


# ---------------------------------------------------------------------------
# Wire format: proves plain JSON (no msgpack/dill) and that responses are
# genuinely signed by the server and verifiable by an independent client key
# ---------------------------------------------------------------------------

def test_wire_format_is_plain_json_and_response_is_signed(typed_servers):
    ports = typed_servers["ports"]
    ctx = zmq.Context()
    sock = ctx.socket(zmq.DEALER)
    sock.connect(f"tcp://127.0.0.1:{ports['query']}")

    auth = HmacServiceAuthenticator(HMAC_KEY, now=time.time)
    request = auth.sign("get_status", "raw-req-1", "raw-nonce-1", {})
    # Hand-build the wire bytes exactly as json.dumps would, proving the
    # request side is plain JSON too (no msgpack framing).
    sock.send(json.dumps(request.model_dump(mode="json")).encode("utf-8"))

    frames = sock.recv_multipart()
    # json.loads must succeed directly on the raw frame -- this IS the proof
    # the wire format is plain JSON, not msgpack (msgpack bytes are not valid
    # UTF-8 JSON in general and would raise here).
    payload = json.loads(frames[-1])
    assert payload["ok"] is True
    assert payload["body"] == {"ok": True}

    response = TypedRpcResponse.model_validate(payload)
    assert response.signature  # server actually signed it
    auth.verify_response(response)  # must not raise: signed with the same shared key

    # Tampering with the body after the fact must invalidate the signature.
    tampered = response.model_copy(update={"body": {"ok": False}})
    with pytest.raises(AuthenticationError):
        auth.verify_response(tampered)

    sock.close()


def test_bad_request_signature_over_the_wire_yields_authentication_error(typed_servers):
    ports = typed_servers["ports"]
    ctx = zmq.Context()
    sock = ctx.socket(zmq.DEALER)
    sock.connect(f"tcp://127.0.0.1:{ports['query']}")

    auth = HmacServiceAuthenticator(HMAC_KEY, now=time.time)
    request = auth.sign("get_status", "raw-req-2", "raw-nonce-2", {})
    forged = request.model_copy(update={"signature": "0" * len(request.signature)})
    sock.send(json.dumps(forged.model_dump(mode="json")).encode("utf-8"))

    payload = json.loads(sock.recv_multipart()[-1])
    assert payload["ok"] is False
    assert payload["problem"]["code"] == "AUTHENTICATION_ERROR"
    # Even the error response is properly signed by the server's real key.
    response = TypedRpcResponse.model_validate(payload)
    auth.verify_response(response)

    sock.close()


def test_client_rejects_a_response_signed_with_the_wrong_key(typed_servers):
    ports = typed_servers["ports"]
    right_key_client = TypedRpcClient(
        "query", HmacServiceAuthenticator(HMAC_KEY, now=time.time),
        port=ports["query"], timeout=2.0,
    )
    right_key_client.connect()
    # Swap in an authenticator with a DIFFERENT key purely for verify_response
    # -- simulates a misconfigured/compromised peer whose replies don't carry
    # a signature this client can trust, even though the request went through
    # fine (the server used the real key to authenticate + sign).
    right_key_client.authenticator = HmacServiceAuthenticator(b"x" * 32, now=time.time)
    with pytest.raises(AuthenticationError):
        right_key_client.call("get_status", {}, dict)
    right_key_client.close()


# ---------------------------------------------------------------------------
# Non-finite / malformed reply: rejected in-taxonomy AND socket reset
# ---------------------------------------------------------------------------

def test_non_finite_reply_is_rejected_in_taxonomy_and_socket_is_reset(client_authenticator):
    """A forged reply carrying a non-finite number (matching request_id) must
    be rejected via an in-taxonomy AuthenticationError -- NOT a bare
    ValueError from verify_response -> canonical_json -- and the poisoned
    socket must be reset so a subsequent call still succeeds.

    Uses a hand-driven ROUTER that returns the poison reply for the first
    request and a valid signed reply for the second, so we can prove both
    halves: (1) in-taxonomy rejection, (2) the pipe was reset, not left
    poisoned.
    """
    port = _free_port()
    server_auth = HmacServiceAuthenticator(HMAC_KEY, now=time.time)
    ctx = zmq.Context()
    router = ctx.socket(zmq.ROUTER)
    router.setsockopt(zmq.LINGER, 0)
    router.bind(f"tcp://127.0.0.1:{port}")

    stop = threading.Event()
    seen = {"n": 0}

    def _serve():
        poller = zmq.Poller()
        poller.register(router, zmq.POLLIN)
        while not stop.is_set():
            if not poller.poll(50):
                continue
            frames = router.recv_multipart()
            client_id, raw = frames[0], frames[-1]
            request_id = json.loads(raw)["request_id"]
            seen["n"] += 1
            if seen["n"] == 1:
                # Hand-built forged reply: the non-finite literal 1e999 spliced
                # directly into the body (canonical_json refuses to EMIT it, so
                # it has to be constructed as raw bytes), with a MATCHING
                # request_id so the client would correlate it as "our" reply.
                poison = (
                    '{"request_id":"%s","ok":true,"body":{"x":1e999},'
                    '"problem":null,"signature":"deadbeef"}' % request_id
                ).encode("utf-8")
                router.send_multipart([client_id, b"", poison])
            else:
                resp = server_auth.sign_response(
                    TypedRpcResponse(request_id=request_id, ok=True, body={"ok": True}))
                router.send_multipart(
                    [client_id, b"", canonical_json(resp.model_dump(mode="json"))])

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    time.sleep(0.1)

    client = TypedRpcClient("query", client_authenticator, port=port, timeout=2.0)
    client.connect()
    try:
        with pytest.raises(AuthenticationError):
            client.call("get_status", {}, dict)
        # The socket was reset (fresh identity) -- let the reconnect settle
        # (IMMEDIATE=1) and prove a subsequent call still works.
        time.sleep(0.15)
        assert client.call("get_status", {}, dict) == {"ok": True}
    finally:
        client.close()
        stop.set()
        thread.join(timeout=2.0)
        router.close(linger=0)
        ctx.term()


# ---------------------------------------------------------------------------
# Transport hygiene: socket options
# ---------------------------------------------------------------------------

def test_client_socket_hygiene_options(query_client):
    assert query_client.socket.getsockopt(zmq.LINGER) == 0
    assert query_client.socket.getsockopt(zmq.IMMEDIATE) == 1
    assert query_client.socket.getsockopt(zmq.MAXMSGSIZE) == MAX_REQUEST_BYTES


def test_server_socket_hygiene_options(typed_servers):
    server = typed_servers["servers"]["query"]
    assert server.socket.getsockopt(zmq.LINGER) == 0
    assert server.socket.getsockopt(zmq.MAXMSGSIZE) == MAX_REQUEST_BYTES


# ---------------------------------------------------------------------------
# Fresh DEALER identity after timeout + dropped late reply
# ---------------------------------------------------------------------------

def test_dealer_gets_fresh_identity_after_timeout_and_late_reply_is_dropped(query_client):
    old_identity = query_client.socket.getsockopt(zmq.IDENTITY)

    with pytest.raises(TimeoutError):
        query_client.call("slow_query", {}, dict, timeout=0.2)

    new_identity = query_client.socket.getsockopt(zmq.IDENTITY)
    assert new_identity != old_identity

    # Let the slow handler actually finish server-side (it replies ~0.5s
    # after being invoked); its reply is addressed to the OLD identity and
    # must be dropped by ZMQ, not misdelivered here.
    time.sleep(0.6)

    result = query_client.call("get_status", {}, dict)
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# Feed isolation: a slow feed call must never block a concurrent command call
# ---------------------------------------------------------------------------

def test_feed_and_command_clients_are_separate_instances_and_locks(feed_client, command_client):
    assert feed_client is not command_client
    assert feed_client._lock is not command_client._lock
    assert feed_client.socket is not command_client.socket


def test_slow_feed_call_does_not_block_concurrent_command_call(feed_client, command_client):
    results = {}

    def _run_feed():
        start = time.monotonic()
        results["feed"] = feed_client.call("slow_feed", {}, dict, timeout=2.0)
        results["feed_elapsed"] = time.monotonic() - start

    feed_thread = threading.Thread(target=_run_feed)
    feed_thread.start()
    time.sleep(0.05)  # let the feed call actually get in flight first

    start = time.monotonic()
    command_result = command_client.call("approve_proposal", {"proposal_id": 99}, dict)
    command_elapsed = time.monotonic() - start

    feed_thread.join(timeout=2.0)

    assert command_result == {"proposal_id": 99, "status": "EXECUTED"}
    # The command call must complete quickly -- nowhere near the feed
    # handler's ~0.4s delay -- because it never contends for the feed
    # client's lock or socket.
    assert command_elapsed < 0.3
    assert results["feed"] == {"feed": "done"}


# ---------------------------------------------------------------------------
# Service HMAC key file: the four production-hardening checks
# ---------------------------------------------------------------------------

class TestLoadServiceHmacKey:
    def test_missing_path_is_rejected(self):
        with pytest.raises(ServiceHmacKeyError, match="not configured"):
            load_service_hmac_key("")

    def test_nonexistent_file_is_rejected(self, tmp_path):
        with pytest.raises(ServiceHmacKeyError, match="not found"):
            load_service_hmac_key(str(tmp_path / "does-not-exist.key"))

    def test_wrong_permissions_are_rejected(self, tmp_path):
        key_path = tmp_path / "service_hmac.key"
        key_path.write_bytes(b"k" * 32)
        key_path.chmod(0o644)
        with pytest.raises(ServiceHmacKeyError, match="mode"):
            load_service_hmac_key(str(key_path))

    def test_empty_file_is_rejected(self, tmp_path):
        key_path = tmp_path / "service_hmac.key"
        key_path.write_bytes(b"")
        key_path.chmod(0o600)
        with pytest.raises(ServiceHmacKeyError, match="empty"):
            load_service_hmac_key(str(key_path))

    def test_too_short_key_is_rejected(self, tmp_path):
        key_path = tmp_path / "service_hmac.key"
        key_path.write_bytes(b"x" * (MIN_KEY_BYTES - 1))
        key_path.chmod(0o600)
        with pytest.raises(ServiceHmacKeyError, match="32"):
            load_service_hmac_key(str(key_path))

    def test_valid_key_file_loads(self, tmp_path):
        key_path = tmp_path / "service_hmac.key"
        key_bytes = b"z" * 40
        key_path.write_bytes(key_bytes)
        key_path.chmod(0o600)
        loaded = load_service_hmac_key(str(key_path))
        assert loaded == key_bytes
        # And it's directly usable to construct a real authenticator.
        HmacServiceAuthenticator(loaded)

    def test_does_not_strip_trailing_newline(self, tmp_path):
        # Deliberately NOT stripped -- see load_service_hmac_key's docstring:
        # stripping would risk silently truncating real key material that
        # legitimately ends in 0x0a.
        key_path = tmp_path / "service_hmac.key"
        key_bytes = b"y" * 31 + b"\n"  # 32 bytes total, last byte is 0x0a
        key_path.write_bytes(key_bytes)
        key_path.chmod(0o600)
        loaded = load_service_hmac_key(str(key_path))
        assert loaded == key_bytes
        assert len(loaded) == 32
