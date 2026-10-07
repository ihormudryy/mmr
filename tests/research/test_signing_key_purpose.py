import os

import pytest

from trader.messaging.rpc_keys import init_keys
from trader.research.signing import (
    InvalidKeyType,
    MalformedKey,
    generate_private_key_pem,
    load_signing_key,
    load_verify_key,
)


def test_bundle_signer_refuses_an_rpc_private_key(tmp_path, monkeypatch):
    rpc = tmp_path / "rpc"
    init_keys(rpc)
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    with pytest.raises(InvalidKeyType, match="RPC"):
        load_signing_key(str(rpc / "ai_research.key"))


def test_bundle_verifier_refuses_an_rpc_public_key(tmp_path, monkeypatch):
    rpc = tmp_path / "rpc"
    init_keys(rpc)
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    with pytest.raises(InvalidKeyType, match="RPC"):
        load_verify_key(str(rpc / "trader.pub"))


def test_bundle_keys_still_load_when_no_rpc_dir_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "absent"))
    pem = tmp_path / "k.pem"
    pem.write_bytes(generate_private_key_pem())
    os.chmod(pem, 0o600)
    load_signing_key(str(pem))


def test_malformed_rpc_pub_surfaces_as_malformed_key(tmp_path, monkeypatch):
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    (rpc / "cli.pub").write_bytes(b"garbage")
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    pem = tmp_path / "k.pem"
    pem.write_bytes(generate_private_key_pem())
    os.chmod(pem, 0o600)
    with pytest.raises(MalformedKey, match="cli.pub"):
        load_signing_key(str(pem))


# --- PR #50 round 1, finding 2: every mounted RPC private key is refused ---

def _mirror_service_view(rpc_dir, files, keyset_dir):
    import shutil

    rpc_dir.mkdir(parents=True)
    for name in sorted(files):
        shutil.copy2(keyset_dir / name, rpc_dir / name)


def _signing_services():
    from trader.messaging.principals import SERVICE_PRINCIPAL

    return sorted(name for name, principal in SERVICE_PRINCIPAL.items() if principal is not None)


@pytest.mark.parametrize("service", _signing_services())
@pytest.mark.parametrize("with_own_pub", [True, False], ids=["compose-layout", "own-pub-not-mounted"])
def test_every_rpc_private_key_a_container_sees_is_refused_as_a_bundle_key(
        tmp_path, monkeypatch, service, with_own_pub):
    import shutil

    from tests.compose_rpc_helpers import load_compose, visible_rpc_files
    from trader.messaging.principals import SERVICE_PRINCIPAL

    keyset = tmp_path / "host_rpc"
    init_keys(keyset)
    files = visible_rpc_files(load_compose()["services"][service])
    if not with_own_pub:
        files = files - {f"{SERVICE_PRINCIPAL[service]}.pub"}
    container_rpc = tmp_path / "container" / "rpc"
    _mirror_service_view(container_rpc, files, keyset)
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(container_rpc))

    private_keys = sorted(name for name in files if name.endswith(".key"))
    assert private_keys, service
    for name in private_keys:
        with pytest.raises(InvalidKeyType, match="RPC"):
            load_signing_key(str(container_rpc / name))
        copied = tmp_path / "container" / "private" / f"bundle_{name}.pem"
        copied.parent.mkdir(exist_ok=True)
        shutil.copy2(container_rpc / name, copied)
        with pytest.raises(InvalidKeyType, match="RPC"):
            load_signing_key(str(copied))


def test_public_half_of_an_unpaired_rpc_private_key_is_refused_as_a_verify_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization

    keyset = tmp_path / "host_rpc"
    init_keys(keyset)
    rpc = tmp_path / "rpc"
    _mirror_service_view(rpc, {"trader.key"}, keyset)
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    derived = serialization.load_pem_private_key(
        (rpc / "trader.key").read_bytes(), password=None).public_key()
    pub = tmp_path / "verify.pem"
    pub.write_bytes(derived.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(InvalidKeyType, match="RPC"):
        load_verify_key(str(pub))


def test_unreadable_rpc_private_key_stops_the_bundle_loader(tmp_path, monkeypatch):
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    (rpc / "trader.key").write_bytes(b"garbage")
    os.chmod(rpc / "trader.key", 0o600)
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(rpc))
    pem = tmp_path / "k.pem"
    pem.write_bytes(generate_private_key_pem())
    os.chmod(pem, 0o600)
    with pytest.raises(MalformedKey, match="trader.key"):
        load_signing_key(str(pem))
