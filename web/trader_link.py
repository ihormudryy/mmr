"""Shared typed-RPC transport to the trader / strategy services.

One ``TraderLink`` == one typed-RPC socket (a role at a ``tcp://host:port``
endpoint). It owns the plumbing the dashboard's typed clients used to each
re-implement: endpoint parsing, HMAC-authenticator construction, lazy client
build, reconnect after a transport failure, and serialization under a
per-socket lock.

``call`` raises exactly one transport error, ``TraderLinkError``, carrying a
``kind`` (``"timeout"`` | ``"unavailable"``) and the original ``cause`` so a
caller can map it to its own envelope (the command gateway → ``GatewayError``;
the manage client → re-raise the original ``TimeoutError``/``OSError``). A
``TypedRpcRemoteError`` — an application-level rejection from a *healthy*
socket — propagates unchanged and does NOT trigger a reconnect.

Two kinds of caller:
- Callers that want reconnect + a serialization lock hold a ``TraderLink``
  (``DashboardCommandGateway``, ``ManageRpcClient``).
- Callers that manage their own reconnect at a higher layer (the event
  bridge's cursor-resnapshot loop) reuse just the construction primitives
  ``parse_endpoint`` / ``build_authenticator`` / ``connect_client`` below.

``TraderLink`` never calls ``connect()`` itself — the ``client_factory`` it is
given must return a ready-to-use client. ``connect_client`` (the default
primitive) does connect; callers injecting their own factory decide.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    load_service_hmac_key,
)

logger = logging.getLogger("web.trader_link")

# The default service-HMAC key path when the env var is unset. Matches the
# value ManageRpcClient used; the command gateway/bridge require the env var to
# be set explicitly (an empty path makes load_service_hmac_key fail loudly).
_DEFAULT_HMAC_KEY_PATH = "~/.config/mmr/service_hmac.key"


# ── construction primitives (shared by TraderLink and the event bridge) ──────

def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Split a ``tcp://host:port`` endpoint into ``TypedRpcClient``'s separate
    ``address``/``port`` constructor args. Fails loudly on any other shape."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"typed endpoint must be tcp://host:port, got {endpoint!r}")
    return f"tcp://{parsed.hostname}", parsed.port


def build_authenticator(
    env: Mapping[str, str] = os.environ,
    *,
    default_key_path: str | None = None,
) -> HmacServiceAuthenticator:
    """Build the shared-secret HMAC authenticator the typed clients sign with.

    Reads the service-HMAC key file from ``MMR_SERVICE_HMAC_KEY_FILE`` and
    validates it via ``load_service_hmac_key`` (missing path, wrong permission
    bits, empty, or too-short key all fail loudly) — the dashboard fails closed
    rather than falling back to an ad-hoc key. ``default_key_path`` is used only
    when the env var is unset; leave it None to require the env var explicitly
    (the command/feed path), or pass ``_DEFAULT_HMAC_KEY_PATH`` for the manage
    path's historical default.
    """
    key_file = (env.get("MMR_SERVICE_HMAC_KEY_FILE") or (default_key_path or "")).strip()
    return HmacServiceAuthenticator(load_service_hmac_key(key_file))


def connect_client(
    role: str,
    endpoint: str,
    *,
    authenticator: HmacServiceAuthenticator | None = None,
    timeout: float = 10.0,
    env: Mapping[str, str] = os.environ,
) -> TypedRpcClient:
    """Build AND connect a ``TypedRpcClient`` for ``role`` at ``endpoint``."""
    address, port = parse_endpoint(endpoint)
    client = TypedRpcClient(
        role, authenticator or build_authenticator(env),
        address=address, port=port, timeout=timeout)
    client.connect()
    return client


# ── the transport ────────────────────────────────────────────────────────────

class TraderLinkError(Exception):
    """The one transport error ``TraderLink.call`` raises. ``kind`` is
    ``"timeout"`` (no reply within the deadline — outcome unknown) or
    ``"unavailable"`` (socket could not carry the request — retryable);
    ``cause`` is the original exception so a caller can re-raise it."""

    def __init__(self, kind: str, message: str, cause: BaseException | None = None):
        super().__init__(message)
        self.kind = kind
        self.cause = cause


class TraderLink:
    """One typed-RPC socket with lazy connect, reconnect-on-transport-failure,
    and a serialization lock. The lock is per-``TraderLink`` — hold one link
    per socket to keep, e.g., the command socket's lock isolated from reads."""

    def __init__(
        self,
        role: str | None = None,
        endpoint: str | None = None,
        *,
        authenticator: HmacServiceAuthenticator | None = None,
        timeout: float = 10.0,
        env: Mapping[str, str] = os.environ,
        client_factory: Callable[[], TypedRpcClient] | None = None,
    ):
        if client_factory is None and (role is None or endpoint is None):
            raise ValueError("TraderLink needs either a client_factory or (role, endpoint)")
        self._role = role
        self._endpoint = endpoint
        self._authenticator = authenticator
        self._timeout = timeout
        self._env = env
        self._client_factory = client_factory or self._default_factory
        self._client: TypedRpcClient | None = None
        self._lock = threading.Lock()

    def _default_factory(self) -> TypedRpcClient:
        return connect_client(
            self._role, self._endpoint, authenticator=self._authenticator,
            timeout=self._timeout, env=self._env)

    def call(self, method: str, body: dict[str, Any], *,
             timeout: Optional[float] = None) -> dict[str, Any]:
        with self._lock:
            if self._client is None:
                self._client = self._client_factory()
            try:
                # Pass timeout only when set: some callers' fakes (and the
                # production 3-arg path) don't accept a timeout kwarg.
                if timeout is None:
                    return self._client.call(method, body, dict)
                return self._client.call(method, body, dict, timeout=timeout)
            except TimeoutError as exc:
                # TimeoutError is an OSError subclass, so this clause MUST
                # precede the (ConnectionError, OSError) one below.
                self._reset_locked()
                raise TraderLinkError(
                    "timeout",
                    f"no reply from {self._role or 'trader'} within the deadline",
                    exc) from exc
            except (ConnectionError, OSError) as exc:
                self._reset_locked()
                raise TraderLinkError(
                    "unavailable",
                    f"{self._role or 'trader'} channel unavailable",
                    exc) from exc
            # TypedRpcRemoteError (and anything else) propagates unchanged: the
            # socket is healthy, so no reconnect.

    def _reset_locked(self) -> None:
        """Discard the current client (stale DEALER identity) so the next call
        builds a fresh one. Caller must hold ``_lock``."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - already discarding
                logger.debug("discarding %s client failed", self._role, exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._reset_locked()
