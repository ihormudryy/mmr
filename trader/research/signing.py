"""Ed25519 signing + strict private-key hygiene (P2 Task 7 -- SECURITY CORE).

This module is the ONLY place a private signing key is ever handled. The rules it
enforces are non-negotiable for a trading system whose eligibility gate a private
key vouches for:

* **A private key is loaded from a filesystem PATH holding a PKCS8 PEM, nothing
  else.** No raw-bytes-in-code, no inline key strings, no env, no YAML. Before the
  file is even *read*, its permission bits are checked: any group/world bit
  (``mode & 0o077``) makes it an ``InsecureKeyFile`` and we refuse to open it.
* **A private key never leaves this process in any serialized form.** The
  ``AttestationSigner`` wrapper redacts its ``repr``/``str``; it exposes the
  PUBLIC key and the stable public-key id, never the private bytes. Nothing here
  logs, prints, or puts private material in an exception message.
* **Public-key identity is a stable rotation fingerprint**: ``public_key_id`` is
  ``"ed25519-" + sha256(raw 32-byte public key)``. A new keypair is a new id, so
  rotating the key is visible everywhere the id is recorded.
* **Signatures are deterministic** (Ed25519): the same key over the same message
  yields byte-identical signatures. ``sign_bytes`` returns urlsafe-base64 (with
  padding); ``verify_bytes`` fails loud (``BadSignature``) on any mismatch,
  tampering, or malformed signature -- it never returns a soft "maybe valid".

The CALLER that writes a freshly generated private PEM MUST create the file with
``0o600`` permissions; this module deliberately does not write private material
to disk (so it can never do so with loose perms).
"""
from __future__ import annotations

import base64
import binascii
import os
import stat
from typing import Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "InsecureKeyFile",
    "InvalidKeyType",
    "MalformedKey",
    "BadSignature",
    "load_signing_key",
    "load_verify_key",
    "public_key_id",
    "generate_private_key_pem",
    "public_key_pem",
    "sign_bytes",
    "verify_bytes",
    "AttestationSigner",
]


# --------------------------------------------------------------------------- #
# Exceptions -- fail loud, never leak key material in the message
# --------------------------------------------------------------------------- #
class InsecureKeyFile(Exception):
    """A private-key file is group/world accessible; we refuse to read it."""


class InvalidKeyType(Exception):
    """A loaded key is not an Ed25519 private/public key as required."""


class MalformedKey(Exception):
    """A key file is not a parseable PEM (garbage / truncated / wrong format)."""


class BadSignature(Exception):
    """A signature does not verify against the message + public key. Fail closed:
    a bad, tampered, or malformed signature RAISES -- it never returns."""


# --------------------------------------------------------------------------- #
# Key loading (path + PKCS8 PEM only) with permission hygiene
# --------------------------------------------------------------------------- #
def _require_secure_perms(path: str) -> None:
    """Refuse to read a private key whose file is readable by group or others.

    The check runs on ``os.stat`` BEFORE any read, so an over-shared key never
    even enters the process. ``mode & 0o077`` catches every group/world bit
    (read, write, or execute); only ``0o600``/``0o400``-style owner-only modes
    pass.
    """
    info = os.stat(path)
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise InsecureKeyFile(
            f"private key file {path!r} has insecure permissions {oct(mode)}; "
            f"must be owner-only (e.g. 0o600). Refusing to read it.")


def load_signing_key(path: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PKCS8 PEM file at ``path``.

    Order is load-bearing: (1) permission check FIRST (refuse group/world access
    before reading a byte), (2) read + parse the PKCS8 PEM, (3) confirm the key
    is Ed25519. Any other key algorithm raises ``InvalidKeyType``; unparseable
    bytes raise ``MalformedKey``. No key material is ever logged or echoed.
    """
    _require_secure_perms(path)
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        # The exception text is generic ("Could not deserialize key data"); it
        # does not carry the key bytes. Re-raise as a clean domain error.
        raise MalformedKey(f"could not parse private key PEM at {path!r}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise InvalidKeyType(
            f"key at {path!r} is {type(key).__name__}, not an Ed25519 private key")
    return key


def load_verify_key(path: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key (SubjectPublicKeyInfo PEM) from ``path``.

    Public keys are not secret, so there is deliberately no permission check.
    Non-Ed25519 keys raise ``InvalidKeyType``; unparseable files ``MalformedKey``.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as exc:
        raise MalformedKey(f"could not parse public key PEM at {path!r}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise InvalidKeyType(
            f"key at {path!r} is {type(key).__name__}, not an Ed25519 public key")
    return key


# --------------------------------------------------------------------------- #
# Public-key identity (rotation fingerprint)
# --------------------------------------------------------------------------- #
def _raw_public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)


def public_key_id(public_key: Ed25519PublicKey) -> str:
    """Stable rotation identifier for a public key.

    ``"ed25519-" + sha256(raw 32-byte public key).hexdigest()``. A different
    keypair yields a different id, so this is what an attestation records to name
    the key that vouches for it -- and what a verifier keys its trust store on.
    """
    import hashlib

    if not isinstance(public_key, Ed25519PublicKey):
        raise InvalidKeyType(
            f"public_key_id requires an Ed25519 public key, got {type(public_key).__name__}")
    return "ed25519-" + hashlib.sha256(_raw_public_bytes(public_key)).hexdigest()


# --------------------------------------------------------------------------- #
# Keygen helpers (tests / offline key provisioning)
# --------------------------------------------------------------------------- #
def generate_private_key_pem() -> bytes:
    """Generate a fresh Ed25519 private key as an UNENCRYPTED PKCS8 PEM.

    Returned as bytes so the CALLER can write it with ``0o600`` (this module
    never writes private material to disk itself, so it cannot do so with loose
    permissions).
    """
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def public_key_pem(key: Union[Ed25519PrivateKey, Ed25519PublicKey]) -> bytes:
    """SubjectPublicKeyInfo PEM for the public half of ``key`` (private or public
    input). Safe to persist and distribute -- it is not secret."""
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    if not isinstance(public, Ed25519PublicKey):
        raise InvalidKeyType(f"cannot derive a public key from {type(key).__name__}")
    return public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)


# --------------------------------------------------------------------------- #
# Sign / verify over raw bytes
# --------------------------------------------------------------------------- #
def sign_bytes(private_key: Ed25519PrivateKey, message: bytes) -> str:
    """Sign ``message`` and return the signature as urlsafe-base64 (with padding).

    Ed25519 is deterministic, so the same key + message always yields the same
    signature bytes and therefore the same base64 string.
    """
    if not isinstance(private_key, Ed25519PrivateKey):
        raise InvalidKeyType("sign_bytes requires an Ed25519 private key")
    signature = private_key.sign(message)
    return base64.urlsafe_b64encode(signature).decode("ascii")


def verify_bytes(public_key: Ed25519PublicKey, message: bytes,
                 signature_b64: str) -> None:
    """Verify ``signature_b64`` over ``message``; return ``None`` on success.

    Raises ``BadSignature`` on ANY problem -- a wrong key, a tampered message, or
    a malformed base64 signature. There is no soft return: a caller that reaches
    the next line KNOWS the signature is good.
    """
    if not isinstance(public_key, Ed25519PublicKey):
        raise InvalidKeyType("verify_bytes requires an Ed25519 public key")
    try:
        raw = base64.urlsafe_b64decode(signature_b64)
    except (binascii.Error, ValueError) as exc:
        raise BadSignature("signature is not valid base64") from exc
    try:
        public_key.verify(raw, message)
    except InvalidSignature as exc:
        raise BadSignature("signature does not verify") from exc


# --------------------------------------------------------------------------- #
# Signer wrapper -- holds the private key, redacts everything about it
# --------------------------------------------------------------------------- #
class AttestationSigner:
    """A thin wrapper around a private key that can sign messages but never leaks
    its private material.

    Hygiene guarantees:
    * ``repr``/``str`` redact -- they show only the public-key id.
    * The private key is not an accessible public attribute (name-mangled) and is
      never returned by any method; only the PUBLIC key and its id are exposed.
    * Nothing here writes the private key anywhere.

    The domain-level ``.sign(unsigned_fields) -> EligibilityAttestation`` method
    is attached by ``trader.research.attestation`` so this security core stays
    free of attestation-domain imports.
    """

    __slots__ = ("__private_key", "__public_key_id")

    def __init__(self, private_key: Ed25519PrivateKey):
        if not isinstance(private_key, Ed25519PrivateKey):
            raise InvalidKeyType(
                "AttestationSigner requires an Ed25519 private key")
        # name-mangled + slotted: no __dict__, and no plain public attribute.
        object.__setattr__(self, "_AttestationSigner__private_key", private_key)
        object.__setattr__(self, "_AttestationSigner__public_key_id",
                           public_key_id(private_key.public_key()))

    @classmethod
    def from_key_file(cls, path: str) -> "AttestationSigner":
        """Load the private key from a PKCS8 PEM path (with permission hygiene)."""
        return cls(load_signing_key(path))

    @classmethod
    def generate(cls) -> "AttestationSigner":
        """Create a signer around a freshly generated in-memory key (tests/keygen)."""
        return cls(Ed25519PrivateKey.generate())

    @property
    def public_key_id(self) -> str:
        return self.__public_key_id

    @property
    def public_key(self) -> Ed25519PublicKey:
        """The PUBLIC verification key (safe to share / build a verifier from)."""
        return self.__private_key.public_key()

    def public_key_pem(self) -> bytes:
        """SubjectPublicKeyInfo PEM of the verification key (not secret)."""
        return public_key_pem(self.__private_key)

    def sign_message(self, message: bytes) -> str:
        """Ed25519-sign raw bytes; returns urlsafe-base64 signature."""
        return sign_bytes(self.__private_key, message)

    def __repr__(self) -> str:
        return (f"<AttestationSigner key_id={self.__public_key_id} "
                f"(private key redacted)>")

    __str__ = __repr__
