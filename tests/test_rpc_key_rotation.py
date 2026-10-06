"""Owner answer 7: rotation is a coordinated switch on every server that trusts the principal."""
import pytest

from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack
from trader.messaging.principals import KNOWN_PRINCIPALS, SERVER_ACCEPTS, SERVER_PRINCIPALS
from trader.messaging.rpc_keys import init_keys
from trader.messaging.typed_rpc import AuthenticationError, ServiceIdentity, TypedRpcRegistry


def _start_server(server, keys):
    """A fresh process-equivalent: the server reloads its keyring from disk."""
    registry = TypedRpcRegistry(acl=ALLOW_ALL)
    registry.register("query", "ping", dict, dict, lambda body: {"pong": True})
    return ServedStack({(server, "query"): registry}, {server: ServiceIdentity.load(server, keys)})


def _code(stack, signer, server):
    return stack.raw_code(stack.signed(None, server, "query", "ping", identity=signer))


@pytest.mark.parametrize("rotated", sorted(KNOWN_PRINCIPALS))
def test_rotating_one_principal_flips_trust_on_every_server_that_trusts_it(tmp_path, rotated):
    keys = tmp_path / "rpc"
    init_keys(keys)
    old_signer = ServiceIdentity.load(rotated, keys)
    init_keys(keys, rotate=rotated)
    new_signer = ServiceIdentity.load(rotated, keys)
    for server in sorted(SERVER_PRINCIPALS):
        stack = _start_server(server, keys)
        try:
            if rotated in SERVER_ACCEPTS[server]:
                assert _code(stack, old_signer, server) == "AUTHENTICATION_ERROR"
                assert _code(stack, new_signer, server) == "OK"
            else:
                assert rotated not in stack.servers[(server, "query")].identity.trusted_principals()
        finally:
            stack.close()


@pytest.mark.parametrize("rotated", sorted(SERVER_PRINCIPALS))
def test_clients_must_reload_after_a_server_key_rotates(tmp_path, rotated):
    keys = tmp_path / "rpc"
    init_keys(keys)
    stale_client = ServiceIdentity.load("cli", keys)       # holds the old server .pub
    init_keys(keys, rotate=rotated)
    stack = _start_server(rotated, keys)
    try:
        with pytest.raises(AuthenticationError):
            stack.client("cli", rotated, "query", identity=stale_client).call("ping", {}, dict)
        fresh_client = ServiceIdentity.load("cli", keys)
        assert stack.client("cli", rotated, "query", identity=fresh_client).call("ping", {}, dict) \
            == {"pong": True}
    finally:
        stack.close()


def test_running_server_keeps_the_old_keyring_until_restarted(tmp_path):
    keys = tmp_path / "rpc"
    init_keys(keys)
    old_cli = ServiceIdentity.load("cli", keys)
    stack = _start_server("trader", keys)
    try:
        init_keys(keys, rotate="cli")
        # Why every listed service must restart: a running server still trusts the old key.
        assert _code(stack, old_cli, "trader") == "OK"
        assert _code(stack, ServiceIdentity.load("cli", keys), "trader") == "AUTHENTICATION_ERROR"
    finally:
        stack.close()
