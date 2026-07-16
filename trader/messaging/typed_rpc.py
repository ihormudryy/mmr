"""Canonical JSON authentication primitives for the typed RPC boundary (G0 Task 2).

This module is the cryptographic core later typed-RPC tasks build on:

- ``canonical_json`` / ``signing_bytes`` / ``_digest`` produce a deterministic
  byte string for a request so the same logical request always hashes the
  same way regardless of dict insertion order or client implementation.
- ``HmacServiceAuthenticator`` signs and verifies ``TypedRpcRequest`` envelopes
  with HMAC-SHA256, covering method, request id, timestamp, nonce, and body.
- ``ReplayNonceCache`` gives verified requests a short-lived, thread-safe
  "already used" registry so a captured-and-resent request is rejected.
- ``decode_request`` is the strict, security-hardened JSON parse boundary for
  *untrusted* wire bytes: it rejects duplicate object keys, non-finite
  numeric literals (``NaN``/``Infinity``/``-Infinity``), oversized payloads,
  and anything that doesn't shape-match ``TypedRpcRequest`` exactly (no
  missing fields, no unknown fields).

Task 3 (dedicated typed ZeroMQ transports) builds ``TypedRpcRegistry``,
``TypedRpcServer``, and ``TypedRpcClient`` on top of these primitives. This
module intentionally has no ZMQ/asyncio dependency of its own so it can be
unit tested in isolation and reused by both the server and client sides.

Security notes (see also CLAUDE.md's "Design Principles"): a flaw here is a
trading-authorization bypass, so every check below is deliberate:

1. Duplicate JSON keys are rejected at parse time via ``object_pairs_hook`` --
   ``json.loads`` silently keeps the *last* value for a repeated key, which
   would let an attacker smuggle a second, differently-interpreted value
   past naive validation.
2. Non-finite numbers are rejected at parse time. Two paths produce them:
   the literal tokens ``NaN``/``Infinity``/``-Infinity`` (rejected via
   ``parse_constant``) AND ordinary numeric literals that *overflow* to
   ``inf`` (e.g. ``1e999`` -> ``float('inf')``), which the default
   ``parse_float`` (plain ``float``) silently accepts and which never reach
   ``parse_constant`` -- rejected via a ``parse_float`` hook that checks
   ``math.isfinite``. Python's ``json`` module accepts all of these on
   *input* by default even though ``canonical_json`` refuses to *emit* them
   (``allow_nan=False``), so the input side needs its own guard. If an
   overflowing literal slipped through, ``verify()``'s call into
   ``canonical_json`` would raise a bare ``ValueError`` ("Out of range float
   ...") *outside* the AuthenticationError/ReplayError taxonomy -- an
   unauthenticated-path exception (DoS / trace-leak surface once a transport
   wraps handlers).
3. Signature comparison uses ``hmac.compare_digest`` (constant-time), never
   ``==``, to resist timing attacks.
4. ``verify()`` claims the nonce dead last: skew check, then signature
   check, then (only on success) the atomic nonce claim. A forged or
   tampered request that fails skew/signature never burns a nonce, so it
   can't be used to evict or exhaust legitimate replay-protection state.
5. Clock skew is checked in both directions (too old and too far in the
   future) and is at most 30 seconds; nonce entries expire after 60 seconds.
   Both are injectable via the ``now`` callable for deterministic tests.
6. Wire payloads larger than 1 MiB are rejected before JSON parsing is
   attempted at all. The limit is envelope-level (the whole raw request,
   not just the ``body`` sub-field), so the effective body ceiling is 1 MiB
   minus envelope overhead -- the intended, stricter-and-safe choice, since
   the actual DoS vector is the size of the bytes handed to ``json.loads``.
7. All envelope models use ``extra="forbid"`` and have no defaults on
   required fields, so missing or unexpected fields raise instead of being
   silently ignored or null-filled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import threading
import time
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, ConfigDict, ValidationError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard ceiling on raw wire bytes accepted for a single request, checked
# before any JSON parsing is attempted.
MAX_REQUEST_BYTES = 1024 * 1024  # 1 MiB

# Maximum allowed difference (in seconds, either direction) between a
# request's claimed timestamp and the verifier's clock.
DEFAULT_CLOCK_SKEW_SECONDS = 30.0

# How long a claimed nonce is remembered before it's evicted from the replay
# cache. Matches the clock-skew window with margin, so a nonce can't be
# forgotten while its request could still plausibly be re-verified.
DEFAULT_NONCE_TTL_SECONDS = 60.0

# Minimum HMAC key length, in bytes (256 bits) -- matches the SHA-256 output
# size used for the digest itself.
MIN_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TypedRpcError(Exception):
    """Base class for typed RPC authentication/protocol failures."""


class AuthenticationError(TypedRpcError):
    """Raised when a request fails shape validation, clock skew, or signature checks.

    Deliberately NOT a base class of ``ReplayError`` (and vice versa): callers
    (and tests) distinguish "this request is invalid/forged" from "this
    request was valid but already used" as separate failure categories.
    """


class ReplayError(TypedRpcError):
    """Raised when a request's nonce has already been claimed (replay attempt)."""


# ---------------------------------------------------------------------------
# Envelope models
# ---------------------------------------------------------------------------

class TypedRpcRequest(BaseModel):
    """A signed RPC request envelope.

    ``extra="forbid"`` and the absence of defaults on any field means both
    unknown keys and missing keys are rejected by Pydantic at construction
    time -- this is what "reject missing fields" means in practice for this
    model.
    """

    model_config = ConfigDict(extra="forbid")

    method: str
    request_id: str
    timestamp: float
    nonce: str
    body: Dict[str, Any]
    signature: str


class TypedRpcResponse(BaseModel):
    """A typed RPC response envelope.

    Exactly one of ``body`` / ``problem`` is meaningful depending on ``ok``;
    both are optional so a success response need not carry a null problem
    and vice versa. Task 3's transport layer is expected to enforce that
    pairing at the point it constructs responses.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    ok: bool
    body: Optional[Dict[str, Any]] = None
    problem: Optional["RpcProblem"] = None


class RpcProblem(BaseModel):
    """Structured error detail carried by a failed ``TypedRpcResponse``."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


TypedRpcResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Canonical form + signing
# ---------------------------------------------------------------------------

def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def signing_bytes(request: TypedRpcRequest) -> bytes:
    return canonical_json({
        "method": request.method,
        "request_id": request.request_id,
        "timestamp": request.timestamp,
        "nonce": request.nonce,
        "body": request.body,
    })


def _digest(key: bytes, request: TypedRpcRequest) -> str:
    return hmac.new(key, signing_bytes(request), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Strict JSON parsing for untrusted wire bytes
# ---------------------------------------------------------------------------

def _reject_duplicate_keys(pairs: list) -> Dict[str, Any]:
    """``object_pairs_hook`` that raises on any repeated key in a JSON object.

    ``json.loads``'s default behaviour is to silently keep the *last* value
    for a duplicated key, which is exactly the kind of "close enough"
    ambiguity a canonical/auth format must not tolerate. This hook fires for
    every JSON object in the document (including nested ones, e.g. inside
    ``body``), so duplicates anywhere in the payload are caught.
    """
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise AuthenticationError(f"duplicate key in JSON object: {key!r}")
        seen[key] = value
    return seen


def _reject_non_finite_constant(constant: str) -> Any:
    """``parse_constant`` hook that rejects ``NaN``/``Infinity``/``-Infinity``.

    ``canonical_json`` already refuses to *emit* these (``allow_nan=False``),
    but ``json.loads`` accepts them on *input* by default -- this closes that
    gap so a malicious or buggy peer can't smuggle a non-finite number into a
    signed body via the special literal tokens.
    """
    raise AuthenticationError(f"non-finite numeric constant in JSON: {constant}")


def _reject_non_finite_float(number_str: str) -> float:
    """``parse_float`` hook that rejects numeric literals overflowing to inf.

    ``parse_constant`` only fires for the special tokens
    ``NaN``/``Infinity``/``-Infinity``. An ordinary numeric literal like
    ``1e999`` is routed through ``parse_float`` (default ``float``), silently
    becomes ``float('inf')``, and never reaches ``parse_constant`` -- so
    without this hook it would sail past ``decode_request`` and only blow up
    later inside ``verify()`` -> ``canonical_json`` as a bare ``ValueError``
    on the unauthenticated path. We reproduce the default (``float(...)``)
    and reject anything non-finite.
    """
    value = float(number_str)
    if not math.isfinite(value):
        raise AuthenticationError(f"non-finite numeric value in JSON: {number_str}")
    return value


def decode_request(raw: bytes | str) -> TypedRpcRequest:
    """Strictly parse untrusted wire bytes into a shape-valid ``TypedRpcRequest``.

    This is the parse boundary for incoming requests: it enforces a hard
    size ceiling, strict JSON (no duplicate keys, no non-finite numbers),
    and the exact envelope shape (no missing or unknown fields). It does
    NOT check signature, clock skew, or replay -- call
    ``HmacServiceAuthenticator.verify()`` on the result for that.
    """
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise AuthenticationError(
            f"request of {len(encoded)} bytes exceeds the {MAX_REQUEST_BYTES}-byte limit"
        )

    try:
        parsed = json.loads(
            encoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
            parse_float=_reject_non_finite_float,
        )
    except AuthenticationError:
        raise
    except json.JSONDecodeError as exc:
        raise AuthenticationError(f"malformed JSON request: {exc}") from exc

    try:
        return TypedRpcRequest.model_validate(parsed)
    except ValidationError as exc:
        raise AuthenticationError(f"malformed typed RPC request: {exc}") from exc


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------

class ReplayNonceCache:
    """Thread-safe, TTL-bounded registry of already-claimed nonces.

    ``claim()`` is atomic: under concurrent callers, exactly one claim of a
    given nonce succeeds and every other (concurrent or later, until expiry)
    raises ``ReplayError``. Expired entries are swept opportunistically on
    each ``claim()`` call, bounding memory without a background thread.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_NONCE_TTL_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._lock = threading.Lock()
        self._claimed_at: Dict[str, float] = {}

    def claim(self, nonce: str) -> None:
        """Atomically claim ``nonce``. Raises ``ReplayError`` if already claimed."""
        now = self._now()
        with self._lock:
            self._evict_expired(now)
            if nonce in self._claimed_at:
                raise ReplayError(f"nonce already used: {nonce!r}")
            self._claimed_at[nonce] = now

    def _evict_expired(self, now: float) -> None:
        expired = [
            claimed_nonce
            for claimed_nonce, claimed_at in self._claimed_at.items()
            if now - claimed_at > self._ttl_seconds
        ]
        for claimed_nonce in expired:
            del self._claimed_at[claimed_nonce]


# ---------------------------------------------------------------------------
# Authenticator
# ---------------------------------------------------------------------------

class HmacServiceAuthenticator:
    """Signs and verifies ``TypedRpcRequest`` envelopes with HMAC-SHA256."""

    def __init__(
        self,
        key: bytes,
        now: Callable[[], float] = time.time,
        clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
        nonce_cache: Optional[ReplayNonceCache] = None,
    ):
        if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
            raise ValueError(f"HMAC key must be at least {MIN_KEY_BYTES} bytes")
        self._key = bytes(key)
        self._now = now
        self._clock_skew_seconds = clock_skew_seconds
        self._nonce_cache = (
            nonce_cache if nonce_cache is not None else ReplayNonceCache(now=now)
        )

    def sign(self, method: str, request_id: str, nonce: str, body: Dict[str, Any]) -> TypedRpcRequest:
        """Build and sign a request envelope for ``method``/``body`` right now."""
        unsigned = TypedRpcRequest(
            method=method,
            request_id=request_id,
            timestamp=self._now(),
            nonce=nonce,
            body=body,
            signature="",
        )
        signature = _digest(self._key, unsigned)
        return unsigned.model_copy(update={"signature": signature})

    def verify(self, request: TypedRpcRequest) -> None:
        """Verify ``request``: clock skew, then signature, then claim the nonce.

        Order matters (see module docstring, point 4): the nonce is only
        claimed after both the skew and signature checks succeed, so a
        forged or tampered request never consumes replay-protection state.
        """
        now = self._now()
        if abs(now - request.timestamp) > self._clock_skew_seconds:
            raise AuthenticationError(
                f"timestamp {request.timestamp!r} outside the "
                f"{self._clock_skew_seconds}s allowed clock skew (now={now!r})"
            )

        expected = _digest(self._key, request)
        if not hmac.compare_digest(expected, request.signature):
            raise AuthenticationError("signature mismatch")

        self._nonce_cache.claim(request.nonce)
