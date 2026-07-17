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


def _default_command_gateway(env=os.environ):
    """Production wiring for the command gateway.

    Mirrors ``_default_query_client``/``_default_feed_client`` above: a lazy
    import (so `web.command_center.gateway` -- and the `trader.messaging.
    typed_rpc` service-HMAC-key loading it does at call time -- is only ever
    touched from inside `CommandCenter._start_or_degrade`'s try/except, never
    at module import or `create_app()` time). [M1-C] Task 3 fix (I-1): a
    missing/invalid `MMR_SERVICE_HMAC_KEY_FILE` raises here, which
    `_start_or_degrade` catches like any other startup failure -- it
    DEGRADES the command center to inert rather than crashing `create_app()`
    and taking the always-on `/healthz`/`/readyz` ops probes down with it.
    """
    from web.command_center.gateway import build_command_gateway
    return build_command_gateway(env)


class CommandCenter:
    def __init__(self, config: CommandCenterConfig, *,
                 credentials_loader: Callable[[], DashboardCredentials]
                 = load_dashboard_credentials,
                 query_client_factory=None,
                 feed_client_factory=None,
                 bridge_factory=None,
                 quote_plane_factory=None,
                 commands_enabled: bool = False,
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
        # [M1-C] Task 3 fix (I-1): the command gateway is built lazily, in
        # `_start_or_degrade`, inside the SAME degrade-tolerant try/except
        # that brings up the bridge + quote plane below -- never eagerly at
        # `create_app()` time (that used to raise past this constructor
        # entirely on a bad/missing service HMAC key, taking the whole ASGI
        # boot -- and its always-on `/healthz`/`/readyz` probes -- down with
        # it). `commands_enabled` mirrors `CommandFlags.commands_enabled`
        # (web/app.py, [M1-C] Task 3): a disabled deployment never attempts
        # the build at all (that's normal "off", not a failure); when
        # enabled, a factory failure degrades the WHOLE center to inert --
        # same as any other `_start_or_degrade` failure -- not just the
        # gateway.
        self._commands_enabled = commands_enabled
        self._command_gateway_factory = command_gateway_factory or _default_command_gateway
        self.command_gateway = None  # built by _start_or_degrade, see above
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
        """Bring up the command center, but NEVER abort ASGI startup.

        A misconfiguration (``MMR_DILL_STRICT`` unset, dashboard credentials
        missing) or a bridge/quote-plane/command-gateway start failure
        DEGRADES the command center to inert -- logged loudly -- and still
        yields, so the app (and its always-on ``/healthz`` / ``/readyz`` ops
        probes) boots regardless. This is where the command gateway (when
        ``commands_enabled``) is built too -- see the constructor's docstring
        for why that must NOT happen eagerly in ``create_app()`` instead
        ([M1-C] Task 3 fix I-1). Degrading here does NOT weaken security:

        * dashboard routes still fail loud *per request* -- ``require_session``
          -> ``ensure_session_manager()`` raises ``CredentialConfigError`` on a
          dashboard request without credentials (unchanged, request-scoped);
        * dill-strict is still enforced at the quote-decode seam
          (``quotes._default_decode`` raises unless ``MMR_DILL_STRICT`` is set),
          so relaxing the *startup* assertion to degrade-not-abort opens no dill
          execution hole -- the quote plane simply is not started in this path.
        """
        started = self._start_or_degrade()
        try:
            yield
        finally:
            if started:
                # Uvicorn has already drained or cancelled SSE responses within
                # its graceful-shutdown bound; generators unregister in finally.
                remaining = self.fanout.client_count()
                if remaining:
                    logger.warning("lifespan teardown with %d SSE clients still "
                                   "registered", remaining)
                self._teardown()

    def _start_or_degrade(self) -> bool:
        """Start the bridge + quote plane (+ command gateway, if enabled).
        Returns True on a full start, or False after catching+logging a
        startup failure (degraded/inert)."""
        try:
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
            self.quote_plane = self._quote_plane_factory(
                loop, self.fanout.publish_quotes)
            self.quote_plane.start()
            if self._commands_enabled:
                # [M1-C] Task 3 fix (I-1): built HERE (same try as bridge/
                # quote-plane), not eagerly in `create_app()` -- a bad/missing
                # service HMAC key (or any other gateway-factory failure) is
                # caught by the `except` below and DEGRADES the whole center
                # to inert, exactly like a dill-strict or credentials failure
                # would; it never aborts ASGI startup.
                self.command_gateway = self._command_gateway_factory()
            return True
        except Exception:  # noqa: BLE001 - degrade to inert, never abort the app
            logger.exception(
                "command center failed to start -- dashboard DEGRADED to inert "
                "(ops probes /healthz and /readyz still serve; dashboard routes "
                "fail loud per request)")
            # Partial-startup cleanup: if the bridge started but the quote plane
            # (or gateway) then failed, stop the bridge so its thread never
            # leaks. _teardown() is idempotent over the None-guarded handles.
            self._teardown()
            return False

    def _teardown(self) -> None:
        """Stop whatever came up, guarded so it is safe on a partial start."""
        if self.quote_plane is not None:
            try:
                self.quote_plane.stop()
            except Exception:  # noqa: BLE001
                logger.exception("quote plane stop failed during teardown")
            self.quote_plane = None
        if self.bridge is not None:
            try:
                self.bridge.stop()
            except Exception:  # noqa: BLE001
                logger.exception("bridge stop failed during teardown")
            self.bridge = None
        self.command_gateway = None
        for attr in ("_query_client", "_feed_client"):
            client = getattr(self, attr)
            if client is not None and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    logger.exception("%s close failed during teardown", attr)
            setattr(self, attr, None)
