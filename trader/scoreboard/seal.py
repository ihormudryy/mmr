"""Canonical row digests and the append-only seal chain (Plan 5 ruling 11)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any, Mapping

GENESIS = "0" * 64


def _canonical(value: Any) -> Any:
    """Each value carries a type tag so 1, 1.0, "1" and a date never collide."""
    if value is None:
        return None
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, int):
        return ["i", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"a non-finite float cannot be sealed: {value!r}")
        return ["f", repr(value)]
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise TypeError("a naive datetime cannot be sealed")
        return ["t", value.astimezone(dt.timezone.utc).isoformat()]
    if isinstance(value, dt.date):
        return ["d", value.isoformat()]
    if isinstance(value, str):
        return ["s", value]
    raise TypeError(f"cannot seal a value of type {type(value).__name__}")


def row_digest(row: Mapping[str, Any]) -> str:
    canonical = {key: _canonical(row[key]) for key in sorted(row)}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def chain_digest(prev: str, table: str, key: str, digest: str) -> str:
    return hashlib.sha256(f"{prev}|{table}|{key}|{digest}".encode()).hexdigest()
