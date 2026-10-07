"""Split-service round trips over every trust-matrix edge (spec 5.3 cutover gate).

Real key files (`init_keys` into a temp dir), real loaders
(`ServiceIdentity.load`), the real production trader registry and the real
strategy-control registration, served on 127.0.0.1 over ZMQ.
"""
import builtins
import io
import os
import pathlib
import uuid
from unittest.mock import MagicMock

import pytest

from tests.rpc_identity_fixtures import (
    RecordingCoordinator, ServedStack, build_full_production_registry, legacy_hmac_envelope_bytes,
)
from trader.messaging.principals import STRATEGY_ACL, TRADER_ACL
from trader.messaging.rpc_keys import RpcKeyError, init_keys
from trader.messaging.typed_rpc import (
    ServiceIdentity, TypedRpcRegistry, TypedRpcRequest, rpc_signing_bytes,
)
from trader.research.signing import AttestationSigner
from trader.strategy.strategy_runtime import register_strategy_control_authority

REFUSED = {"AUTHENTICATION_ERROR", "REPLAY_ERROR", "PERMISSION_DENIED", "METHOD_NOT_ALLOWED"}
CALLERS = ("trader", "strategy", "cli", "dashboard", "ai_supervisor", "ai_research")


def _stub_registries(acl):
    by_role = {role: TypedRpcRegistry(acl=acl) for role in ("query", "command", "feed")}
    for role, method in acl:
        by_role[role].register(role, method, dict, dict, lambda body: {"reached": True})
    return by_role


@pytest.fixture(scope="module")
def keys_dir(tmp_path_factory):
    keys = tmp_path_factory.mktemp("rpc_keys")
    init_keys(keys)
    return keys


def _identities(keys_dir):
    return {p: ServiceIdentity.load(p, keys_dir) for p in CALLERS}


@pytest.fixture
def stack(keys_dir):
    """Production registries (real handlers on stubs) for raw checks."""
    ids = _identities(keys_dir)
    trader_registry = build_full_production_registry(ids["trader"], coordinator=RecordingCoordinator())
    strategy_command = TypedRpcRegistry(acl=STRATEGY_ACL)
    strategy_query = TypedRpcRegistry(acl=STRATEGY_ACL)
    register_strategy_control_authority(strategy_command, strategy_query, MagicMock())
    registries = {("trader", role): trader_registry for role in ("query", "command", "feed")}
    registries[("strategy", "command")] = strategy_command
    registries[("strategy", "query")] = strategy_query
    served = ServedStack(registries, ids)
    yield served
    served.close()


@pytest.fixture
def stub_stack(keys_dir):
    """Same tables, stub handlers, so a full client round trip succeeds."""
    ids = _identities(keys_dir)
    trader = _stub_registries(TRADER_ACL)
    strategy = _stub_registries(STRATEGY_ACL)
    registries = {("trader", role): reg for role, reg in trader.items()}
    registries.update({("strategy", role): strategy[role] for role in ("query", "command")})
    served = ServedStack(registries, ids)
    yield served
    served.close()


EDGES = [  # (caller, server, role, method) -- one allowed call per trust-matrix edge
    ("cli", "trader", "query", "get_status"),
    ("cli", "strategy", "query", "list_strategies"),
    ("dashboard", "trader", "query", "get_positions"),
    ("dashboard", "strategy", "command", "enable_strategy_by_name"),
    ("cli", "strategy", "command", "enable_strategy_by_name"),
    ("dashboard", "trader", "command", "deactivate_live_canary"),
    ("strategy", "trader", "query", "resolve_instrument"),
    ("strategy", "trader", "command", "create_proposal"),
    ("strategy", "trader", "feed", "read_domain_events"),
    ("trader", "strategy", "command", "enable_strategy"),
    ("trader", "strategy", "query", "get_paper_automation_arm"),
    ("ai_supervisor", "trader", "query", "get_account_values"),
    ("ai_supervisor", "trader", "command", "pause_trading"),
    ("ai_research", "trader", "query", "get_snapshot"),
]


@pytest.mark.parametrize("caller,server,role,method", EDGES)
def test_every_trust_matrix_edge_round_trips(stub_stack, caller, server, role, method):
    assert stub_stack.client(caller, server, role).call(method, {}, dict) == {"reached": True}


@pytest.mark.parametrize("caller,server,role,method", EDGES)
def test_every_edge_reaches_the_production_handler(stack, caller, server, role, method):
    body = {"source": "strategy:matrix"} if method == "create_proposal" else {}
    assert stack.raw_code(stack.signed(caller, server, role, method, body)) not in REFUSED


NON_EDGES = [("ai_supervisor", "strategy"), ("ai_research", "strategy"), ("strategy", "strategy"),
             ("trader", "trader")]


@pytest.mark.parametrize("caller,server", NON_EDGES)
def test_non_edges_are_refused(stack, caller, server):
    method = "list_strategies" if server == "strategy" else "get_status"
    assert stack.raw_code(stack.signed(caller, server, "query", method)) == "AUTHENTICATION_ERROR"


def _ok_method(server):
    return "list_strategies" if server == "strategy" else "get_status"


@pytest.mark.parametrize("server", ["trader", "strategy"])
class TestEachServerRefuses:
    def test_wrong_principal(self, stack, server):
        req = stack.signed("dashboard", server, "query", _ok_method(server), claim="cli")
        assert stack.raw_code(req) == "AUTHENTICATION_ERROR"

    def test_tampered(self, stack, server):
        req = stack.signed("cli", server, "query", _ok_method(server), tamper_body=True)
        assert stack.raw_code(req) == "AUTHENTICATION_ERROR"

    def test_replayed(self, stack, server):
        req = stack.signed("cli", server, "query", _ok_method(server))
        assert stack.raw_code(req) not in REFUSED
        assert stack.raw_code(req) == "REPLAY_ERROR"

    def test_wrong_destination(self, stack, server):
        other = "strategy" if server == "trader" else "trader"
        req = stack.signed("cli", other, "query", _ok_method(server))
        assert stack.raw_code(req, to=server, role="query") == "AUTHENTICATION_ERROR"

    def test_legacy_hmac(self, stack, server):
        reply = stack.send_raw(legacy_hmac_envelope_bytes(), to=server, role="query")
        assert reply.problem.code == "AUTHENTICATION_ERROR" and reply.request_digest == ""


def test_scheduler_has_no_identity(keys_dir):
    with pytest.raises(RpcKeyError):
        ServiceIdentity.load("scheduler", keys_dir)


def test_dashboard_cannot_activate_but_cli_reaches_the_handler(stack):
    assert stack.raw_code(stack.signed("dashboard", "trader", "command", "activate_live_canary")) \
        == "PERMISSION_DENIED"
    assert stack.raw_code(stack.signed("cli", "trader", "command", "activate_live_canary")) not in REFUSED


def test_forward_from_trader_carries_the_original_caller_for_the_log_only(keys_dir):
    ids = _identities(keys_dir)
    seen = []
    command = TypedRpcRegistry(acl=STRATEGY_ACL)
    command.register("command", "enable_strategy", dict, dict,
                     lambda body, caller: seen.append(caller) or {}, with_caller=True)
    served = ServedStack({("strategy", "command"): command}, ids)
    try:
        served.client("trader", "strategy", "command").call(
            "enable_strategy", {}, dict, on_behalf_of="dashboard")
        assert [(c.principal, c.on_behalf_of) for c in seen] == [("trader", "dashboard")]
        req = served.signed("dashboard", "strategy", "command", "enable_strategy",
                            on_behalf_of="cli")
        assert served.raw_code(req) == "AUTHENTICATION_ERROR"
    finally:
        served.close()


def test_bundle_key_cannot_sign_rpc(stack):
    signer = AttestationSigner.generate()
    unsigned = TypedRpcRequest(
        method="get_status", request_id=str(uuid.uuid4()), timestamp=__import__("time").time(),
        nonce=uuid.uuid4().hex, body={}, principal="cli", server="trader", role="query",
        on_behalf_of=None, signature="")
    forged = unsigned.model_copy(update={"signature": signer.sign_message(rpc_signing_bytes(unsigned))})
    assert stack.raw_code(forged) == "AUTHENTICATION_ERROR"


def test_no_code_path_opens_the_retired_hmac_key(tmp_path, monkeypatch):
    keys = tmp_path / "config/mmr/keys/rpc"
    init_keys(keys)
    hmac_file = tmp_path / "config/mmr/service_hmac.key"
    hmac_file.write_bytes(b"k" * 48)
    os.chmod(hmac_file, 0o600)
    yaml_file = tmp_path / "config/mmr/trader.yaml"
    yaml_file.write_text(f"service_hmac_key_file: {hmac_file}\ntyped_query_port: 42101\n")
    monkeypatch.setenv("MMR_SERVICE_HMAC_KEY_FILE", str(hmac_file))

    def _guard(real):
        def _wrapped(path, *args, **kwargs):
            if "service_hmac" in os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else False:
                pytest.fail(f"opened the retired HMAC key: {path}")
            return real(path, *args, **kwargs)
        return _wrapped

    monkeypatch.setattr(builtins, "open", _guard(builtins.open))
    monkeypatch.setattr(io, "open", _guard(io.open))
    monkeypatch.setattr(os, "open", _guard(os.open))
    real_path_open = pathlib.Path.open
    monkeypatch.setattr(pathlib.Path, "open",
                        lambda self, *a, **k: _guard(lambda p, *a2, **k2: real_path_open(self, *a2, **k2))(
                            self, *a, **k))

    from trader.config import MMRConfig
    MMRConfig.from_yaml(str(yaml_file))
    ids = {p: ServiceIdentity.load(p, keys) for p in ("trader", "cli")}
    registry = TypedRpcRegistry(acl={("query", "ping"): frozenset({"cli"})})
    registry.register("query", "ping", dict, dict, lambda body: {"pong": True})
    served = ServedStack({("trader", "query"): registry}, ids)
    try:
        assert served.client("cli", "trader", "query").call("ping", {}, dict) == {"pong": True}
    finally:
        served.close()
