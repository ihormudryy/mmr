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
