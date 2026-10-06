"""Shared typed-RPC transport to the trader / strategy services.

One ``TraderLink`` == one typed-RPC socket (a role at a ``tcp://host:port``
endpoint). It owns the plumbing the dashboard's typed clients used to each
re-implement: endpoint parsing, dashboard-identity construction, lazy client
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
  ``parse_endpoint`` / ``build_identity`` / ``connect_client`` below.

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

from trader.messaging.typed_rpc import ServiceIdentity, TypedRpcClient

logger = logging.getLogger("web.trader_link")

DASHBOARD_PRINCIPAL = "dashboard"


# ── construction primitives (shared by TraderLink and the event bridge) ──────

def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Split a ``tcp://host:port`` endpoint into ``TypedRpcClient``'s separate
    ``address``/``port`` constructor args. Fails loudly on any other shape."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"typed endpoint must be tcp://host:port, got {endpoint!r}")
    return f"tcp://{parsed.hostname}", parsed.port


def build_identity(env: Mapping[str, str] = os.environ) -> ServiceIdentity:
    """Load the dashboard's Ed25519 identity (own key + trader/strategy public keys).

    The keys directory is ``MMR_RPC_KEYS_DIR`` or ``~/.config/mmr/keys/rpc``.
    A missing or unsafe key fails loudly; there is no fallback key.
    """
    return ServiceIdentity.load(DASHBOARD_PRINCIPAL, env.get("MMR_RPC_KEYS_DIR") or None)


def connect_client(
    role: str,
    endpoint: str,
    *,
    server: str = "trader",
    identity: ServiceIdentity | None = None,
    timeout: float = 10.0,
    env: Mapping[str, str] = os.environ,
) -> TypedRpcClient:
    """Build AND connect a ``TypedRpcClient`` for ``role`` at ``endpoint`` on ``server``."""
    address, port = parse_endpoint(endpoint)
    client = TypedRpcClient(
        role, identity or build_identity(env), server=server,
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
        server: str = "trader",
        identity: ServiceIdentity | None = None,
        timeout: float = 10.0,
        env: Mapping[str, str] = os.environ,
        client_factory: Callable[[], TypedRpcClient] | None = None,
    ):
        if client_factory is None and (role is None or endpoint is None):
            raise ValueError("TraderLink needs either a client_factory or (role, endpoint)")
        self._role = role
        self._endpoint = endpoint
        self._server = server
        self._identity = identity
        self._timeout = timeout
        self._env = env
        self._client_factory = client_factory or self._default_factory
        self._client: TypedRpcClient | None = None
        self._lock = threading.Lock()

    def _default_factory(self) -> TypedRpcClient:
        return connect_client(
            self._role, self._endpoint, server=self._server, identity=self._identity,
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
