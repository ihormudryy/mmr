"""Typed RPC boundary: canonical JSON, Ed25519 service identities, transport.

- ``canonical_json`` / ``rpc_signing_bytes`` / ``response_signing_bytes`` give
  a deterministic, domain-separated byte string for a request or response.
- ``ServiceIdentity`` holds one principal's Ed25519 private key and the
  keyring of its peers' public keys (``trader.messaging.rpc_keys``). It signs
  requests (principal, destination server + role + method, payload) and
  verifies them, and the servers sign their responses, which name the
  request id and a digest of the raw request bytes. There is no HMAC mode
  (spec 5.3, hard cutover).
- ``ReplayNonceCache`` gives verified requests a short-lived, thread-safe
  "already used" registry so a captured-and-resent request is rejected.
- ``decode_request`` is the strict JSON parse boundary for *untrusted* wire
  bytes: it rejects duplicate object keys, non-finite numbers, oversized
  payloads, and anything that doesn't shape-match ``TypedRpcRequest``
  exactly. A legacy HMAC envelope (no ``principal``/``server``/``role``)
  fails here, before any handler or nonce claim.
- ``TypedRpcRegistry`` (the per-role method registry), ``TypedRpcServer``
  (raw-JSON ROUTER socket) and ``TypedRpcClient`` (raw-JSON DEALER socket).

Response signing: every reply is signed with the server's own private key
and carries ``server`` and ``request_digest`` (sha256 of the raw request
bytes). A client verifies it with the public key of the server it dialled
(never one the reply names) and accepts only the reply to its own request,
so a compromised container on the private network cannot forge or replay a
server's answer. A reply to a request the server could not decode carries
empty ``request_id``/``request_digest`` and is never accepted as anyone's.

Security notes (see also AGENTS.md's "Design Principles"): a flaw here is a
trading-authorization bypass, so every check below is deliberate:

1. Duplicate JSON keys are rejected at parse time via ``object_pairs_hook``.
2. Non-finite numbers are rejected at parse time, both the literal tokens
   ``NaN``/``Infinity`` (``parse_constant``) and literals that overflow to
   ``inf`` such as ``1e999`` (``parse_float``), so ``canonical_json``
   (``allow_nan=False``) never raises a bare ``ValueError`` on the
   unauthenticated path.
3. Signatures are Ed25519 verifications against a key chosen from the
   keyring loaded at startup; no secret is compared and a request can never
   supply or point to a key.
4. ``verify_request`` claims the nonce dead last: skew, principal, signature,
   destination and ``on_behalf_of`` checks first, so a forged, tampered or
   misdirected request never burns a nonce.
5. Clock skew is checked in both directions (at most 30 seconds); nonce
   entries expire after 60 seconds. Both are injectable via ``now``.
6. Wire payloads larger than 1 MiB are rejected before JSON parsing.
7. All envelope models use ``extra="forbid"``. Required fields have no
   defaults, so a missing or unexpected field raises. The one optional
   field is ``controller_epoch`` (default null, always signed).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import inspect
import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterator, Literal, Mapping, Optional

import zmq
import zmq.asyncio
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trader.common.logging_helper import setup_logging
from trader.automation.controller_epoch import MAX_CONTROLLER_EPOCH, is_epoch_number
from trader.messaging.principals import (
    CONTROLLER_PRINCIPAL,
    SERVER_ACCEPTS,
    SERVER_PRINCIPALS,
    is_valid_principal_name,
)
from trader.messaging.rpc_keys import RpcKeyError, RpcKeyring, load_identity_material
from trader.research.signing import BadSignature, public_key_id, sign_bytes, verify_bytes

logging = setup_logging(module_name='trader.messaging.typed_rpc')


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard ceiling on raw wire bytes accepted for a single request, checked
# before any JSON parsing is attempted.
MAX_REQUEST_BYTES = 1024 * 1024  # 1 MiB
MAX_REQUEST_TIMESTAMP = 4_102_444_800.0  # 2100-01-01 UTC; anything later is not a clock reading

# Maximum allowed difference (in seconds, either direction) between a
# request's claimed timestamp and the verifier's clock.
DEFAULT_CLOCK_SKEW_SECONDS = 30.0

# How long a claimed nonce is remembered before it's evicted from the replay
# cache. Matches the clock-skew window with margin, so a nonce can't be
# forgotten while its request could still plausibly be re-verified.
DEFAULT_NONCE_TTL_SECONDS = 60.0

# Domain separation: RPC signatures can never be confused with bundle
# signatures (which sign other bytes) or with each other's direction.
RPC_REQUEST_CONTEXT = b"mmr.typed-rpc.request.v3\x00"  # v3: the signed bytes carry controller_epoch
RPC_RESPONSE_CONTEXT = b"mmr.typed-rpc.response.v2\x00"


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


class MalformedReplyError(TypedRpcError):
    """Raised when a verified reply does not have the shape the caller asked for."""


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
    principal: str
    server: str
    role: str
    on_behalf_of: Optional[str]
    signature: str
    # SP2 spec 5.1: the ai controller's trader-granted epoch. Optional so every
    # other principal's request keeps its shape; always signed (null when absent).
    controller_epoch: Optional[int] = None

    @field_validator("controller_epoch", mode="before")
    @classmethod
    def _epoch_is_a_json_integer(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        if not is_epoch_number(value):
            raise ValueError(f"controller_epoch must be null or a JSON integer in 1..{MAX_CONTROLLER_EPOCH}")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _timestamp_is_a_plausible_epoch_number(cls, value: Any) -> float:
        # Pydantic's lax float would turn true / "1" into 1.0, and NaN or a
        # huge value would reach the skew arithmetic. Refuse all of them here.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timestamp must be a JSON number")
        if not math.isfinite(value) or not 0 <= value <= MAX_REQUEST_TIMESTAMP:
            raise ValueError("timestamp must be a finite epoch time between 0 and year 2100")
        return float(value)


class TypedRpcResponse(BaseModel):
    """A typed RPC response envelope.

    Exactly one of ``body`` / ``problem`` is meaningful depending on ``ok``;
    both are optional so a success response need not carry a null problem
    and vice versa. Task 3's transport layer enforces that pairing at the
    point it constructs responses.

    ``signature`` is optional at the model level only so a response can be
    built unsigned and then signed via ``ServiceIdentity.sign_response`` --
    every response ``TypedRpcServer`` sends is signed, and
    ``TypedRpcClient`` refuses one that isn't.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    ok: bool
    body: Optional[Dict[str, Any]] = None
    problem: Optional["RpcProblem"] = None
    server: str
    request_digest: str
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


def rpc_signing_bytes(request: TypedRpcRequest) -> bytes:
    """Bytes a request signature covers: caller, destination, payload and the controller epoch."""
    return RPC_REQUEST_CONTEXT + canonical_json({
        "principal": request.principal, "on_behalf_of": request.on_behalf_of,
        "server": request.server, "role": request.role, "method": request.method,
        "request_id": request.request_id, "timestamp": request.timestamp,
        "nonce": request.nonce, "body": request.body, "controller_epoch": request.controller_epoch,
    })


def response_signing_bytes(response: TypedRpcResponse) -> bytes:
    """Bytes a response signature covers, including the request it answers."""
    return RPC_RESPONSE_CONTEXT + canonical_json({
        "server": response.server, "request_id": response.request_id,
        "request_digest": response.request_digest, "ok": response.ok, "body": response.body,
        "problem": response.problem.model_dump(mode="json") if response.problem is not None else None,
    })


def request_digest(raw: bytes) -> str:
    """sha256 of the raw request wire bytes; binds a response to one request."""
    return hashlib.sha256(raw).hexdigest()


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
    ``ServiceIdentity.verify_request()`` on the result for that.
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
# Service identity (Ed25519)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RpcCaller:
    """The authenticated caller of one request. ``on_behalf_of`` is log-only.

    ``controller_epoch`` is the signed envelope epoch (only ai_supervisor sends one).
    """

    principal: str
    on_behalf_of: Optional[str]
    controller_epoch: Optional[int] = None


class ServiceIdentity:
    """One principal's private key plus the keyring of its peers' public keys.

    The private key is held in a name-mangled attribute and never exposed;
    ``repr`` shows only the principal and the key id.
    """

    def __init__(
        self,
        principal: str,
        private_key: Ed25519PrivateKey,
        keyring: RpcKeyring,
        *,
        now: Callable[[], float] = time.time,
        clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
        nonce_cache: Optional[ReplayNonceCache] = None,
    ):
        if not is_valid_principal_name(principal):
            raise ValueError(f"unknown principal {principal!r}")
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("ServiceIdentity requires an Ed25519 private key")
        if not isinstance(keyring, RpcKeyring):
            raise TypeError("ServiceIdentity requires an RpcKeyring")
        self._principal = principal
        self.__private_key = private_key
        self._key_id = public_key_id(private_key.public_key())
        self._keyring = keyring
        self._now = now
        self._clock_skew_seconds = clock_skew_seconds
        self._nonce_cache = nonce_cache if nonce_cache is not None else ReplayNonceCache(now=now)

    @classmethod
    def load(cls, principal: str, keys_dir=None, *, now: Callable[[], float] = time.time) -> "ServiceIdentity":
        """Load ``principal``'s key and its peers' public keys from disk (startup only)."""
        private_key, keyring = load_identity_material(principal, keys_dir)
        return cls(principal, private_key, keyring, now=now)

    @property
    def principal(self) -> str:
        return self._principal

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def public_key(self):
        """This identity's own public key (not secret)."""
        return self.__private_key.public_key()

    def trusted_principals(self) -> frozenset:
        return self._keyring.principals()

    def accepts(self, principal: str) -> bool:
        """Whether this identity's keyring holds ``principal``'s public key."""
        return principal in self._keyring.principals()

    def __repr__(self) -> str:
        return f"<ServiceIdentity principal={self._principal} key_id={self._key_id}>"

    __str__ = __repr__

    def sign_request(
        self,
        *,
        server: str,
        role: str,
        method: str,
        request_id: str,
        nonce: str,
        body: Dict[str, Any],
        on_behalf_of: Optional[str] = None,
        controller_epoch: Optional[int] = None,
    ) -> TypedRpcRequest:
        unsigned = TypedRpcRequest(
            method=method, request_id=request_id, timestamp=self._now(), nonce=nonce,
            body=body, principal=self._principal, server=server, role=role,
            on_behalf_of=on_behalf_of, signature="", controller_epoch=controller_epoch,
        )
        signature = sign_bytes(self.__private_key, rpc_signing_bytes(unsigned))
        return unsigned.model_copy(update={"signature": signature})

    def verify_request(self, request: TypedRpcRequest, *, role: str) -> RpcCaller:
        """Verify ``request``; return the authenticated caller.

        Order is load-bearing: skew, principal, signature, destination,
        ``on_behalf_of``, then (only on success) the nonce claim. No
        filesystem access: the key comes from the startup keyring.
        """
        now = self._now()
        if abs(now - request.timestamp) > self._clock_skew_seconds:
            raise AuthenticationError(
                f"timestamp outside the {self._clock_skew_seconds}s allowed clock skew")
        accepted = SERVER_ACCEPTS.get(self._principal, frozenset())
        if not is_valid_principal_name(request.principal) or request.principal not in accepted:
            raise AuthenticationError("unknown principal")
        try:
            public_key = self._keyring.get(request.principal)
        except RpcKeyError as exc:
            raise AuthenticationError("unknown principal") from exc
        try:
            verify_bytes(public_key, rpc_signing_bytes(request), request.signature)
        except BadSignature as exc:
            raise AuthenticationError("signature mismatch") from exc
        if request.server != self._principal or request.role != role:
            raise AuthenticationError("wrong destination")
        if request.on_behalf_of is not None and (
                request.principal != "trader" or not is_valid_principal_name(request.on_behalf_of)):
            raise AuthenticationError("on_behalf_of is only accepted from trader")
        if request.controller_epoch is not None and request.principal != CONTROLLER_PRINCIPAL:
            raise AuthenticationError("controller_epoch is only accepted from ai_supervisor")
        self._nonce_cache.claim(request.nonce)
        return RpcCaller(request.principal, request.on_behalf_of, request.controller_epoch)

    def sign_response(self, response: TypedRpcResponse) -> TypedRpcResponse:
        if response.server != self._principal:
            raise ValueError(
                f"{self._principal} cannot sign a response naming server {response.server!r}")
        unsigned = response.model_copy(update={"signature": None})
        signature = sign_bytes(self.__private_key, response_signing_bytes(unsigned))
        return unsigned.model_copy(update={"signature": signature})

    def verify_response(self, response: TypedRpcResponse, *, server: str, request_digest: str) -> None:
        """Verify a reply with the key of the server this client dialled."""
        if not response.signature:
            raise AuthenticationError("response is missing its signature")
        if response.server != server:
            raise AuthenticationError("response names the wrong server")
        try:
            public_key = self._keyring.get(server)
        except RpcKeyError as exc:
            raise AuthenticationError(f"no trusted key for server {server!r}") from exc
        unsigned = response.model_copy(update={"signature": None})
        try:
            verify_bytes(public_key, response_signing_bytes(unsigned), response.signature)
        except BadSignature as exc:
            raise AuthenticationError("response signature mismatch") from exc
        if not hmac.compare_digest(response.request_digest, request_digest):
            raise AuthenticationError("response request digest mismatch")


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
    handler: Callable[..., Any]
    execution: Literal["inline", "thread"]
    allowed_principals: FrozenSet[str] = frozenset()
    with_caller: bool = False


class TypedRpcRegistry:
    """Per-role method allowlist: a method is registered on exactly one role.

    There is deliberately no way to look up a method without also specifying
    the role it was registered on (see ``resolve``) -- a command sent to the
    query socket must not be dispatchable just because the method name
    happens to match something registered elsewhere.
    """

    def __init__(
        self,
        *,
        acl: Optional[Mapping[tuple, FrozenSet[str]]] = None,
        default_execution: Literal["inline", "thread"] = "inline",
    ) -> None:
        if default_execution not in ("inline", "thread"):
            raise ValueError("default_execution must be 'inline' or 'thread'")
        self.default_execution = default_execution
        # With an acl every registration must have an entry (no silent dead
        # method); without one (tests) every method denies everyone.
        self._acl = acl
        self._by_role_method: Dict[tuple, TypedRpcRegistration] = {}
        self._method_role: Dict[str, str] = {}

    def register(
        self,
        socket_role: str,
        method: str,
        request_model: Any,
        response_model: Any,
        handler: Callable[..., Any],
        *,
        execution: Optional[Literal["inline", "thread"]] = None,
        with_caller: bool = False,
    ) -> None:
        if socket_role not in VALID_SOCKET_ROLES:
            raise ValueError(
                f"socket_role must be one of {sorted(VALID_SOCKET_ROLES)}, got {socket_role!r}"
            )
        _validate_method_name(method)
        selected_execution = self.default_execution if execution is None else execution
        if selected_execution not in ("inline", "thread"):
            raise ValueError("execution must be 'inline' or 'thread'")

        existing_role = self._method_role.get(method)
        if existing_role is not None and existing_role != socket_role:
            raise ValueError(
                f"method {method!r} is already registered on role {existing_role!r}; "
                "a method may be registered on exactly one role"
            )
        if (socket_role, method) in self._by_role_method:
            raise ValueError(f"method {method!r} is already registered on role {socket_role!r}")
        if self._acl is None:
            allowed: FrozenSet[str] = frozenset()
        elif (socket_role, method) in self._acl:
            allowed = frozenset(self._acl[(socket_role, method)])
        else:
            raise ValueError(
                f"({socket_role!r}, {method!r}) has no allow-list entry in "
                "trader.messaging.principals; add one before registering it")

        self._method_role[method] = socket_role
        self._by_role_method[(socket_role, method)] = TypedRpcRegistration(
            socket_role=socket_role,
            method=method,
            request_model=request_model,
            response_model=response_model,
            handler=handler,
            execution=selected_execution,
            allowed_principals=allowed,
            with_caller=with_caller,
        )

    def unregister(self, socket_role: str, method: str) -> bool:
        """Remove a ``(role, method)`` registration if present.

        Returns ``True`` when a registration was removed. Used by paper
        automation hot-arm to tear down ``execute_automated_intent`` without
        restarting the process. Idempotent for missing entries.
        """
        key = (socket_role, method)
        if key not in self._by_role_method:
            return False
        del self._by_role_method[key]
        if self._method_role.get(method) == socket_role:
            del self._method_role[method]
        return True

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

    def registrations(self) -> Iterator[TypedRpcRegistration]:
        return iter(list(self._by_role_method.values()))


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


def _build_dataclass_reply(method: str, body: Any, response_type: Any) -> Any:
    """Build a stdlib-dataclass reply; the keys must equal the field names exactly."""
    expected = {field.name for field in dataclasses.fields(response_type)}
    if not isinstance(body, dict):
        raise MalformedReplyError(
            f"typed RPC {method!r}: reply for {response_type.__name__} must be an object, "
            f"got {type(body).__name__}")
    missing = sorted(expected - body.keys())
    unexpected = sorted(body.keys() - expected)
    if missing or unexpected:
        raise MalformedReplyError(
            f"typed RPC {method!r}: reply does not match {response_type.__name__}: "
            f"missing fields {missing}, unexpected fields {unexpected}")
    return response_type(**body)


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
        identity: ServiceIdentity,
        address: str = "tcp://127.0.0.1",
        port: int = 0,
        max_in_flight: Optional[int] = None,
    ):
        if socket_role not in VALID_SOCKET_ROLES:
            raise ValueError(
                f"socket_role must be one of {sorted(VALID_SOCKET_ROLES)}, got {socket_role!r}"
            )
        if not isinstance(identity, ServiceIdentity) or identity.principal not in SERVER_PRINCIPALS:
            raise ValueError(
                f"a typed RPC server needs a server identity (one of {sorted(SERVER_PRINCIPALS)})"
            )
        self.socket_role = socket_role
        self.registry = registry
        self.identity = identity
        self.address = f"{address}:{port}"
        configured_max = (
            os.getenv("TYPED_RPC_MAX_IN_FLIGHT", "32")
            if max_in_flight is None else max_in_flight
        )
        try:
            self.max_in_flight = int(configured_max)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_in_flight must be a positive integer") from exc
        if self.max_in_flight <= 0:
            raise ValueError("max_in_flight must be a positive integer")
        self.ctx: Optional[zmq.asyncio.Context] = zmq.asyncio.Context()
        self.socket: Optional[zmq.asyncio.Socket] = None
        self._serve_task: Optional[asyncio.Task] = None
        # Track spawned per-request handler tasks so close() can cancel them
        # instead of leaving orphaned coroutines running against a context
        # that's about to be terminated (which is what triggers asyncio's
        # "Task was destroyed but it is pending!" noise).
        self._handler_tasks: "set[asyncio.Task]" = set()
        self._active_handlers = 0
        self._closing = False

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
        if self._closing:
            return
        digest = request_digest(bytes(raw))
        try:
            request = decode_request(raw)
        except AuthenticationError as exc:
            await self._reply(client_id, "", "", False, None, RpcProblem(
                code="AUTHENTICATION_ERROR", message=str(exc)))
            return
        except Exception:  # pragma: no cover - decode_request's own contract is narrow
            # Generic message on the wire: the real exception (type + trace)
            # is logged server-side only, never echoed to the peer. Leaking
            # internal exception text from a production security transport
            # hands an attacker free reconnaissance / stack detail.
            logging.exception(f"TypedRpcServer[{self.socket_role}] unexpected decode failure")
            await self._reply(client_id, "", "", False, None, RpcProblem(
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
            caller = self.identity.verify_request(request, role=self.socket_role)
            logging.debug(
                "typed rpc dispatch principal=%s on_behalf_of=%s method=%s request_id=%s",
                caller.principal, caller.on_behalf_of, request.method, request.request_id)

            registration = self.registry.resolve(self.socket_role, request.method)
            if registration is None:
                raise _DispatchProblem(
                    "METHOD_NOT_ALLOWED",
                    f"method {request.method!r} is not registered on the "
                    f"{self.socket_role!r} socket",
                )
            if caller.principal not in registration.allowed_principals:
                logging.warning(
                    "typed rpc PERMISSION_DENIED principal=%s method=%s request_id=%s",
                    caller.principal, request.method, request.request_id)
                raise _DispatchProblem(
                    "PERMISSION_DENIED",
                    f"principal {caller.principal!r} may not call {request.method!r}")

            try:
                parsed_body = _coerce_request_body(request.body, registration.request_model)
            except (ValidationError, TypeError) as exc:
                raise _DispatchProblem("VALIDATION_ERROR", f"invalid request body: {exc}") from exc

            if self._active_handlers >= self.max_in_flight:
                raise _DispatchProblem(
                    "SERVER_BUSY", "server command capacity is temporarily exhausted")
            self._active_handlers += 1
            try:
                handler_args = (parsed_body, caller) if registration.with_caller else (parsed_body,)
                if registration.execution == "thread":
                    result = await asyncio.to_thread(registration.handler, *handler_args)
                else:
                    result = registration.handler(*handler_args)
                if inspect.isawaitable(result):
                    result = await result

                try:
                    body = _coerce_response_value(result, registration.response_model)
                    # The reply is signed over canonical JSON; a value that
                    # cannot be encoded must fail here, in-taxonomy, not
                    # later in _reply where the client would only time out.
                    canonical_json(body)
                except (ValidationError, TypeError, ValueError) as exc:
                    raise _DispatchProblem(
                        "VALIDATION_ERROR", f"handler returned an invalid response: {exc}") from exc
            finally:
                self._active_handlers -= 1

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

        if not self._closing:
            await self._reply(client_id, request_id, digest, ok, body if ok else None, problem)

    async def _reply(
        self,
        client_id: bytes,
        request_id: str,
        digest: str,
        ok: bool,
        body: Optional[Dict[str, Any]],
        problem: Optional[RpcProblem],
    ) -> None:
        # Shutdown is an authorization boundary for output as well as input:
        # a thread handler may finish after its awaiting task was cancelled,
        # and decode/auth failures can race with resource teardown.  Never
        # write once close has begun, regardless of which response path won
        # that race.
        if self._closing:
            return
        response = TypedRpcResponse(
            request_id=request_id, ok=ok, body=body, problem=problem,
            server=self.identity.principal, request_digest=digest)
        signed = self.identity.sign_response(response)
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
        self._closing = True
        if self._serve_task is not None:
            self._serve_task.cancel()
            self._serve_task = None
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        self._close_resources()

    async def aclose(self, *, drain_timeout: float = 5.0) -> None:
        """Stop accepting, drain handlers for at most ``drain_timeout``, close.

        Thread work cannot be force-stopped safely. After the bound expires its
        awaiting task is cancelled and the socket is closed, so a late handler
        completion can neither emit a response nor keep shutdown blocked.
        """
        self._closing = True
        timeout = max(0.0, drain_timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        serve_task = self._serve_task
        self._serve_task = None
        if serve_task is not None:
            serve_task.cancel()
            await asyncio.wait({serve_task}, timeout=max(0.0, deadline - loop.time()))
        pending = {task for task in self._handler_tasks if not task.done()}
        if pending:
            _done, pending = await asyncio.wait(
                pending,
                timeout=max(0.0, deadline - loop.time()),
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._handler_tasks.clear()
        self._close_resources()

    def _close_resources(self) -> None:
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
        identity: ServiceIdentity,
        *,
        server: str,
        address: str = "tcp://127.0.0.1",
        port: int = 0,
        timeout: float = 10.0,
    ):
        if socket_role not in VALID_SOCKET_ROLES:
            raise ValueError(
                f"socket_role must be one of {sorted(VALID_SOCKET_ROLES)}, got {socket_role!r}"
            )
        if server not in SERVER_PRINCIPALS:
            raise ValueError(f"server must be one of {sorted(SERVER_PRINCIPALS)}, got {server!r}")
        if not isinstance(identity, ServiceIdentity):
            raise TypeError("TypedRpcClient requires a ServiceIdentity")
        self.socket_role = socket_role
        self.identity = identity
        self.server = server
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
        # SNDTIMEO must NEVER stay at ZMQ's infinite default. IMMEDIATE=1 means
        # an unconnected DEALER refuses to *queue* -- but in blocking mode a
        # refused send WAITS for a peer instead of raising, so `call`'s
        # `except zmq.Again -> ConnectionError` below would be unreachable dead
        # code and the calling thread would wedge forever holding `self._lock`
        # (permanently disabling the whole client, not just that one call).
        # The legacy dill client hit exactly this and fixed it the same way --
        # see clientserver.RPCClient._configure_socket. Mirror its fallback so
        # a 0/None timeout can't reintroduce the infinite default.
        effective_timeout = self.timeout if self.timeout else 10.0
        socket.setsockopt(zmq.SNDTIMEO, int(effective_timeout * 1000))
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
        *,
        on_behalf_of: Optional[str] = None,
        controller_epoch: Optional[int] = None,
    ) -> Any:
        """Sign, send, and await a reply for ``method``/``body``.

        Raises ``ConnectionError`` if unreachable, ``TimeoutError`` on no
        reply within the deadline, ``AuthenticationError``/``ReplayError`` if
        the reply's signature doesn't verify, and ``TypedRpcRemoteError`` if
        the server replied ``ok=False`` (e.g. ``METHOD_NOT_ALLOWED``,
        ``VALIDATION_ERROR``). On success, returns the body parsed against
        ``response_model`` (or the raw dict if ``response_model is dict``).
        A stdlib dataclass ``response_model`` is built strictly: the reply
        must carry exactly its fields, else ``MalformedReplyError``.
        """
        deadline_s = timeout if timeout is not None else self.timeout
        request_id = str(uuid.uuid4())
        nonce = uuid.uuid4().hex
        request = self.identity.sign_request(
            server=self.server, role=self.socket_role, method=method,
            request_id=request_id, nonce=nonce, body=body, on_behalf_of=on_behalf_of,
            controller_epoch=controller_epoch)
        payload = canonical_json(request.model_dump(mode="json"))
        digest = request_digest(payload)

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
                self.identity.verify_response(reply, server=self.server, request_digest=digest)
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
        if isinstance(response_model, type) and dataclasses.is_dataclass(response_model):
            return _build_dataclass_reply(method, body_data, response_model)
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
