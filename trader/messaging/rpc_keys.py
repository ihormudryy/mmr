"""Strict loading of RPC identity keys into an in-memory keyring.

Keys live in ``~/.config/mmr/keys/rpc`` (``MMR_RPC_KEYS_DIR`` overrides it):
``<principal>.key`` is the Ed25519 private key (PKCS8 PEM, mode exactly
0600) and ``<principal>.pub`` its public key. They are read once at startup;
request handling only does dictionary lookups. Any missing, symlinked,
wrong-mode, malformed or duplicate key stops startup with an error that names
the file. Error messages never contain key bytes.
"""

from __future__ import annotations

import io
import os
import secrets
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from trader.messaging.principals import KNOWN_PRINCIPALS, is_valid_principal_name, peers_for
from trader.research.key_purpose import (
    KeyPurposeError,
    bundle_public_raw,
    bundle_verify_dir_for,
    default_rpc_keys_dir,
    raw_public_bytes,
)
from trader.research.signing import generate_private_key_pem, public_key_id, public_key_pem

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
    """Load ``principal``'s private key and the public keys of its peers.

    The own ``.pub`` must exist and match the private key, so a half-done
    rotation or a missing mount stops startup instead of serving. It is
    checked, never added to the keyring: a server does not accept itself.
    """
    if not is_valid_principal_name(principal):
        raise RpcKeyError(f"unknown principal {principal!r}")
    keys_dir = Path(keys_dir) if keys_dir is not None else default_rpc_keys_dir()
    private_key = _load_matching_pair(keys_dir, principal)
    peer_keys = {peer: load_rpc_public_key(keys_dir, peer) for peer in sorted(peers_for(principal))}
    raw_by_principal = {peer: raw_public_bytes(key) for peer, key in peer_keys.items()}
    raw_by_principal[principal] = raw_public_bytes(private_key.public_key())
    _refuse_shared_or_bundle_keys(keys_dir, raw_by_principal)
    return private_key, RpcKeyring(peer_keys)


# ---------------------------------------------------------------------------
# Key generation and rotation (`mmr keys init`)
# ---------------------------------------------------------------------------

# Long-lived compose services and the principals each signs as. A rotated
# principal's key lives in its own service and in every service that has it
# as a peer; all of them must restart together (owner answer 7).
_LONG_LIVED_SERVICE_PRINCIPALS: Mapping[str, tuple[str, ...]] = {
    "trader": ("trader",), "strategy": ("strategy",), "dashboard": ("dashboard",),
    "ai": ("ai_supervisor", "ai_research"), "research": ("research",),
}


def _services_holding(principal: str) -> tuple[str, ...]:
    return tuple(sorted(
        service for service, owns in _LONG_LIVED_SERVICE_PRINCIPALS.items()
        if principal in owns or any(principal in peers_for(own) for own in owns)
    ))


RESTART_ON_ROTATE: Mapping[str, tuple[str, ...]] = MappingProxyType({
    principal: _services_holding(principal) for principal in sorted(KNOWN_PRINCIPALS)
})


@dataclass(frozen=True)
class KeyInitRow:
    principal: str
    status: Literal["created", "kept", "rotated"]
    key_id: str


def _stage_file(path: Path, data: bytes, mode: int) -> Path:
    """Write ``data`` to a fresh fsynced temp file next to ``path`` and return its path."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _new_keypair_bytes() -> tuple[bytes, bytes, Ed25519PrivateKey]:
    private_pem = generate_private_key_pem()
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    return private_pem, public_key_pem(private_key), private_key


def _install_keypair(keys_dir: Path, principal: str) -> str:
    """Write both new files to temp names first, then rename ``.pub`` and ``.key``.

    Two renames cannot be one atomic step. A crash before the first rename
    leaves the old pair untouched. A crash between them leaves a new ``.pub``
    with the old ``.key``; every loader then refuses the pair ("does not
    match") and ``--rotate`` again repairs it. The private key is renamed last
    so the old secret stays in place until the new public half is on disk.
    """
    private_pem, public_pem, private_key = _new_keypair_bytes()
    private_path = keys_dir / f"{principal}{PRIVATE_SUFFIX}"
    public_path = keys_dir / f"{principal}{PUBLIC_SUFFIX}"
    staged: list[Path] = []
    try:
        staged_public = _stage_file(public_path, public_pem, PUBLIC_KEY_MODE)
        staged.append(staged_public)
        staged_private = _stage_file(private_path, private_pem, PRIVATE_KEY_MODE)
        staged.append(staged_private)
        os.replace(staged_public, public_path)
        os.replace(staged_private, private_path)
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
    return public_key_id(private_key.public_key())


def _load_matching_pair(keys_dir: Path, principal: str) -> Ed25519PrivateKey:
    private_key = load_rpc_private_key(keys_dir, principal)
    public_key = load_rpc_public_key(keys_dir, principal)
    if raw_public_bytes(private_key.public_key()) != raw_public_bytes(public_key):
        raise RpcKeyError(
            f"{keys_dir / (principal + PUBLIC_SUFFIX)} does not match "
            f"{keys_dir / (principal + PRIVATE_SUFFIX)}; rotate {principal} to replace both")
    return private_key


def _existing_pair_key_id(keys_dir: Path, principal: str) -> str:
    return public_key_id(_load_matching_pair(keys_dir, principal).public_key())


def init_keys(keys_dir: Path, *, rotate: Optional[str] = None) -> list[KeyInitRow]:
    """Create every missing keypair; never overwrite. ``rotate`` replaces one pair."""
    if rotate is not None and not is_valid_principal_name(rotate):
        raise RpcKeyError(f"unknown principal {rotate!r}; cannot rotate it")
    keys_dir = Path(keys_dir)
    keys_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    rows = []
    for principal in sorted(KNOWN_PRINCIPALS):
        if principal == rotate:
            rows.append(KeyInitRow(principal, "rotated", _install_keypair(keys_dir, principal)))
            continue
        has_private = os.path.lexists(keys_dir / f"{principal}{PRIVATE_SUFFIX}")
        has_public = os.path.lexists(keys_dir / f"{principal}{PUBLIC_SUFFIX}")
        if has_private and has_public:
            rows.append(KeyInitRow(principal, "kept", _existing_pair_key_id(keys_dir, principal)))
        elif not has_private and not has_public:
            rows.append(KeyInitRow(principal, "created", _install_keypair(keys_dir, principal)))
        else:
            missing = PUBLIC_SUFFIX if has_private else PRIVATE_SUFFIX
            raise RpcKeyError(
                f"{keys_dir / (principal + missing)} is missing but its pair exists; "
                f"restore it from the backup or run `mmr keys init --rotate {principal}`")
    return rows


# ---------------------------------------------------------------------------
# Encrypted backup and restore (`mmr keys backup|restore`, owner answer 9)
# ---------------------------------------------------------------------------

AgeRunner = Callable[..., bytes]


def _run_age(argv: list[str], stdin_bytes: bytes, *, identity: Optional[bytes] = None) -> bytes:
    """Run ``age``; the identity, if any, goes through a pipe, never argv or disk."""
    binary = shutil.which(argv[0])
    if binary is None:
        raise RpcKeyError("the `age` binary is not installed; it is needed for RPC key backups")
    pass_fds: tuple[int, ...] = ()
    read_fd = write_fd = None
    command = [binary, *argv[1:]]
    if identity is not None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, identity)
        os.close(write_fd)
        pass_fds = (read_fd,)
        command += ["-i", f"/dev/fd/{read_fd}"]
    try:
        result = subprocess.run(command, input=stdin_bytes, capture_output=True,
                                pass_fds=pass_fds, check=False)
    finally:
        if read_fd is not None:
            os.close(read_fd)
    if result.returncode != 0:
        raise RpcKeyError(f"`age` failed with exit code {result.returncode}")
    return result.stdout


def _require_age(run: Optional[AgeRunner]) -> AgeRunner:
    if run is not None:
        return run
    if shutil.which("age") is None:
        raise RpcKeyError("the `age` binary is not installed; it is needed for RPC key backups")
    return _run_age


def _key_file_names() -> list[str]:
    return [f"{p}{suffix}" for p in sorted(KNOWN_PRINCIPALS) for suffix in (PRIVATE_SUFFIX, PUBLIC_SUFFIX)]


def _tar_of_keys(keys_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in _key_file_names():
            data = (keys_dir / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = PRIVATE_KEY_MODE if name.endswith(PRIVATE_SUFFIX) else PUBLIC_KEY_MODE
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def backup_keys(keys_dir: Path, out_path: Path, recipients_file: Path, *,
                run: Optional[AgeRunner] = None) -> Path:
    """Encrypt ``keys_dir``'s RPC keys to ``out_path`` with ``age -R recipients_file``."""
    runner = _require_age(run)
    keys_dir, out_path = Path(keys_dir), Path(out_path)
    for principal in sorted(KNOWN_PRINCIPALS):
        _existing_pair_key_id(keys_dir, principal)
    ciphertext = runner(["age", "-R", str(recipients_file)], _tar_of_keys(keys_dir))
    if not ciphertext:
        raise RpcKeyError("`age` produced no output; backup not written")
    _private_dir(out_path.parent)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(ciphertext)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(out_path, 0o600)
    return out_path


def _key_members(plaintext: bytes) -> dict[str, bytes]:
    allowed = set(_key_file_names())
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
        for member in archive.getmembers():
            if member.name not in allowed or not member.isfile() or member.name in members:
                raise RpcKeyError(f"backup holds an unexpected entry {member.name!r}; nothing restored")
            handle = archive.extractfile(member)
            members[member.name] = handle.read() if handle is not None else b""
    missing = allowed - set(members)
    if missing:
        raise RpcKeyError(f"backup is missing {sorted(missing)}; nothing restored")
    return members


def restore_keys(archive: Path, keys_dir: Path, identity: bytes, *,
                 run: Optional[AgeRunner] = None) -> list[str]:
    """Decrypt ``archive`` into an empty ``keys_dir`` and re-check every key strictly."""
    runner = _require_age(run)
    keys_dir = Path(keys_dir)
    if any(keys_dir.glob("*.key")) or any(keys_dir.glob("*.pub")):
        raise RpcKeyError(f"{keys_dir} already holds RPC keys; restore needs an empty directory")
    plaintext = runner(["age", "-d"], Path(archive).read_bytes(), identity=identity)
    members = _key_members(plaintext)
    _private_dir(keys_dir)
    written: list[Path] = []
    try:
        for name, data in sorted(members.items()):
            mode = PRIVATE_KEY_MODE if name.endswith(PRIVATE_SUFFIX) else PUBLIC_KEY_MODE
            path = keys_dir / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            written.append(path)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(path, mode)
        for principal in sorted(KNOWN_PRINCIPALS):
            _existing_pair_key_id(keys_dir, principal)
            load_identity_material(principal, keys_dir)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return sorted(KNOWN_PRINCIPALS)
