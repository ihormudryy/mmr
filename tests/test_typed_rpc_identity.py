import builtins
import os
import re
import time
from uuid import uuid4

import pytest

from tests.rpc_identity_fixtures import (
    ALLOW_ALL,
    ServedStack, legacy_hmac_envelope_bytes, make_identities, write_keyset,
)
from trader.messaging.rpc_keys import RpcKeyError
from trader.messaging.typed_rpc import (
    RPC_REQUEST_CONTEXT, AuthenticationError, ReplayError, RpcCaller, ServiceIdentity,
    TypedRpcRegistry, TypedRpcResponse, TypedRpcServer, canonical_json, decode_request,
    rpc_signing_bytes,
)
from trader.research.signing import sign_bytes

NOW = 1_700_000_000.0


def _req(ids, caller="cli", server="trader", role="query", method="get_status", body=None, **kw):
    return ids[caller].sign_request(server=server, role=role, method=method,
                                    request_id="r1", nonce=uuid4().hex, body=body or {}, **kw)


def test_round_trip_returns_the_authenticated_caller():
    ids = make_identities(now=lambda: NOW)
    assert ids["trader"].verify_request(_req(ids), role="query") == RpcCaller("cli", None)


def test_unknown_principal_is_rejected_before_signature_work():
    ids = make_identities(now=lambda: NOW)
    req = _req(ids).model_copy(update={"principal": "telegram_bridge"})
    with pytest.raises(AuthenticationError, match="unknown principal"):
        ids["trader"].verify_request(req, role="query")


def test_path_like_principal_is_rejected_without_touching_the_filesystem(monkeypatch):
    ids = make_identities(now=lambda: NOW)
    requests = [_req(ids).model_copy(update={"principal": bad})
                for bad in ["../verify/paper-automation", "/etc/passwd", "CLI", "cli\x00"]]

    def _fail(*a, **k):
        pytest.fail("filesystem touched")

    monkeypatch.setattr(builtins, "open", _fail)
    monkeypatch.setattr(os, "lstat", _fail)
    monkeypatch.setattr(os, "stat", _fail)
    for req in requests:
        with pytest.raises(AuthenticationError):
            ids["trader"].verify_request(req, role="query")


def test_principal_not_accepted_by_this_server_is_rejected():
    ids = make_identities(now=lambda: NOW)
    req = ids["ai_supervisor"].sign_request(server="strategy", role="query", method="list_strategies",
                                            request_id="r", nonce="n", body={})
    with pytest.raises(AuthenticationError, match="unknown principal"):
        ids["strategy"].verify_request(req, role="query")


def test_signature_by_another_principals_key_is_rejected():
    ids = make_identities(now=lambda: NOW)
    req = _req(ids, caller="dashboard").model_copy(update={"principal": "cli"})
    with pytest.raises(AuthenticationError, match="signature"):
        ids["trader"].verify_request(req, role="query")


@pytest.mark.parametrize("field,value", [("body", {"x": 1}), ("method", "get_positions"),
                                         ("timestamp", NOW + 1), ("nonce", "other"),
                                         ("request_id", "r2"), ("on_behalf_of", "cli")])
def test_tampered_request_is_rejected(field, value):
    ids = make_identities(now=lambda: NOW)
    with pytest.raises(AuthenticationError):
        ids["trader"].verify_request(_req(ids).model_copy(update={field: value}), role="query")


@pytest.mark.parametrize("server,role", [("strategy", "query"), ("trader", "command")])
def test_wrong_destination_is_rejected_and_does_not_burn_the_nonce(server, role):
    ids = make_identities(now=lambda: NOW)

    def sign(srv, rl):
        return ids["cli"].sign_request(server=srv, role=rl, method="get_status",
                                       request_id="r", nonce="same-nonce", body={})

    with pytest.raises(AuthenticationError, match="destination"):
        ids["trader"].verify_request(sign(server, role), role="query")
    ids["trader"].verify_request(sign("trader", "query"), role="query")


def test_replayed_nonce_is_rejected():
    ids = make_identities(now=lambda: NOW)
    req = _req(ids)
    ids["trader"].verify_request(req, role="query")
    with pytest.raises(ReplayError):
        ids["trader"].verify_request(req, role="query")


@pytest.mark.parametrize("skew", [31.0, -31.0])
def test_stale_or_future_timestamp_is_rejected(skew):
    clock = {"t": NOW}
    ids = make_identities(now=lambda: clock["t"])
    req = _req(ids)
    clock["t"] = NOW + skew
    with pytest.raises(AuthenticationError, match="clock skew"):
        ids["trader"].verify_request(req, role="query")


def test_on_behalf_of_from_non_trader_is_rejected():
    ids = make_identities(now=lambda: NOW)
    with pytest.raises(AuthenticationError, match="on_behalf_of"):
        ids["trader"].verify_request(_req(ids, caller="dashboard", on_behalf_of="trader"), role="query")
    req = ids["trader"].sign_request(server="strategy", role="command", method="enable_strategy",
                                     request_id="r", nonce="n2", body={}, on_behalf_of="dashboard")
    assert ids["strategy"].verify_request(req, role="command") == RpcCaller("trader", "dashboard")


def test_on_behalf_of_must_name_a_known_principal():
    ids = make_identities(now=lambda: NOW)
    req = ids["trader"].sign_request(server="strategy", role="command", method="enable_strategy",
                                     request_id="r", nonce="n3", body={}, on_behalf_of="operator")
    with pytest.raises(AuthenticationError, match="on_behalf_of"):
        ids["strategy"].verify_request(req, role="command")


def test_bundle_signature_over_the_same_bytes_does_not_verify_as_rpc():
    ids = make_identities(now=lambda: NOW)
    req = _req(ids)
    raw_key = ids["cli"]._private_key_for_tests()
    bundle_style = sign_bytes(raw_key, rpc_signing_bytes(req)[len(RPC_REQUEST_CONTEXT):])
    with pytest.raises(AuthenticationError):
        ids["trader"].verify_request(req.model_copy(update={"signature": bundle_style}), role="query")


def test_legacy_hmac_envelope_is_refused_at_decode():
    legacy = canonical_json({"method": "get_status", "request_id": "r", "timestamp": NOW,
                             "nonce": "n", "body": {}, "signature": "00" * 32})
    with pytest.raises(AuthenticationError, match="malformed"):
        decode_request(legacy)


def test_client_rejects_response_signed_by_the_other_server():
    ids = make_identities(now=lambda: NOW)
    resp = ids["strategy"].sign_response(TypedRpcResponse(request_id="r", ok=True, body={},
                                         server="strategy", request_digest="d"))
    with pytest.raises(AuthenticationError):
        ids["cli"].verify_response(resp.model_copy(update={"server": "trader"}), server="trader",
                                   request_digest="d")
    with pytest.raises(AuthenticationError, match="server"):
        ids["cli"].verify_response(resp, server="trader", request_digest="d")


def test_client_rejects_response_for_another_request_digest():
    ids = make_identities(now=lambda: NOW)
    resp = ids["trader"].sign_response(TypedRpcResponse(request_id="r", ok=True, body={},
                                       server="trader", request_digest="other"))
    with pytest.raises(AuthenticationError, match="digest"):
        ids["cli"].verify_response(resp, server="trader", request_digest="mine")
    ids["cli"].verify_response(resp, server="trader", request_digest="other")


def test_unsigned_response_is_rejected():
    ids = make_identities(now=lambda: NOW)
    resp = TypedRpcResponse(request_id="r", ok=True, body={}, server="trader", request_digest="d")
    with pytest.raises(AuthenticationError, match="signature"):
        ids["cli"].verify_response(resp, server="trader", request_digest="d")


def test_identity_repr_never_shows_the_private_key():
    ids = make_identities()
    text = repr(ids["cli"]) + str(ids["cli"])
    assert "cli" in text and "PRIVATE" not in text and "ed25519-" in text


# --------------------------------------------------------------------------- transport

@pytest.fixture
def stack():
    ids = make_identities()
    calls = {"n": 0}

    def _status(_body):
        calls["n"] += 1
        return {"ok": 1}

    registry = TypedRpcRegistry(acl=ALLOW_ALL)
    registry.register("query", "get_status", dict, dict, _status)
    served = ServedStack({("trader", "query"): registry}, ids)
    served.calls = calls
    yield served
    served.close()


def test_server_replies_authentication_error_and_runs_no_handler_for_legacy_envelope(stack):
    reply = stack.send_raw(legacy_hmac_envelope_bytes(), to="trader", role="query")
    assert not reply.ok and reply.problem.code == "AUTHENTICATION_ERROR"
    assert reply.request_id == "" and reply.request_digest == ""
    assert stack.calls["n"] == 0


def test_client_resets_socket_on_reply_from_wrong_server():
    import threading

    import zmq

    from tests.rpc_identity_fixtures import free_port
    from trader.messaging.typed_rpc import TypedRpcClient, canonical_json, decode_request

    ids = make_identities()
    port = free_port()
    ctx = zmq.Context()
    router = ctx.socket(zmq.ROUTER)
    router.setsockopt(zmq.LINGER, 0)
    router.bind(f"tcp://127.0.0.1:{port}")

    def _fake_server():
        frames = router.recv_multipart()
        request = decode_request(frames[-1])
        from trader.messaging.typed_rpc import request_digest
        forged = ids["strategy"].sign_response(TypedRpcResponse(
            request_id=request.request_id, ok=True, body={}, server="strategy",
            request_digest=request_digest(frames[-1])))
        router.send_multipart([frames[0], b"", canonical_json(forged.model_dump(mode="json"))])

    thread = threading.Thread(target=_fake_server, daemon=True)
    thread.start()
    client = TypedRpcClient("query", ids["cli"], server="trader", port=port, timeout=3.0)
    client.connect()
    before = client.socket.getsockopt(zmq.IDENTITY)
    try:
        with pytest.raises(AuthenticationError):
            client.call("get_status", {}, dict)
        assert client.socket.getsockopt(zmq.IDENTITY) != before
    finally:
        thread.join(timeout=3.0)
        client.close()
        router.close(linger=0)
        ctx.term()


def test_round_trip_over_sockets(stack):
    assert stack.client("cli").call("get_status", {}, dict) == {"ok": 1}


def test_server_identity_must_be_a_server_principal():
    ids = make_identities()
    with pytest.raises(ValueError):
        TypedRpcServer("query", TypedRpcRegistry(acl=ALLOW_ALL), ids["cli"])


def test_client_must_name_a_server():
    from trader.messaging.typed_rpc import TypedRpcClient
    ids = make_identities()
    with pytest.raises(ValueError):
        TypedRpcClient("query", ids["cli"], server="dashboard")


def test_startup_fails_without_keys(tmp_path):
    with pytest.raises(RpcKeyError):
        ServiceIdentity.load("trader", tmp_path)


def test_load_from_real_key_files(tmp_path):
    write_keyset(tmp_path)
    identity = ServiceIdentity.load("strategy", tmp_path)
    assert identity.principal == "strategy"
    assert identity.trusted_principals() == {"cli", "dashboard", "trader"}


def test_handler_returning_a_non_json_value_fails_loudly_not_silently():
    ids = make_identities()
    registry = TypedRpcRegistry(acl=ALLOW_ALL)
    registry.register("query", "bad", dict, dict, lambda body: {"x": object()})
    served = ServedStack({("trader", "query"): registry}, ids)
    try:
        assert served.raw_code(served.signed("cli", "trader", "query", "bad")) == "VALIDATION_ERROR"
    finally:
        served.close()


# --------------------------------------------------------------------------- bad timestamps

def _signed_request_with_timestamp(ids, literal):
    """Wire bytes of a signed request whose timestamp is replaced by a raw JSON literal."""
    wire = canonical_json(_req(ids).model_dump(mode="json")).decode()
    return re.sub(r'"timestamp":[^,}]+', lambda _m: '"timestamp":' + literal, wire).encode()


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "true", '"1"', "null", "-1", "1e30"])
def test_server_refuses_bad_timestamp_cleanly_before_authentication(stack, caplog, literal):
    raw = _signed_request_with_timestamp(stack.identities, literal)
    with caplog.at_level("ERROR"):
        reply = stack.send_raw(raw, to="trader", role="query")
    assert not reply.ok
    assert reply.problem.code == "AUTHENTICATION_ERROR"
    assert stack.calls["n"] == 0
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_server_still_accepts_a_valid_timestamp(stack):
    assert stack.client("cli").call("get_status", {}, dict) == {"ok": 1}


@pytest.mark.parametrize("literal", ["true", '"1"', "null", "-1", "1e30"])
def test_decode_refuses_bad_timestamp_before_any_signature_work(literal):
    ids = make_identities()
    with pytest.raises(AuthenticationError, match="timestamp"):
        decode_request(_signed_request_with_timestamp(ids, literal))


def test_accepts_reports_keyring_membership():
    ids = make_identities()
    assert ids["trader"].accepts("ai_supervisor") and not ids["strategy"].accepts("ai_supervisor")
