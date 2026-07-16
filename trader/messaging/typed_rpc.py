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

Task 3 adds the dedicated typed ZeroMQ transport on top of these primitives:
``TypedRpcRegistry`` (the per-role method allowlist), ``TypedRpcServer``
(raw-JSON ROUTER socket), and ``TypedRpcClient`` (raw-JSON DEALER socket).
See "Response signing" below for the one load-bearing design decision Task 3
had to make explicitly rather than inherit silently from Task 2.

Response signing (Task 3 decision, carried forward from Task 2's review):
Task 2 left ``TypedRpcResponse``/``RpcProblem`` unsigned. Task 3 signs them.
Requests are HMAC-signed because a client's AUTHORIZATION must be proven
before a handler runs; responses carry no such authorization decision, so at
first glance the threat model (these sockets bind on the private Compose
network only, never host-published — see ``config_defaults/trader.yaml``)
might seem to make response signing unnecessary: forging a reply requires an
attacker already inside the private Docker network. But "private network"
does not mean "single tenant" — the whole point of the private network is
that several of *our own* containers (dashboard, coordinator, strategy,
trader) share it, and a defense-in-depth posture assumes any one of them
could be compromised without that compromise silently escalating into
"can impersonate trader_service's replies to everyone else on the network".
An attacker who popped the dashboard container, for instance, could otherwise
inject a forged ``{"ok": true, "body": {...}}`` for a command it never
issued, or spoof an ``EXECUTED`` result for a proposal that actually failed.
Signing responses with the same already-built ``HmacServiceAuthenticator``
closes that gap for the cost of one extra HMAC computation per response —
not a large lift given the authenticator already exists — so ``sign_response``
/ ``verify_response`` below are the chosen, documented posture (option (a)
from the task brief), not option (b) ("trust the private transport").
Unlike request signing, response signing does NOT need its own nonce/replay
window: the client already discards any reply whose ``request_id`` doesn't
match the call currently in flight (a transport-hygiene rule enforced by
``TypedRpcClient.call`` independently of authentication), and ``request_id``
is a fresh client-generated UUID per call, so a stale-but-validly-signed
response for a *different* call can never be mistaken for the current one.

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

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Optional

import zmq
import zmq.asyncio
from pydantic import BaseModel, ConfigDict, ValidationError

from trader.common.logging_helper import setup_logging

logging = setup_logging(module_name='trader.messaging.typed_rpc')


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


class ServiceHmacKeyError(TypedRpcError):
    """Raised when the service HMAC key file fails a production hardening check.

    Covers all four ways ``load_service_hmac_key`` can refuse to start: the
    path is unset/missing, the file's permission bits allow group/other
    access, the file is empty, or the key material is shorter than
    ``MIN_KEY_BYTES``. Deliberately a distinct type from
    ``AuthenticationError`` -- this is a *startup configuration* failure
    (fail loudly before binding any socket), not a per-request auth failure.
    """


class TypedRpcRemoteError(TypedRpcError):
    """Raised by ``TypedRpcClient.call`` when the server replies ``ok=False``.

    Carries the structured ``RpcProblem`` fields so callers can branch on
    ``.code`` (e.g. ``"METHOD_NOT_ALLOWED"``, ``"VALIDATION_ERROR"``) without
    string-matching ``.message``.
    """

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


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
    and vice versa. Task 3's transport layer enforces that pairing at the
    point it constructs responses.

    ``signature`` is Task 3's addition (see the module docstring's "Response
    signing" section for why): it is optional at the model level only so a
    response can be constructed unsigned and then signed via
    ``HmacServiceAuthenticator.sign_response`` in a second step (mirroring how
    ``TypedRpcRequest.signature`` starts as ``""`` in ``sign()``) -- every
    response that actually goes out over ``TypedRpcServer`` carries a real
    signature, and ``TypedRpcClient`` refuses to trust one that doesn't.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    ok: bool
    body: Optional[Dict[str, Any]] = None
    problem: Optional["RpcProblem"] = None
    signature: Optional[str] = None


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


def response_signing_bytes(response: TypedRpcResponse) -> bytes:
    """Canonical bytes covered by a response signature (see module docstring).

    Deliberately excludes ``signature`` itself (obviously) and needs no
    timestamp/nonce -- see "Response signing" above for why request-id
    correlation on the client is sufficient without a replay window here.
    """
    return canonical_json({
        "request_id": response.request_id,
        "ok": response.ok,
        "body": response.body,
        "problem": response.problem.model_dump(mode="json") if response.problem is not None else None,
    })


def _response_digest(key: bytes, response: TypedRpcResponse) -> str:
    return hmac.new(key, response_signing_bytes(response), hashlib.sha256).hexdigest()


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


def _strict_json_loads(raw: bytes | str) -> Any:
    """Size-guarded, hardened ``json.loads`` for untrusted wire bytes.

    Shared by both ``decode_request`` (server side, inbound requests) and
    ``decode_response`` (client side, inbound replies) so BOTH directions get
    the identical guards: a hard size ceiling, no duplicate object keys, and
    no non-finite numbers (the ``NaN``/``Infinity``/``-Infinity`` literals AND
    ordinary literals that overflow to ``inf``). Returns the raw parsed
    object; the caller shape-validates it into the appropriate envelope.

    Response parsing needs these guards just as much as request parsing: a
    forged reply carrying ``1e999`` would otherwise sail through a plain
    ``json.loads`` (as ``float('inf')``) and only blow up later inside
    ``verify_response`` -> ``response_signing_bytes`` -> ``canonical_json``
    (``allow_nan=False``) as a *bare* ``ValueError`` outside the
    AuthenticationError/ReplayError taxonomy -- exactly the Task 2 hazard,
    mirrored on the client side.
    """
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise AuthenticationError(
            f"payload of {len(encoded)} bytes exceeds the {MAX_REQUEST_BYTES}-byte limit"
        )

    try:
        return json.loads(
            encoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
            parse_float=_reject_non_finite_float,
        )
    except AuthenticationError:
        raise
    except json.JSONDecodeError as exc:
        raise AuthenticationError(f"malformed JSON: {exc}") from exc


def decode_request(raw: bytes | str) -> TypedRpcRequest:
    """Strictly parse untrusted wire bytes into a shape-valid ``TypedRpcRequest``.

    This is the parse boundary for incoming requests: it enforces a hard
    size ceiling, strict JSON (no duplicate keys, no non-finite numbers),
    and the exact envelope shape (no missing or unknown fields). It does
    NOT check signature, clock skew, or replay -- call
    ``HmacServiceAuthenticator.verify()`` on the result for that.
    """
    parsed = _strict_json_loads(raw)
    try:
        return TypedRpcRequest.model_validate(parsed)
    except ValidationError as exc:
        raise AuthenticationError(f"malformed typed RPC request: {exc}") from exc


def decode_response(raw: bytes | str) -> TypedRpcResponse:
    """Strictly parse untrusted wire bytes into a shape-valid ``TypedRpcResponse``.

    The client-side counterpart to ``decode_request``: same size/dup-key/
    non-finite guards, then the exact ``TypedRpcResponse`` shape. Callers
    (``TypedRpcClient.call``) still verify the response signature and match
    ``request_id`` on the result; this only closes the parse-time gap.
    """
    parsed = _strict_json_loads(raw)
    try:
        return TypedRpcResponse.model_validate(parsed)
    except ValidationError as exc:
        raise AuthenticationError(f"malformed typed RPC response: {exc}") from exc


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

    def sign_response(self, response: TypedRpcResponse) -> TypedRpcResponse:
        """Sign ``response`` (see module docstring "Response signing").

        Any existing ``signature`` is ignored/overwritten -- the digest is
        always computed over the unsigned fields, never over a prior
        signature, so re-signing is idempotent regardless of input state.
        """
        unsigned = response.model_copy(update={"signature": None})
        signature = _response_digest(self._key, unsigned)
        return unsigned.model_copy(update={"signature": signature})

    def verify_response(self, response: TypedRpcResponse) -> None:
        """Verify a server-signed response. Raises ``AuthenticationError`` on failure.

        Constant-time comparison (``hmac.compare_digest``), matching
        ``verify()``'s handling of request signatures -- the same timing-attack
        rationale applies to the reverse direction.
        """
        if not response.signature:
            raise AuthenticationError("response is missing its signature")
        unsigned = response.model_copy(update={"signature": None})
        expected = _response_digest(self._key, unsigned)
        if not hmac.compare_digest(expected, response.signature):
            raise AuthenticationError("response signature mismatch")


# ---------------------------------------------------------------------------
# Service HMAC key file loading (production startup hardening)
# ---------------------------------------------------------------------------

# Required permission bits for the service HMAC key file: owner read/write
# only. Anything looser (group or other access) means the trading-
# authorization secret is readable by other local users/processes -- on a
# shared host or a container image with a misconfigured volume mount, that's
# a lateral-movement path straight to "can sign requests as trader_service".
REQUIRED_KEY_FILE_MODE = 0o600


def load_service_hmac_key(path: str) -> bytes:
    """Load and validate the service HMAC key file for production startup.

    Production startup MUST fail -- not silently fall back to an ad-hoc or
    empty key -- if the key file:

    1. is unset or doesn't exist (``path`` is falsy, or no file at ``path``);
    2. is not permission-restricted to the owner only (mode must be exactly
       ``0o600``; group/other read is refused even if it's also owner-only
       writable, and so is a too-permissive write bit);
    3. is empty; or
    4. contains fewer than ``MIN_KEY_BYTES`` (32) bytes.

    The raw file bytes ARE the key material -- no hex/base64 decoding, and
    deliberately NO newline-stripping. A "helpful" strip of a trailing
    ``\\n`` would silently truncate a key whose 33rd (or Nth) byte is
    genuinely a ``0x0a`` in the actual output of ``openssl rand`` (~1/256
    chance per key), which is exactly the kind of "close enough" data
    massaging this codebase's design principles forbid for trading-adjacent
    secrets (see CLAUDE.md, "Precision over convenience"). Generate a
    compliant key with e.g.::

        openssl rand 32 > /path/to/service_hmac.key
        chmod 600 /path/to/service_hmac.key

    (``openssl rand -hex 32`` also works -- it produces a 64-byte ASCII file
    with no trailing newline, comfortably above the 32-byte floor -- but do
    not pipe through a text editor or ``echo``, both of which like to append
    a trailing newline.)

    Raises ``ServiceHmacKeyError`` (a ``TypedRpcError``) for all four cases,
    never a bare ``OSError``/``ValueError``, so callers can catch one type.
    """
    if not path:
        raise ServiceHmacKeyError(
            "service_hmac_key_file is not configured (empty path) -- "
            "production startup requires a real key file"
        )
    if not os.path.isfile(path):
        raise ServiceHmacKeyError(f"service HMAC key file not found: {path!r}")

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != REQUIRED_KEY_FILE_MODE:
        raise ServiceHmacKeyError(
            f"service HMAC key file {path!r} has mode {oct(mode)}; expected "
            f"{oct(REQUIRED_KEY_FILE_MODE)} (owner read/write only) -- run "
            f"`chmod 600 {path}`"
        )

    with open(path, "rb") as key_file:
        key = key_file.read()

    if len(key) == 0:
        raise ServiceHmacKeyError(f"service HMAC key file {path!r} is empty")
    if len(key) < MIN_KEY_BYTES:
        raise ServiceHmacKeyError(
            f"service HMAC key file {path!r} contains {len(key)} byte(s); "
            f"at least {MIN_KEY_BYTES} are required"
        )
    return key


# ---------------------------------------------------------------------------
# TypedRpcRegistry -- per-role method allowlist
# ---------------------------------------------------------------------------

# The only three roles a method may be registered under. This is the
# production defense against arbitrary-method invocation: there is no
# catch-all dispatch anywhere in TypedRpcServer -- a method that isn't in
# this registry, under the exact role the request arrived on, simply isn't
# reachable.
VALID_SOCKET_ROLES: FrozenSet[str] = frozenset({"query", "command", "feed"})


def _validate_method_name(method: str) -> None:
    """Shared name validation for both registration and (defensively) dispatch.

    Registration is the primary enforcement point (an invalid name can never
    make it into the registry at all), but keeping this as a standalone
    function documents the exact rule in one place: no empty names, no
    leading underscore (private/internal), no dots (attribute-traversal
    style method names like ``trader.client.ib.reqGlobalCancel``).
    """
    if not isinstance(method, str) or not method:
        raise ValueError("method name must be a non-empty string")
    if method.startswith("_"):
        raise ValueError(f"method name {method!r} must not start with '_' (private)")
    if "." in method:
        raise ValueError(
            f"method name {method!r} must not contain '.' (no dotted/attribute traversal)"
        )


@dataclass(frozen=True)
class TypedRpcRegistration:
    """One (role, method) -> (schemas, handler) entry in a ``TypedRpcRegistry``."""

    socket_role: str
    method: str
    request_model: Any
    response_model: Any
    handler: Callable[[Any], Any]


class TypedRpcRegistry:
    """Per-role method allowlist: a method is registered on exactly one role.

    There is deliberately no way to look up a method without also specifying
    the role it was registered on (see ``resolve``) -- a command sent to the
    query socket must not be dispatchable just because the method name
    happens to match something registered elsewhere.
    """

    def __init__(self) -> None:
        self._by_role_method: Dict[tuple, TypedRpcRegistration] = {}
        self._method_role: Dict[str, str] = {}

    def register(
        self,
        socket_role: str,
        method: str,
        request_model: Any,
        response_model: Any,
        handler: Callable[[Any], Any],
    ) -> None:
        if socket_role not in VALID_SOCKET_ROLES:
            raise ValueError(
                f"socket_role must be one of {sorted(VALID_SOCKET_ROLES)}, got {socket_role!r}"
            )
        _validate_method_name(method)

        existing_role = self._method_role.get(method)
        if existing_role is not None and existing_role != socket_role:
            raise ValueError(
                f"method {method!r} is already registered on role {existing_role!r}; "
                "a method may be registered on exactly one role"
            )
        if (socket_role, method) in self._by_role_method:
            raise ValueError(f"method {method!r} is already registered on role {socket_role!r}")

        self._method_role[method] = socket_role
        self._by_role_method[(socket_role, method)] = TypedRpcRegistration(
            socket_role=socket_role,
            method=method,
            request_model=request_model,
            response_model=response_model,
            handler=handler,
        )

    def resolve(self, socket_role: str, method: str) -> Optional[TypedRpcRegistration]:
        """Look up the registration for an exact ``(role, method)`` pair.

        Returns ``None`` for anything not registered on that exact role --
        including a method that IS registered, just on a different role. The
        server maps that ``None`` to a uniform ``METHOD_NOT_ALLOWED`` problem,
        so wrong-role, unknown, dotted, and private methods are all rejected
        through the same code path.
        """
        return self._by_role_method.get((socket_role, method))

    def contains(self, socket_role: str, method: str) -> bool:
        return (socket_role, method) in self._by_role_method


# ---------------------------------------------------------------------------
# Request/response (de)serialization helpers shared by the server dispatch
# ---------------------------------------------------------------------------

class _DispatchProblem(Exception):
    """Internal control-flow exception carrying an ``RpcProblem`` code.

    Raised inside ``TypedRpcServer._handle_request`` for failures that are
    "the request/response didn't match its schema" rather than "the request
    wasn't authentic" -- kept distinct from ``AuthenticationError``/
    ``ReplayError`` so the dispatch loop's except-clauses map each failure
    family to its own ``RpcProblem.code`` without guessing from message text.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _coerce_request_body(body: Dict[str, Any], request_model: Any) -> Any:
    """Validate an inbound request body against its registered model.

    ``request_model is dict`` is the escape hatch for handlers that want the
    raw, already-JSON-safe dict with no further schema -- everything else
    must be a Pydantic ``BaseModel`` subclass and goes through
    ``model_validate`` (raises ``pydantic.ValidationError`` on mismatch).
    """
    if request_model is dict:
        if not isinstance(body, dict):
            raise TypeError(f"expected a dict body, got {type(body).__name__}")
        return body
    return request_model.model_validate(body)


def _coerce_response_value(value: Any, response_model: Any) -> Dict[str, Any]:
    """Validate a handler's return value against its registered response model.

    Symmetric with ``_coerce_request_body``: ``response_model is dict`` means
    "handler returns a plain dict, no further schema"; a ``BaseModel``
    instance is dumped directly (fast path, also covers a handler that
    returns the dict-declared response as a convenience model instance);
    anything else is validated via ``model_validate`` first.
    """
    if response_model is dict:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if not isinstance(value, dict):
            raise TypeError(
                f"handler for response_model=dict must return a dict, got {type(value).__name__}"
            )
        return value
    if isinstance(value, response_model):
        return value.model_dump(mode="json")
    return response_model.model_validate(value).model_dump(mode="json")


# ---------------------------------------------------------------------------
# TypedRpcServer -- raw-JSON ROUTER socket
# ---------------------------------------------------------------------------

class TypedRpcServer:
    """One ROUTER socket bound to a single ``socket_role``.

    Decodes wire bytes with ``json.loads`` (via ``decode_request``),
    authenticates, resolves ONLY the exact ``(socket_role, method)``
    registration, validates the body and response against their models, and
    replies with a signed ``TypedRpcResponse``. Never calls ``pack``,
    ``unpack``, ``dill.loads``, or any object traversal from
    ``clientserver.py`` -- the whole point of this transport is that it does
    not share that attack surface.
    """

    def __init__(
        self,
        socket_role: str,
        registry: TypedRpcRegistry,
        authenticator: HmacServiceAuthenticator,
        address: str = "tcp://127.0.0.1",
        port: int = 0,
    ):
        if socket_role not in VALID_SOCKET_ROLES:
            raise ValueError(
                f"socket_role must be one of {sorted(VALID_SOCKET_ROLES)}, got {socket_role!r}"
            )
        self.socket_role = socket_role
        self.registry = registry
        self.authenticator = authenticator
        self.address = f"{address}:{port}"
        self.ctx: Optional[zmq.asyncio.Context] = zmq.asyncio.Context()
        self.socket: Optional[zmq.asyncio.Socket] = None
        self._serve_task: Optional[asyncio.Task] = None
        # Track spawned per-request handler tasks so close() can cancel them
        # instead of leaving orphaned coroutines running against a context
        # that's about to be terminated (which is what triggers asyncio's
        # "Task was destroyed but it is pending!" noise).
        self._handler_tasks: "set[asyncio.Task]" = set()

    async def serve(self) -> None:
        self.socket = self.ctx.socket(zmq.ROUTER)
        self.socket.setsockopt(zmq.LINGER, 0)
        # Hard ceiling on inbound message size at the ZMQ layer itself (not
        # just the application-level check inside decode_request) -- an
        # oversized frame is dropped/connection-reset before it is even
        # buffered for us, not just rejected after being fully received.
        self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_REQUEST_BYTES)
        # Retry the bind with backoff -- see RPCServer.serve() in
        # clientserver.py for the identical rationale (a crash-restart can
        # leave the previous process's socket briefly lingering).
        last_err: Optional[Exception] = None
        for attempt in range(10):
            try:
                self.socket.bind(self.address)
                break
            except zmq.ZMQError as ex:
                last_err = ex
                logging.warning(
                    'TypedRpcServer[%s] bind %s in use (attempt %d), retrying: %s',
                    self.socket_role, self.address, attempt + 1, ex,
                )
                await asyncio.sleep(min(2 ** attempt, 8) * 0.25)
        else:
            raise last_err  # type: ignore[misc]
        self._serve_task = asyncio.create_task(self._serve_loop())

    async def _serve_loop(self) -> None:
        while True:
            try:
                frames = await self.socket.recv_multipart()
                # frames: [client_id, ...(anything)..., request_bytes] -- take
                # the first frame as the ROUTER-prepended identity and the
                # last as the payload, robust to either a 2- or 3-frame
                # DEALER/ROUTER envelope shape.
                client_id = frames[0]
                raw = frames[-1]
                task = asyncio.create_task(self._handle_request(client_id, raw))
                self._handler_tasks.add(task)
                task.add_done_callback(self._handler_tasks.discard)
            except zmq.ZMQError as e:
                logging.debug(f"TypedRpcServer[{self.socket_role}] ZMQ error in serve loop: {e}")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.exception(f"TypedRpcServer[{self.socket_role}] unexpected error: {e}")

    async def _handle_request(self, client_id: bytes, raw: bytes) -> None:
        try:
            request = decode_request(raw)
        except AuthenticationError as exc:
            await self._reply(client_id, "", False, None, RpcProblem(
                code="AUTHENTICATION_ERROR", message=str(exc)))
            return
        except Exception:  # pragma: no cover - decode_request's own contract is narrow
            # Generic message on the wire: the real exception (type + trace)
            # is logged server-side only, never echoed to the peer. Leaking
            # internal exception text from a production security transport
            # hands an attacker free reconnaissance / stack detail.
            logging.exception(f"TypedRpcServer[{self.socket_role}] unexpected decode failure")
            await self._reply(client_id, "", False, None, RpcProblem(
                code="INTERNAL_ERROR", message="internal error"))
            return

        request_id = request.request_id
        ok = False
        body: Optional[Dict[str, Any]] = None
        problem: Optional[RpcProblem] = None
        try:
            # Order matters: authenticate BEFORE any allowlist/schema work,
            # so an unauthenticated caller learns nothing about which methods
            # exist or what shape they expect.
            self.authenticator.verify(request)

            registration = self.registry.resolve(self.socket_role, request.method)
            if registration is None:
                raise _DispatchProblem(
                    "METHOD_NOT_ALLOWED",
                    f"method {request.method!r} is not registered on the "
                    f"{self.socket_role!r} socket",
                )

            try:
                parsed_body = _coerce_request_body(request.body, registration.request_model)
            except (ValidationError, TypeError) as exc:
                raise _DispatchProblem("VALIDATION_ERROR", f"invalid request body: {exc}") from exc

            result = registration.handler(parsed_body)
            if inspect.isawaitable(result):
                result = await result

            try:
                body = _coerce_response_value(result, registration.response_model)
            except (ValidationError, TypeError) as exc:
                raise _DispatchProblem(
                    "VALIDATION_ERROR", f"handler returned an invalid response: {exc}") from exc

            ok = True
        except ReplayError as exc:
            problem = RpcProblem(code="REPLAY_ERROR", message=str(exc))
        except AuthenticationError as exc:
            problem = RpcProblem(code="AUTHENTICATION_ERROR", message=str(exc))
        except _DispatchProblem as exc:
            problem = RpcProblem(code=exc.code, message=str(exc))
        except Exception:
            # Generic message on the wire (real detail logged server-side
            # only). METHOD_NOT_ALLOWED / VALIDATION_ERROR / AUTHENTICATION_ERROR
            # above stay descriptive (they don't leak internal exception text
            # or secrets); only this unhandled-handler-error catch-all is
            # scrubbed, since it would otherwise echo an arbitrary handler
            # traceback string to the peer.
            logging.exception(
                f"TypedRpcServer[{self.socket_role}] unhandled error dispatching {request.method!r}")
            problem = RpcProblem(code="INTERNAL_ERROR", message="internal error")

        await self._reply(client_id, request_id, ok, body if ok else None, problem)

    async def _reply(
        self,
        client_id: bytes,
        request_id: str,
        ok: bool,
        body: Optional[Dict[str, Any]],
        problem: Optional[RpcProblem],
    ) -> None:
        response = TypedRpcResponse(request_id=request_id, ok=ok, body=body, problem=problem)
        signed = self.authenticator.sign_response(response)
        try:
            payload = canonical_json(signed.model_dump(mode="json"))
            await self.socket.send_multipart([client_id, b"", payload])
        except Exception:
            logging.exception(f"TypedRpcServer[{self.socket_role}] failed to send response")

    def close(self) -> None:
        """Stop serving and release all ZMQ resources.

        Cancels the accept loop AND every in-flight handler task, closes the
        socket (LINGER=0, so no blocking flush), and terminates the ZMQ
        context so Task 4 can wire this into a service lifecycle without
        leaking tasks or contexts. Safe to call more than once and safe to
        call whether or not ``serve()`` ever ran. Because the socket is closed
        with LINGER=0 before ``ctx.term()``, term has no open sockets to wait
        on and returns immediately -- no teardown hang.
        """
        if self._serve_task is not None:
            self._serve_task.cancel()
            self._serve_task = None
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        if self.socket is not None:
            try:
                self.socket.close(linger=0)
            except Exception:
                pass
            self.socket = None
        if self.ctx is not None:
            try:
                self.ctx.term()
            except Exception:
                pass
            self.ctx = None


# ---------------------------------------------------------------------------
# TypedRpcClient -- raw-JSON DEALER socket
# ---------------------------------------------------------------------------

class TypedRpcClient:
    """One DEALER socket bound to a single ``socket_role``.

    Construct one instance PER ROLE you need (query/command/feed) -- each
    gets its own socket and its own ``threading.Lock``, so a slow feed read
    can never block an urgent command call (they don't share anything).
    """

    def __init__(
        self,
        socket_role: str,
        authenticator: HmacServiceAuthenticator,
        address: str = "tcp://127.0.0.1",
        port: int = 0,
        timeout: float = 10.0,
    ):
        if socket_role not in VALID_SOCKET_ROLES:
            raise ValueError(
                f"socket_role must be one of {sorted(VALID_SOCKET_ROLES)}, got {socket_role!r}"
            )
        self.socket_role = socket_role
        self.authenticator = authenticator
        self.address = f"{address}:{port}"
        self.timeout = timeout
        self.ctx = zmq.Context()
        self.socket: Optional[zmq.Socket] = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            self.socket = self._new_socket()

    def _new_socket(self) -> zmq.Socket:
        socket = self.ctx.socket(zmq.DEALER)
        # LINGER=0: closing/replacing the socket drops any queued request
        #   instead of leaking it for later redelivery.
        # IMMEDIATE=1: never queue a send to a peer with no live connection --
        #   an urgent command to a down service fails loudly, not silently.
        # MAXMSGSIZE: refuse to buffer an oversized reply from a peer.
        # IDENTITY: explicitly assigned (rather than relying on libzmq's
        #   internal, non-introspectable auto-identity) so "fresh identity
        #   after a timeout" is a verifiable property, not an implementation
        #   detail we can't observe. A ROUTER can never route a message to an
        #   identity nobody is connected with anymore, so once this socket is
        #   replaced the old identity is permanently unreachable -- a late
        #   reply to a timed-out request is dropped by ZMQ itself, not by any
        #   application-level filtering.
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.IMMEDIATE, 1)
        socket.setsockopt(zmq.MAXMSGSIZE, MAX_REQUEST_BYTES)
        socket.setsockopt(zmq.IDENTITY, uuid.uuid4().bytes)
        socket.connect(self.address)
        return socket

    def _require_socket(self) -> zmq.Socket:
        if self.socket is None:
            raise ConnectionError("typed RPC client is not connected")
        return self.socket

    def _reset_socket_locked(self) -> None:
        """Drop and recreate the DEALER socket with a FRESH ZMQ identity.

        Must be called only while holding ``self._lock``. Called after a
        timeout (or an unverifiable reply) so that: (1) the in-flight request
        cannot be redelivered to the old identity later, and (2) a late reply
        that eventually does arrive addressed to the OLD identity is dropped
        by ZMQ (nobody is listening on that identity anymore) rather than
        being misdelivered to whatever call happens to be in flight next.
        """
        old = self.socket
        self.socket = None
        if old is not None:
            try:
                old.close(linger=0)
            except Exception:
                pass
        self.socket = self._new_socket()

    def call(
        self,
        method: str,
        body: Dict[str, Any],
        response_model: Any,
        timeout: Optional[float] = None,
    ) -> Any:
        """Sign, send, and await a reply for ``method``/``body``.

        Raises ``ConnectionError`` if unreachable, ``TimeoutError`` on no
        reply within the deadline, ``AuthenticationError``/``ReplayError`` if
        the reply's signature doesn't verify, and ``TypedRpcRemoteError`` if
        the server replied ``ok=False`` (e.g. ``METHOD_NOT_ALLOWED``,
        ``VALIDATION_ERROR``). On success, returns the body parsed against
        ``response_model`` (or the raw dict if ``response_model is dict``).
        """
        deadline_s = timeout if timeout is not None else self.timeout
        request_id = str(uuid.uuid4())
        nonce = uuid.uuid4().hex
        request = self.authenticator.sign(method, request_id, nonce, body)
        payload = canonical_json(request.model_dump(mode="json"))

        with self._lock:
            socket = self._require_socket()
            try:
                socket.send(payload)
            except zmq.Again:
                # IMMEDIATE=1 means an unconnected DEALER refuses to queue --
                # surface "no route to server" loudly instead of buffering.
                self._reset_socket_locked()
                raise ConnectionError(
                    f"typed RPC call to {method!r} could not be sent: no route to server")

            poller = zmq.Poller()
            poller.register(socket, zmq.POLLIN)
            deadline_ms = deadline_s * 1000
            elapsed_ms = 0.0
            poll_interval_ms = 50
            reply: Optional[TypedRpcResponse] = None
            while elapsed_ms < deadline_ms:
                ready = poller.poll(poll_interval_ms)
                if not ready:
                    elapsed_ms += poll_interval_ms
                    continue
                frames = socket.recv_multipart(zmq.NOBLOCK)
                try:
                    # SAME hardened parse as the server's request path (via
                    # decode_response -> _strict_json_loads): a reply carrying
                    # a non-finite number or duplicate key is rejected HERE,
                    # in-taxonomy, rather than slipping through to
                    # verify_response and raising a bare ValueError.
                    candidate = decode_response(frames[-1])
                except Exception as exc:
                    # A frame we cannot safely parse is a protocol violation or
                    # forgery, not a benign stale frame we could correlate by
                    # request_id and skip (we can't even read its request_id).
                    # Treat the pipe as poisoned: reset to a fresh identity and
                    # fail loudly in-taxonomy so the socket the code considers
                    # poisoned is actually reset before we return.
                    self._reset_socket_locked()
                    if isinstance(exc, TypedRpcError):
                        raise
                    raise AuthenticationError(f"unparseable reply: {exc}") from exc
                if candidate.request_id != request_id:
                    # Stale reply from an earlier timed-out call -- discard
                    # and keep waiting for OUR reply within the remaining
                    # budget. Mirrors _SyncMethodCall's handling in
                    # clientserver.py.
                    logging.warning(
                        f"typed RPC {method!r}: discarding stale reply "
                        f"{candidate.request_id!r} (awaiting {request_id!r})")
                    elapsed_ms += poll_interval_ms
                    continue
                reply = candidate
                break

            if reply is None:
                # Drop the poisoned pipe: a late reply addressed to this
                # identity must not be mismatched to whatever call comes next.
                self._reset_socket_locked()
                raise TimeoutError(
                    f"typed RPC call to {method!r} timed out after {deadline_ms:.0f}ms")

            try:
                self.authenticator.verify_response(reply)
            except Exception:
                # A reply that doesn't verify cannot be trusted AT ALL -- not
                # even its problem code -- so reset the connection rather
                # than act on unauthenticated data, and propagate loudly.
                # Broadened to ANY exception (not just AuthenticationError/
                # ReplayError) so the socket the code treats as poisoned is
                # ALWAYS actually reset before the exception escapes, even for
                # an unexpected failure class from the verification stage.
                self._reset_socket_locked()
                raise

        if not reply.ok:
            problem = reply.problem or RpcProblem(
                code="UNKNOWN_ERROR", message="server returned ok=False with no problem detail")
            raise TypedRpcRemoteError(problem.code, problem.message, problem.details)

        body_data = reply.body or {}
        if response_model is dict:
            return body_data
        return response_model.model_validate(body_data)

    def close(self) -> None:
        with self._lock:
            if self.socket is not None:
                try:
                    self.socket.close(linger=0)
                except Exception:
                    pass
                self.socket = None
            # Terminate the context too (LINGER=0 socket already closed, so
            # term returns immediately) -- keeps a long-lived process that
            # opens/closes many clients from leaking one ZMQ context each.
            if self.ctx is not None:
                try:
                    self.ctx.term()
                except Exception:
                    pass
                self.ctx = None
