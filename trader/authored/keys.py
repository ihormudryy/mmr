"""Content-addressed digests and authored strategy keys (spec section 3).

A digest is ``sha256:<hex>`` of the file bytes, the store key and the binding
key. An authored key is ``authored:sha256:<hex>:<Class>``.
"""
from __future__ import annotations

import hashlib
import re

_DIGEST_PATTERN = r"sha256:[0-9a-f]{64}"
# ASCII only and at most 64 characters: the static checker refuses any class name this refuses.
_CLASS_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
_DIGEST = re.compile(_DIGEST_PATTERN)
_CLASS_NAME = re.compile(_CLASS_NAME_PATTERN)
_AUTHORED_KEY = re.compile(f"authored:({_DIGEST_PATTERN}):({_CLASS_NAME_PATTERN})")


def source_digest(source: bytes) -> str:
    if not isinstance(source, (bytes, bytearray)):
        raise TypeError(f"source must be bytes, got {type(source).__name__}")
    return f"sha256:{hashlib.sha256(source).hexdigest()}"


def is_valid_class_name(class_name: object) -> bool:
    return isinstance(class_name, str) and _CLASS_NAME.fullmatch(class_name) is not None


def authored_key(digest: str, class_name: str) -> str:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError(f"digest must match sha256:<64 hex>, got {digest!r}")
    if not is_valid_class_name(class_name):
        raise ValueError(f"class name is not a Python identifier: {class_name!r}")
    return f"authored:{digest}:{class_name}"


def parse_authored_key(key: str) -> tuple[str, str]:
    match = _AUTHORED_KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise ValueError(f"not an authored key: {key!r}")
    return match.group(1), match.group(2)


def is_authored_key(key: str) -> bool:
    return isinstance(key, str) and _AUTHORED_KEY.fullmatch(key) is not None
