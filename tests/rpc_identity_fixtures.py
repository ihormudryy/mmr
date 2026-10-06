"""Test helpers for RPC identities: real key files in temp dirs, in-memory identities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trader.messaging.principals import KNOWN_PRINCIPALS


def write_keyset(directory: Path, principals: Iterable[str] = KNOWN_PRINCIPALS) -> dict[str, Ed25519PrivateKey]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    keys = {}
    for principal in principals:
        key = Ed25519PrivateKey.generate()
        private_path = directory / f"{principal}.key"
        public_path = directory / f"{principal}.pub"
        private_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        public_path.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        os.chmod(private_path, 0o600)
        os.chmod(public_path, 0o644)
        keys[principal] = key
    return keys
