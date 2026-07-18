"""P2 Task 7 -- strict private-key hygiene for the Ed25519 signing core.

These tests are the security floor for ``trader/research/signing.py``. They pin
the promises that make it safe to hold a signing key:

* an over-shared key file (any group/world permission bit) is REFUSED before it
  is even read;
* only a PKCS8 PEM Ed25519 private key loads -- a wrong algorithm or garbage
  bytes fail loud;
* the signer never leaks private material in ``repr``/``str``;
* nothing the module produces or persists contains private key bytes.
"""
from __future__ import annotations

import os
import stat

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from trader.research import signing
from trader.research.signing import (
    AttestationSigner,
    InsecureKeyFile,
    InvalidKeyType,
    MalformedKey,
    generate_private_key_pem,
    load_signing_key,
    load_verify_key,
    public_key_id,
    public_key_pem,
)


def _write_key(tmp_path, name, data: bytes, mode: int):
    path = tmp_path / name
    path.write_bytes(data)
    os.chmod(path, mode)
    return str(path)


# --------------------------------------------------------------------------- #
# Permission hygiene -- refuse group/world access, accept owner-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660, 0o606, 0o666, 0o755])
def test_load_signing_key_refuses_group_or_world_accessible(tmp_path, mode):
    pem = generate_private_key_pem()
    path = _write_key(tmp_path, "insecure.pem", pem, mode)
    with pytest.raises(InsecureKeyFile):
        load_signing_key(path)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_load_signing_key_accepts_owner_only(tmp_path, mode):
    pem = generate_private_key_pem()
    path = _write_key(tmp_path, "secure.pem", pem, mode)
    key = load_signing_key(path)
    assert isinstance(key, ed25519.Ed25519PrivateKey)


def test_insecure_file_is_not_read_before_permission_check(tmp_path, monkeypatch):
    # The permission check must happen BEFORE any open() -- prove it by making
    # open() explode and asserting InsecureKeyFile still wins.
    pem = generate_private_key_pem()
    path = _write_key(tmp_path, "insecure.pem", pem, 0o644)

    import builtins
    real_open = builtins.open

    def _boom(*a, **k):
        raise AssertionError("file was opened before the permission check")

    monkeypatch.setattr(builtins, "open", _boom)
    with pytest.raises(InsecureKeyFile):
        load_signing_key(path)
    # sanity: real open still works for the secure case
    monkeypatch.setattr(builtins, "open", real_open)


# --------------------------------------------------------------------------- #
# PKCS8 PEM only -- reject wrong algorithm + garbage
# --------------------------------------------------------------------------- #
def test_rejects_non_ed25519_pkcs8_key(tmp_path):
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    path = _write_key(tmp_path, "rsa.pem", pem, 0o600)
    with pytest.raises(InvalidKeyType):
        load_signing_key(path)


def test_rejects_garbage_non_pem_file(tmp_path):
    path = _write_key(tmp_path, "garbage.pem", b"this is not a PEM key at all\n", 0o600)
    with pytest.raises(MalformedKey):
        load_signing_key(path)


def test_load_verify_key_rejects_non_ed25519_public(tmp_path):
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    path = tmp_path / "rsa_pub.pem"
    path.write_bytes(pem)
    with pytest.raises(InvalidKeyType):
        load_verify_key(str(path))


def test_load_verify_key_round_trips_and_matches_id(tmp_path):
    signer = AttestationSigner.generate()
    pub_path = tmp_path / "pub.pem"
    pub_path.write_bytes(signer.public_key_pem())
    loaded = load_verify_key(str(pub_path))
    assert public_key_id(loaded) == signer.public_key_id


# --------------------------------------------------------------------------- #
# No private material ever leaks from the signer
# --------------------------------------------------------------------------- #
def _raw_private_hex(signer_pem: bytes) -> str:
    key = serialization.load_pem_private_key(signer_pem, password=None)
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    return raw.hex()


def test_signer_repr_and_str_redact_private_key():
    pem = generate_private_key_pem()
    key = serialization.load_pem_private_key(pem, password=None)
    raw_hex = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()).hex()
    signer = AttestationSigner(key)

    for text in (repr(signer), str(signer), f"{signer}"):
        assert "redacted" in text
        assert signer.public_key_id in text
        assert raw_hex not in text
        # also the base64 forms should not appear
        import base64
        raw = bytes.fromhex(raw_hex)
        assert base64.b64encode(raw).decode() not in text
        assert base64.urlsafe_b64encode(raw).decode() not in text


def test_signer_has_no_public_private_attribute():
    signer = AttestationSigner.generate()
    # slotted + name-mangled: no __dict__, no plain private attribute
    assert not hasattr(signer, "__dict__")
    assert not hasattr(signer, "private_key")
    assert not hasattr(signer, "_private_key")


def test_generated_pem_is_pkcs8_and_public_pem_is_spki():
    pem = generate_private_key_pem()
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    pub = public_key_pem(serialization.load_pem_private_key(pem, password=None))
    assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")
    # the public PEM must not contain the private bytes
    assert _raw_private_hex(pem) not in pub.hex()
