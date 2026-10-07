"""Where RPC identity keys live, and the raw public bytes of RPC and bundle keys.

Imports only the standard library and ``cryptography`` so that
``trader.research.signing`` can use it without an import cycle. It lets the
bundle loaders refuse an RPC key and the RPC loaders refuse a bundle key
(spec 5.3: a bundle key is refused as an RPC key and the other way round).
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

RPC_KEYS_DIR_ENV = "MMR_RPC_KEYS_DIR"


class KeyPurposeError(Exception):
    """A key file in a key directory could not be read as an Ed25519 public key."""


def default_rpc_keys_dir() -> Path:
    override = os.environ.get(RPC_KEYS_DIR_ENV, "")
    if override:
        return Path(override)
    return Path.home() / ".config" / "mmr" / "keys" / "rpc"


def bundle_verify_dir_for(rpc_dir: Path) -> Path:
    return Path(rpc_dir).parent / "verify"


def raw_public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _raw_public_keys_in(directory: Path, pattern: str) -> frozenset[bytes]:
    directory = Path(directory)
    if not directory.is_dir():
        return frozenset()
    found = set()
    for path in sorted(directory.glob(pattern)):
        try:
            key = serialization.load_pem_public_key(path.read_bytes())
        except (ValueError, TypeError, OSError) as exc:
            raise KeyPurposeError(f"{path} is not a readable public key PEM") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise KeyPurposeError(f"{path} is not an Ed25519 public key")
        found.add(raw_public_bytes(key))
    return frozenset(found)


def rpc_public_raw(rpc_dir: Path) -> frozenset[bytes]:
    return _raw_public_keys_in(rpc_dir, "*.pub")


def rpc_private_derived_raw(rpc_dir: Path) -> frozenset[bytes]:
    """Public halves derived from every ``*.key`` in ``rpc_dir``.

    A container may hold its own ``.key`` without the matching ``.pub``, so
    the bundle loaders cannot rely on the ``.pub`` files alone.
    """
    rpc_dir = Path(rpc_dir)
    if not rpc_dir.is_dir():
        return frozenset()
    found = set()
    for path in sorted(rpc_dir.glob("*.key")):
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (ValueError, TypeError, OSError) as exc:
            raise KeyPurposeError(f"{path} is not a readable private key PEM") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise KeyPurposeError(f"{path} is not an Ed25519 private key")
        found.add(raw_public_bytes(key.public_key()))
    return frozenset(found)


def rpc_identity_raw(rpc_dir: Path) -> frozenset[bytes]:
    """Every RPC public key visible in ``rpc_dir``: the ``.pub`` files and those derived from ``.key`` files."""
    return rpc_public_raw(rpc_dir) | rpc_private_derived_raw(rpc_dir)


def bundle_public_raw(verify_dir: Path) -> frozenset[bytes]:
    return _raw_public_keys_in(verify_dir, "*.pem")
