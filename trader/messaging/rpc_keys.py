"""Strict loading of RPC identity keys into an in-memory keyring.

Keys live in ``~/.config/mmr/keys/rpc`` (``MMR_RPC_KEYS_DIR`` overrides it):
``<principal>.key`` is the Ed25519 private key (PKCS8 PEM, mode exactly
0600) and ``<principal>.pub`` its public key. They are read once at startup;
request handling only does dictionary lookups. Any missing, symlinked,
wrong-mode, malformed or duplicate key stops startup with an error that names
the file. Error messages never contain key bytes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from trader.messaging.principals import is_valid_principal_name, peers_for
from trader.research.key_purpose import (
    KeyPurposeError,
    bundle_public_raw,
    bundle_verify_dir_for,
    default_rpc_keys_dir,
    raw_public_bytes,
)

PRIVATE_SUFFIX = ".key"
PUBLIC_SUFFIX = ".pub"
PRIVATE_KEY_MODE = 0o600
PUBLIC_KEY_MODE = 0o644


class RpcKeyError(Exception):
    """An RPC key file is missing, unsafe or malformed, or a principal is unknown."""


def _checked_path(keys_dir: Path, principal: str, suffix: str, *, private: bool) -> Path:
    if not is_valid_principal_name(principal):
        raise RpcKeyError(f"unknown principal {principal!r}")
    path = Path(keys_dir) / f"{principal}{suffix}"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise RpcKeyError(f"{path} is missing; run `mmr keys init` on the host") from None
    if stat.S_ISLNK(info.st_mode):
        raise RpcKeyError(f"{path} is a symlink; RPC keys must be regular files")
    if not stat.S_ISREG(info.st_mode):
        raise RpcKeyError(f"{path} is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if private and mode != PRIVATE_KEY_MODE:
        raise RpcKeyError(f"{path} has mode {oct(mode)}; must be exactly 0o600")
    if not private and mode & 0o022:
        raise RpcKeyError(f"{path} is group/world writable ({oct(mode)})")
    return path


def load_rpc_private_key(keys_dir: Path, principal: str) -> Ed25519PrivateKey:
    path = _checked_path(keys_dir, principal, PRIVATE_SUFFIX, private=True)
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise RpcKeyError(f"{path} is not a valid private key PEM") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise RpcKeyError(f"{path} is not an Ed25519 private key")
    return key


def load_rpc_public_key(keys_dir: Path, principal: str) -> Ed25519PublicKey:
    path = _checked_path(keys_dir, principal, PUBLIC_SUFFIX, private=False)
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (ValueError, TypeError) as exc:
        raise RpcKeyError(f"{path} is not a valid public key PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise RpcKeyError(f"{path} is not an Ed25519 public key")
    return key


class RpcKeyring:
    """Immutable map of principal -> public key, built once at startup."""

    __slots__ = ("_keys",)

    def __init__(self, keys: Mapping[str, Ed25519PublicKey]):
        for principal, key in keys.items():
            if not is_valid_principal_name(principal):
                raise RpcKeyError(f"unknown principal {principal!r}")
            if not isinstance(key, Ed25519PublicKey):
                raise RpcKeyError(f"key for {principal!r} is not an Ed25519 public key")
        object.__setattr__(self, "_keys", MappingProxyType(dict(keys)))

    @classmethod
    def from_public_keys(cls, keys: Mapping[str, Ed25519PublicKey]) -> "RpcKeyring":
        return cls(keys)

    def get(self, principal: str) -> Ed25519PublicKey:
        try:
            return self._keys[principal]
        except (KeyError, TypeError):
            raise RpcKeyError("unknown principal") from None

    def principals(self) -> frozenset[str]:
        return frozenset(self._keys)

    def __setattr__(self, name, value):
        raise AttributeError("RpcKeyring is immutable")

    def __repr__(self) -> str:
        return f"<RpcKeyring principals={sorted(self._keys)}>"


def _refuse_shared_or_bundle_keys(keys_dir: Path, raw_by_principal: Mapping[str, bytes]) -> None:
    seen: dict[bytes, str] = {}
    for principal, raw in sorted(raw_by_principal.items()):
        other = seen.get(raw)
        if other is not None:
            raise RpcKeyError(
                f"{other} and {principal} have the same public key in {keys_dir}; "
                "each principal needs its own keypair (run `mmr keys init --rotate`)")
        seen[raw] = principal
    verify_dir = bundle_verify_dir_for(keys_dir)
    try:
        bundle_keys = bundle_public_raw(verify_dir)
    except KeyPurposeError as exc:
        raise RpcKeyError(str(exc)) from exc
    for raw, principal in seen.items():
        if raw in bundle_keys:
            raise RpcKeyError(
                f"the RPC key of {principal} is a bundle key in {verify_dir}; "
                "RPC keys and bundle-signing keys must be separate")


def load_identity_material(
    principal: str, keys_dir: Optional[Path] = None,
) -> tuple[Ed25519PrivateKey, RpcKeyring]:
    """Load ``principal``'s private key and the public keys of its peers."""
    if not is_valid_principal_name(principal):
        raise RpcKeyError(f"unknown principal {principal!r}")
    keys_dir = Path(keys_dir) if keys_dir is not None else default_rpc_keys_dir()
    private_key = load_rpc_private_key(keys_dir, principal)
    peer_keys = {peer: load_rpc_public_key(keys_dir, peer) for peer in sorted(peers_for(principal))}
    raw_by_principal = {peer: raw_public_bytes(key) for peer, key in peer_keys.items()}
    raw_by_principal[principal] = raw_public_bytes(private_key.public_key())
    _refuse_shared_or_bundle_keys(keys_dir, raw_by_principal)
    return private_key, RpcKeyring(peer_keys)
