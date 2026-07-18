"""Canonical JSON + SHA-256 digests for the research evidence chain (P2 Task 2).

``canonical_json_bytes`` is the ROOT of every research digest and Ed25519
attestation: the exact bytes must be deterministic across processes and
platforms, and must fail loudly on anything non-canonical -- a naive datetime,
a NaN/Infinity, or an unsupported type -- because wrong evidence bytes are worse
than none (they would silently fork the digest chain).

Canonical rules:
- mapping keys sorted, compact separators, UTF-8 (not ASCII-escaped);
- list/tuple order preserved;
- ``datetime`` MUST be timezone-aware; serialized as UTC ISO-8601;
- ``Decimal`` serialized as its exact string (never a lossy float);
- ``date`` serialized as ISO-8601;
- NaN / +/-Infinity (float or Decimal) rejected;
- unsupported types rejected.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping


def _canonicalize(value: Any) -> Any:
    # bool is an int subclass; both pass through and json emits true/false/ints.
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite float is not canonical: {value!r}")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal is not canonical: {value!r}")
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"naive datetime is not canonical (must be tz-aware UTC): {value!r}")
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    raise TypeError(f"unsupported type in canonical JSON: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic canonical-JSON encoding of ``value`` as UTF-8 bytes."""
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(prefix: str, value: Any) -> str:
    """Namespaced SHA-256 hex digest: ``sha256(prefix + "\\n" + canonical bytes)``.

    The prefix domain-separates digests (a dataset manifest and a trial with the
    same body get different digests), so a digest can never be replayed across
    artifact kinds.
    """
    return hashlib.sha256(
        prefix.encode("utf-8") + b"\n" + canonical_json_bytes(value)).hexdigest()
