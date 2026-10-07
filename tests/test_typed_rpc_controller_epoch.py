"""SP2 Plan 1 Task 2: the controller epoch rides in the signed envelope (spec 5.1, 6.2b)."""
from __future__ import annotations

import json
import time
import uuid

import pytest

from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, make_identities
from trader.messaging.typed_rpc import (
    RPC_REQUEST_CONTEXT, AuthenticationError, RpcCaller, TypedRpcRegistry, canonical_json, decode_request,
    rpc_signing_bytes,
)

NOW = 1_800_000_000.0


def signed(ids, caller="ai_supervisor", epoch=None):
    return ids[caller].sign_request(server="trader", role="command", method="m", request_id=str(uuid.uuid4()),
                                    nonce=uuid.uuid4().hex, body={}, controller_epoch=epoch)


def test_the_key_is_always_signed():
    ids = make_identities(now=lambda: NOW)
    assert RPC_REQUEST_CONTEXT == b"mmr.typed-rpc.request.v3\x00"
    for epoch in (None, 3):
        covered = json.loads(rpc_signing_bytes(signed(ids, epoch=epoch))[len(RPC_REQUEST_CONTEXT):])
        assert "controller_epoch" in covered and covered["controller_epoch"] == epoch


def test_verify_hands_the_epoch_to_the_caller():
    ids = make_identities(now=lambda: NOW)
    caller = ids["trader"].verify_request(signed(ids, epoch=3), role="command")
    assert caller == RpcCaller("ai_supervisor", None, 3)
    assert RpcCaller("cli", None) == RpcCaller("cli", None, None)         # SP1 call sites unchanged


@pytest.mark.parametrize("altered", [4, None, 2])
def test_altered_epoch_breaks_the_signature(altered):                    # Review Focus 3
    ids = make_identities(now=lambda: NOW)
    request = signed(ids, epoch=3).model_copy(update={"controller_epoch": altered})
    with pytest.raises(AuthenticationError, match="signature mismatch"):
        ids["trader"].verify_request(request, role="command")


def test_adding_an_epoch_after_signing_breaks_the_signature():
    ids = make_identities(now=lambda: NOW)
    request = signed(ids).model_copy(update={"controller_epoch": 5})
    with pytest.raises(AuthenticationError, match="signature mismatch"):
        ids["trader"].verify_request(request, role="command")


@pytest.mark.parametrize("caller", ["cli", "dashboard", "ai_research", "strategy"])
def test_epoch_from_another_principal_is_refused(caller):               # Ruling 6
    ids = make_identities(now=lambda: NOW)
    with pytest.raises(AuthenticationError, match="controller_epoch is only accepted from ai_supervisor"):
        ids["trader"].verify_request(signed(ids, caller=caller, epoch=1), role="command")


@pytest.mark.parametrize("bad", [True, "3", 3.0, 0, -1, 2**53 + 1])
def test_epoch_on_the_wire_is_strict(bad):
    ids = make_identities(now=lambda: NOW)
    wire = signed(ids, epoch=3).model_dump(mode="json")
    wire["controller_epoch"] = bad
    with pytest.raises(AuthenticationError, match="malformed typed RPC request"):
        decode_request(canonical_json(wire))


def test_a_request_without_the_key_decodes_as_none():
    ids = make_identities(now=lambda: NOW)
    wire = signed(ids).model_dump(mode="json")
    del wire["controller_epoch"]
    assert decode_request(canonical_json(wire)).controller_epoch is None


def test_the_client_sends_the_epoch_and_the_handler_reads_it():
    ids = make_identities(now=time.time)
    registry = TypedRpcRegistry(acl=ALLOW_ALL)
    registry.register("command", "echo_epoch", dict, dict,
                      lambda body, caller: {"epoch": caller.controller_epoch}, with_caller=True)
    served = ServedStack({("trader", "command"): registry}, ids)
    try:
        client = served.client("ai_supervisor", "trader", "command")
        assert client.call("echo_epoch", {}, dict, controller_epoch=7) == {"epoch": 7}
        assert client.call("echo_epoch", {}, dict) == {"epoch": None}
        tampered = served.signed("ai_supervisor", role="command", method="echo_epoch", controller_epoch=7)
        tampered = tampered.model_copy(update={"controller_epoch": 8})
        assert served.raw_code(tampered) == "AUTHENTICATION_ERROR"
    finally:
        served.close()
