"""[M1-C] DashboardCommandGateway (spec Section 5.3).

A command-only typed RPC connection, independent from the event bridge and
snapshot clients (see ``web/command_center/bridge.py`` and
``web/command_center/__init__.py``'s ``_default_query_client``/
``_default_feed_client``): its own ``TypedRpcClient``, its own timeout, and
its own serialization lock, so a slow read on the query/feed sockets can
never block an urgent command. Maps ``TypedRpcRemoteError`` and transport
failures (timeout, connection loss) to the stable Section 11 error contract:
``{code, safe message, retryable, correlation_id}``.

This module deliberately does not wire itself into ``CommandCenter`` --
``web/command_center/__init__.py`` already reserves a
``command_gateway_factory`` seam for that (a later integration task); this
task only pins the gateway's own shape and its error-mapping contract,
proven against a fake typed client since the real ``preflight_command``
typed method does not exist upstream yet.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from trader.domain.commands import CommandReceipt
from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRemoteError,
    load_service_hmac_key,
)

logger = logging.getLogger("web.command_center.gateway")

# Codes a client may retry after fixing the stated condition. Everything
# else is final for that command_id (retrying returns the ledger outcome).
_RETRYABLE_CODES = frozenset({
    "QUOTE_MISSING", "QUOTE_STALE", "PREFLIGHT_EXPIRED",
    "DEPENDENCY_UNAVAILABLE", "COMMAND_CHANNEL_DOWN",
})


@dataclass(frozen=True)
class GatewayError(Exception):
    """The stable dashboard-command error envelope (spec Section 11).

    ``code`` is a stable machine-readable string a client can branch on
    without string-matching ``message`` (which is a safe, human-readable
    summary -- never a raw exception traceback or internal detail).
    ``retryable`` tells the caller whether resubmitting the same logical
    command might succeed; ``correlation_id`` ties the failure back to the
    ``command_id`` the caller sent, even when the underlying failure (a
    timeout, a dropped socket) never got far enough to produce one itself.
    """

    code: str
    message: str
    retryable: bool
    correlation_id: str | None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class PreflightTicket:
    command_id: str
    nonce: str
    expires_at: str
    summary: dict[str, Any]


class DashboardCommandGateway:
    """Owns a single command-socket ``TypedRpcClient``, built lazily (and
    rebuilt after any timeout/connection failure) via ``client_factory``.

    ``_lock`` serializes calls onto that one client -- exactly one command in
    flight at a time on THIS socket -- and is never shared with the event
    bridge's query/feed clients, so a slow snapshot read elsewhere can never
    stall an urgent approve/reject/cancel.
    """

    def __init__(self, client_factory: Callable[[], TypedRpcClient],
                 timeout_s: float = 5.0):
        self._client_factory = client_factory
        self._client: TypedRpcClient | None = None
        self._lock = threading.Lock()  # command-only; never shared with reads
        self._timeout_s = timeout_s

    # -- typed call plumbing -------------------------------------------------
    def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        correlation = body.get("command_id")
        with self._lock:
            try:
                if self._client is None:
                    self._client = self._client_factory()
                return self._client.call(method, body, dict)
            except TypedRpcRemoteError as exc:
                code = getattr(exc, "code", "UPSTREAM_ERROR") or "UPSTREAM_ERROR"
                raise GatewayError(
                    code=code,
                    message=getattr(exc, "message", None) or str(exc),
                    retryable=code in _RETRYABLE_CODES,
                    correlation_id=correlation,
                ) from exc
            except TimeoutError as exc:
                # NOTE: TimeoutError is an OSError subclass in Python 3, so
                # this clause MUST precede the (ConnectionError, OSError)
                # clause below -- otherwise the broader OSError catch would
                # shadow it and mislabel a timeout as COMMAND_CHANNEL_DOWN.
                self._reset_locked()
                raise GatewayError(
                    code="OUTCOME_UNKNOWN",
                    message="no acknowledgement from the command coordinator; "
                            "reconciling by command id",
                    retryable=False,
                    correlation_id=correlation,
                ) from exc
            except (ConnectionError, OSError) as exc:
                self._reset_locked()
                raise GatewayError(
                    code="COMMAND_CHANNEL_DOWN",
                    message="command channel unavailable",
                    retryable=True,
                    correlation_id=correlation,
                ) from exc

    def _reset_locked(self) -> None:
        """Discard the current client (stale DEALER identity) so the next
        call builds a fresh one. Must be called only while holding ``_lock``.
        """
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - already discarding
                logger.debug("discarding command client failed", exc_info=True)

    # -- public surface ------------------------------------------------------
    def execute(self, method: str, body: dict[str, Any]) -> CommandReceipt:
        raw = self._call(method, body)
        receipt = CommandReceipt(
            command_id=raw["command_id"],
            correlation_id=raw["correlation_id"],
            state=raw["state"],
            outcome=raw.get("outcome"),
            error_code=raw.get("error_code"),
            retryable=bool(raw.get("retryable", False)),
        )
        if receipt.state == "REJECTED":
            message = (receipt.outcome or {}).get("message", "command rejected")
            raise GatewayError(
                code=receipt.error_code or "COMMAND_REJECTED",
                message=message,
                retryable=receipt.retryable,
                correlation_id=receipt.correlation_id,
            )
        return receipt

    def preflight(self, body: dict[str, Any]) -> PreflightTicket:
        """Pins the [M1-F3] ``preflight_command`` contract: request body
        ``{command_id, action, params, expected_version, session_fingerprint}``,
        response ``{command_id, nonce, expires_at, summary}`` where
        ``summary`` carries ``side, instrument, quantity, notional,
        order_type, latest_price, drift_bps, warnings, account_id,
        account_mode``. trader_service reconstructs the exact future
        ``CommandRequest`` and binds the nonce to its canonical hash and this
        browser session before returning the ticket.
        """
        raw = self._call("preflight_command", body)
        return PreflightTicket(
            command_id=raw["command_id"],
            nonce=raw["nonce"],
            expires_at=raw["expires_at"],
            summary=dict(raw["summary"]),
        )

    def get_command(self, command_id: str) -> CommandReceipt:
        raw = self._call("get_command", {"command_id": command_id})
        return CommandReceipt(
            command_id=raw["command_id"],
            correlation_id=raw["correlation_id"],
            state=raw["state"],
            outcome=raw.get("outcome"),
            error_code=raw.get("error_code"),
            retryable=bool(raw.get("retryable", False)),
        )


def _parse_typed_endpoint(endpoint: str) -> tuple[str, int]:
    """Split a ``tcp://host:port`` endpoint into ``TypedRpcClient``'s
    separate ``address``/``port`` constructor args.

    Deliberately duplicated (not imported) from
    ``web/command_center/__init__.py``'s identical helper: this module's own
    docstring commits to being independent from the event bridge/query-client
    wiring, so it doesn't reach into a sibling module's private helper for a
    one-off string split.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
        raise ValueError(f"typed command endpoint must be tcp://host:port, got {endpoint!r}")
    return f"tcp://{parsed.hostname}", parsed.port


def build_command_gateway(env: Mapping[str, str] = os.environ) -> DashboardCommandGateway:
    """Production wiring for ``DashboardCommandGateway``.

    Mirrors the conventions ``web/command_center/__init__.py`` already
    established for the query/feed clients: a single ``tcp://host:port``
    endpoint string (here ``MMR_TYPED_COMMAND_ENDPOINT``, default
    ``tcp://127.0.0.1:42102`` -- the trader command socket) and the shared
    service HMAC key file (``MMR_SERVICE_HMAC_KEY_FILE``) loaded via
    ``load_service_hmac_key``, which fails loudly (missing path, wrong
    permission bits, empty, or too-short key) rather than an unchecked raw
    file read -- consistent with this module's "fail loudly, not silently"
    posture for anything on the trading-authorization path.

    The key file is loaded and validated ONCE, here, not inside the per-
    reconnect client factory below -- ``DashboardCommandGateway`` rebuilds its
    client after every timeout/connection-error reset, and re-reading +
    re-validating the key file on every one of those would be both wasteful
    and a needless repeated stat/permission check for a value that cannot
    change mid-process.
    """
    endpoint = env.get("MMR_TYPED_COMMAND_ENDPOINT", "tcp://127.0.0.1:42102")
    address, port = _parse_typed_endpoint(endpoint)
    key_file = (env.get("MMR_SERVICE_HMAC_KEY_FILE") or "").strip()
    timeout_s = float(env.get("DASHBOARD_COMMAND_TIMEOUT_S", "5.0"))
    authenticator = HmacServiceAuthenticator(load_service_hmac_key(key_file))

    def _factory() -> TypedRpcClient:
        client = TypedRpcClient(
            "command", authenticator, address=address, port=port, timeout=timeout_s)
        client.connect()
        return client

    return DashboardCommandGateway(client_factory=_factory, timeout_s=timeout_s)
