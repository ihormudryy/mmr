"""Command center wiring: FastAPI lifespan owns exactly one bridge, one
reducer (DashboardState), and one fan-out registry (spec §5.1, §4.3)."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from web.command_center.bridge import DashboardEventBridge
from web.command_center.quotes import QuotePlane
from web.command_center.session import (
    DashboardCredentials,
    FailedAttemptLimiter,
    SessionManager,
    build_require_session,
    load_dashboard_credentials,
)
from web.command_center.sse import SseFanout
from web.command_center.state import DashboardState

logger = logging.getLogger("web.command_center")

GRACEFUL_SHUTDOWN_SECONDS = 5


@dataclass(frozen=True)
class CommandCenterConfig:
    typed_query_endpoint: str = "tcp://127.0.0.1:42101"
    typed_feed_endpoint: str = "tcp://127.0.0.1:42103"
    pubsub_address: str = "tcp://127.0.0.1"
    pubsub_port: int = 42002
    quote_hz: float = 4.0
    cookie_secure: bool = False

    @classmethod
    def from_env(cls, env=os.environ) -> "CommandCenterConfig":
        return cls(
            typed_query_endpoint=env.get("MMR_TYPED_QUERY_ENDPOINT",
                                         cls.typed_query_endpoint),
            typed_feed_endpoint=env.get("MMR_TYPED_FEED_ENDPOINT",
                                        cls.typed_feed_endpoint),
            pubsub_address=env.get("MMR_PUBSUB_ADDRESS", cls.pubsub_address),
            pubsub_port=int(env.get("MMR_PUBSUB_PORT", cls.pubsub_port)),
            quote_hz=float(env.get("CC_QUOTE_HZ", cls.quote_hz)),
            cookie_secure=env.get("DASHBOARD_COOKIE_SECURE", "").lower()
            in ("1", "true", "yes"),
        )


def _assert_single_worker(env=os.environ) -> None:
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = (env.get(var) or "").strip()
        if value and value != "1":
            raise RuntimeError(
                f"command center requires exactly one worker; {var}={value}")


def _assert_dill_strict(env=os.environ) -> None:
    if env.get("MMR_DILL_STRICT") != "1":
        raise RuntimeError(
            "command center requires MMR_DILL_STRICT=1: the dashboard process "
            "never executes dill payloads")


def _parse_typed_endpoint(endpoint: str) -> tuple[str, int]:
    """Split a ``tcp://host:port`` endpoint into ``TypedRpcClient``'s
    separate ``address``/``port`` constructor args."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"typed endpoint must be tcp://host:port, got {endpoint!r}")
    return f"tcp://{parsed.hostname}", parsed.port


def _typed_authenticator(env=os.environ):
    """Build the shared-secret HMAC authenticator the typed clients sign
    requests with (see ``trader/messaging/typed_rpc.py``). Reads the same
    kind of service-HMAC key file the typed transport already documents
    (``config_defaults/trader.yaml``'s ``service_hmac_key_file``) -- a
    dashboard without the shared service credential fails closed (via
    ``load_service_hmac_key``'s own checks) rather than falling back to an
    ad-hoc key."""
    from trader.messaging.typed_rpc import HmacServiceAuthenticator, load_service_hmac_key
    key_file = (env.get("MMR_SERVICE_HMAC_KEY_FILE") or "").strip()
    return HmacServiceAuthenticator(load_service_hmac_key(key_file))


def _default_query_client(config: CommandCenterConfig):
    from trader.messaging.typed_rpc import TypedRpcClient
    address, port = _parse_typed_endpoint(config.typed_query_endpoint)
    client = TypedRpcClient("query", _typed_authenticator(), address=address, port=port)
    client.connect()
    return client


def _default_feed_client(config: CommandCenterConfig):
    from trader.messaging.typed_rpc import TypedRpcClient
    address, port = _parse_typed_endpoint(config.typed_feed_endpoint)
    # The feed client long-polls with a 10s wait_ms (see DashboardEventBridge
    # defaults); its own call timeout must comfortably exceed that.
    client = TypedRpcClient("feed", _typed_authenticator(), address=address, port=port,
                            timeout=15.0)
    client.connect()
    return client


class CommandCenter:
    def __init__(self, config: CommandCenterConfig, *,
                 credentials_loader: Callable[[], DashboardCredentials]
                 = load_dashboard_credentials,
                 query_client_factory=None,
                 feed_client_factory=None,
                 bridge_factory=None,
                 quote_plane_factory=None,
                 command_gateway_factory=None):
        self.config = config
        self._credentials_loader = credentials_loader
        self._query_client_factory = (query_client_factory
                                      or (lambda: _default_query_client(config)))
        self._feed_client_factory = (feed_client_factory
                                     or (lambda: _default_feed_client(config)))
        self._bridge_factory = bridge_factory or DashboardEventBridge
        self._quote_plane_factory = quote_plane_factory or (
            lambda loop, deliver: QuotePlane(
                config.pubsub_address, config.pubsub_port, loop, deliver,
                hz=config.quote_hz))
        # Reserved seam for [M1-C]: DashboardCommandGateway lives here.
        self._command_gateway_factory = command_gateway_factory
        self.command_gateway = None
        self.state = DashboardState()
        self.fanout = SseFanout(self.state)
        self.session_manager: Optional[SessionManager] = None
        self.limiter = FailedAttemptLimiter()
        self.bridge: Optional[DashboardEventBridge] = None
        self.quote_plane: Optional[QuotePlane] = None
        self._query_client = None
        self._feed_client = None

    def ensure_session_manager(self) -> SessionManager:
        """Raises CredentialConfigError on missing credentials — called at
        lifespan startup so a misconfigured dashboard fails to start."""
        if self.session_manager is None:
            self.session_manager = SessionManager(self._credentials_loader())
        return self.session_manager

    @property
    def require_session(self):
        manager = self.ensure_session_manager()
        return build_require_session(manager)

    @asynccontextmanager
    async def lifespan(self, app):
        _assert_single_worker()
        _assert_dill_strict()
        self.ensure_session_manager()
        loop = asyncio.get_running_loop()
        self._query_client = self._query_client_factory()
        self._feed_client = self._feed_client_factory()
        self.bridge = self._bridge_factory(
            self._query_client, self._feed_client, self.state, self.fanout, loop)
        if hasattr(self.bridge, "start"):
            self.bridge.start()
        self.quote_plane = self._quote_plane_factory(loop, self.fanout.publish_quotes)
        self.quote_plane.start()
        if self._command_gateway_factory is not None:  # [M1-C] wires this
            self.command_gateway = self._command_gateway_factory(self)
        try:
            yield
        finally:
            # Uvicorn has already drained or cancelled SSE responses within its
            # 5-second graceful-shutdown bound; generators unregister in finally.
            remaining = self.fanout.client_count()
            if remaining:
                logger.warning("lifespan teardown with %d SSE clients still "
                               "registered", remaining)
            self.quote_plane.stop()
            self.bridge.stop()
            for client in (self._query_client, self._feed_client):
                if client is not None and hasattr(client, "close"):
                    client.close()
