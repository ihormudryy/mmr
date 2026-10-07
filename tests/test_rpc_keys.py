import os

import pytest

from tests.rpc_identity_fixtures import write_keyset
from trader.messaging import principals
from trader.messaging.rpc_keys import RpcKeyError, load_identity_material, load_rpc_private_key
from trader.research import key_purpose


def test_default_rpc_keys_dir_is_isolated_in_tests():
    assert str(key_purpose.default_rpc_keys_dir()).startswith(os.environ["MMR_RPC_KEYS_DIR"])
    assert ".config/mmr" not in str(key_purpose.default_rpc_keys_dir())


def test_peers_follow_the_trust_matrix():
    assert principals.peers_for("trader") == {"cli", "dashboard", "strategy", "ai_supervisor", "ai_research"}
    assert principals.peers_for("strategy") == {"cli", "dashboard", "trader"}
    assert principals.peers_for("ai_research") == {"trader"}
    for reserved in ("telegram_bridge", "scheduler"):
        with pytest.raises(ValueError):
            principals.peers_for(reserved)


@pytest.mark.parametrize("name", ["../verify/x", "/etc/passwd", "CLI", "cli\x00", "", "telegram_bridge",
                                  "scheduler", None, 7])
def test_invalid_principal_names(name):
    assert not principals.is_valid_principal_name(name)


def test_loads_own_key_and_peer_keyring(tmp_path):
    write_keyset(tmp_path)
    _private, keyring = load_identity_material("strategy", tmp_path)
    assert keyring.principals() == {"cli", "dashboard", "trader"}
    with pytest.raises(RpcKeyError):
        keyring.get("ai_supervisor")


def test_missing_private_key_names_file_and_command(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "trader.key").unlink()
    with pytest.raises(RpcKeyError, match=r"trader\.key.*mmr keys init"):
        load_identity_material("trader", tmp_path)


def test_missing_peer_public_key_fails(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "ai_research.pub").unlink()
    with pytest.raises(RpcKeyError, match="ai_research.pub"):
        load_identity_material("trader", tmp_path)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o400, 0o700])
def test_private_key_mode_must_be_exactly_0600(tmp_path, mode):
    write_keyset(tmp_path)
    os.chmod(tmp_path / "cli.key", mode)
    with pytest.raises(RpcKeyError, match="0o600"):
        load_rpc_private_key(tmp_path, "cli")


def test_public_key_writable_by_group_is_refused(tmp_path):
    write_keyset(tmp_path)
    os.chmod(tmp_path / "trader.pub", 0o664)
    with pytest.raises(RpcKeyError, match="writable"):
        load_identity_material("cli", tmp_path)


def test_symlinked_key_files_are_refused(tmp_path):
    write_keyset(tmp_path)
    real = tmp_path / "real.key"
    (tmp_path / "cli.key").rename(real)
    os.symlink(real, tmp_path / "cli.key")
    with pytest.raises(RpcKeyError, match="symlink"):
        load_rpc_private_key(tmp_path, "cli")


def test_directory_in_place_of_a_key_is_refused(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "cli.key").unlink()
    (tmp_path / "cli.key").mkdir()
    with pytest.raises(RpcKeyError, match="not a regular file"):
        load_rpc_private_key(tmp_path, "cli")


def test_principal_outside_known_set_never_builds_a_path(tmp_path):
    with pytest.raises(RpcKeyError, match="unknown principal"):
        load_rpc_private_key(tmp_path, "../verify/paper-automation")


def test_two_principals_sharing_a_key_is_refused(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "ai_research.pub").write_bytes((tmp_path / "cli.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="same public key"):
        load_identity_material("trader", tmp_path)


def test_own_key_equal_to_a_peer_key_is_refused(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "cli.pub").write_bytes((tmp_path / "trader.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="same public key"):
        load_identity_material("trader", tmp_path)


def test_bundle_verify_key_is_refused_as_rpc_key(tmp_path):
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    write_keyset(rpc)
    verify = tmp_path / "verify"
    verify.mkdir()
    (verify / "paper-automation.pem").write_bytes((rpc / "dashboard.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="bundle"):
        load_identity_material("trader", rpc)


def test_malformed_public_key_fails(tmp_path):
    write_keyset(tmp_path)
    (tmp_path / "trader.pub").write_bytes(b"garbage")
    with pytest.raises(RpcKeyError, match="trader.pub"):
        load_identity_material("cli", tmp_path)


def test_error_messages_never_contain_key_bytes(tmp_path):
    write_keyset(tmp_path)
    secret = (tmp_path / "cli.key").read_bytes()
    os.chmod(tmp_path / "cli.key", 0o644)
    with pytest.raises(RpcKeyError) as exc:
        load_rpc_private_key(tmp_path, "cli")
    assert b"PRIVATE KEY" not in str(exc.value).encode() and secret not in str(exc.value).encode()


# --- PR #50 round 1, finding 3: the own key pair is checked at startup ---

def test_missing_own_public_key_fails_before_serving(tmp_path):
    from trader.messaging.typed_rpc import ServiceIdentity

    write_keyset(tmp_path)
    (tmp_path / "trader.pub").unlink()
    with pytest.raises(RpcKeyError, match="trader.pub is missing"):
        load_identity_material("trader", tmp_path)
    with pytest.raises(RpcKeyError, match="trader.pub is missing"):
        ServiceIdentity.load("trader", tmp_path)


def test_own_public_key_of_another_keypair_fails_before_serving(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from trader.messaging.typed_rpc import ServiceIdentity

    write_keyset(tmp_path)
    stranger = Ed25519PrivateKey.generate().public_key()
    (tmp_path / "strategy.pub").write_bytes(stranger.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(RpcKeyError, match="does not match"):
        load_identity_material("strategy", tmp_path)
    with pytest.raises(RpcKeyError, match="does not match"):
        ServiceIdentity.load("strategy", tmp_path)


def test_own_public_key_is_checked_but_never_trusted_as_a_caller(tmp_path):
    write_keyset(tmp_path)
    _private, keyring = load_identity_material("dashboard", tmp_path)
    assert "dashboard" not in keyring.principals()
