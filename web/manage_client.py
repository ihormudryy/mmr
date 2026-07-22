"""Typed-RPC client for /manage (universes + strategy deploy helpers).

Uses the same HMAC-authenticated typed transport as the command center, but
targets trader query/command and strategy query/command endpoints directly
so the dashboard never opens legacy dill RPC or a local DuckDB file.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from trader.messaging.typed_rpc import (
    TypedRpcClient,
    TypedRpcRemoteError,
)
from web.trader_link import (
    TraderLink,
    TraderLinkError,
    build_authenticator,
    parse_endpoint,
)

logger = logging.getLogger(__name__)

_DEFAULT_TRADER_QUERY = 'tcp://127.0.0.1:42101'
_DEFAULT_TRADER_COMMAND = 'tcp://127.0.0.1:42102'
_DEFAULT_STRATEGY_QUERY = 'tcp://127.0.0.1:42105'
_DEFAULT_STRATEGY_COMMAND = 'tcp://127.0.0.1:42104'
_DEFAULT_HMAC_KEY_PATH = '~/.config/mmr/service_hmac.key'


class ManageRpcClient:
    """Lazy, thread-safe typed clients for manage-page operations.

    Each of the four buckets is a TraderLink (one typed-RPC socket) built on
    first use, sharing the transport plumbing (parse/auth/connect/reconnect/
    lock) with the command gateway via web/trader_link.py. This client keeps
    its caller contract: on a transport failure it re-raises the original
    ``TimeoutError``/``OSError`` (not TraderLinkError), and ``TypedRpcRemoteError``
    still propagates — so the manage routes' existing ``except`` clauses are
    unchanged.
    """

    def __init__(self, *, client_factory: Callable[[str, str], TypedRpcClient] | None = None,
                 timeout_s: float = 15.0, env: os._Environ = os.environ):
        self._timeout_s = timeout_s
        self._env = env
        self._client_factory = client_factory or self._default_client_factory
        self._links: dict[str, TraderLink | None] = {
            'trader_query': None, 'trader_command': None,
            'strategy_query': None, 'strategy_command': None,
        }
        self._lock = threading.Lock()

    def _default_client_factory(self, role: str, endpoint: str) -> TypedRpcClient:
        address, port = parse_endpoint(endpoint)
        return TypedRpcClient(
            role, build_authenticator(self._env, default_key_path=_DEFAULT_HMAC_KEY_PATH),
            address=address, port=port, timeout=self._timeout_s)

    def _endpoint(self, var: str, default: str) -> str:
        return self._env.get(var, default)

    # IB-backed manage commands (per-symbol resolve) routinely need >10s wall
    # when adding several tickers; keep a higher floor even if the env default
    # is left at the legacy short value.
    _IB_HEAVY_METHODS = frozenset({
        'add_universe_symbols',
        'import_universe_csv',
        'discover_instrument',
    })

    def _link_factory(self, role: str, endpoint: str) -> Callable[[], TypedRpcClient]:
        # TraderLink does not connect; the manage contract is "code connects,
        # not the injected factory" (its tests assert connect() is called), so
        # wrap the (role, endpoint) factory to build AND connect.
        def _build() -> TypedRpcClient:
            client = self._client_factory(role, endpoint)
            client.connect()
            return client
        return _build

    def _call(self, bucket: str, role: str, endpoint: str, method: str,
              body: dict[str, Any] | None = None,
              *, timeout: float | None = None) -> dict[str, Any]:
        payload = body if body is not None else {}
        call_timeout = timeout
        if call_timeout is None and method in self._IB_HEAVY_METHODS:
            call_timeout = max(self._timeout_s, 45.0)
        with self._lock:
            link = self._links[bucket]
            if link is None:
                link = TraderLink(role, endpoint,
                                  client_factory=self._link_factory(role, endpoint),
                                  timeout=self._timeout_s)
                self._links[bucket] = link
        try:
            return link.call(method, payload, timeout=call_timeout)
        except TraderLinkError as exc:
            # TraderLink already discarded its client; surface the ORIGINAL
            # TimeoutError/OSError so the manage routes' except clauses see the
            # same exception type they always have.
            logger.warning('manage typed call %s failed: %s', method, exc.cause or exc)
            raise (exc.cause or exc)
        except TypedRpcRemoteError as exc:
            # Healthy socket, application-level rejection: preserve the historical
            # discard-the-client-and-re-raise behaviour.
            logger.warning('manage typed call %s failed: %s', method, exc)
            link.close()
            raise

    def trader_query(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call('trader_query', 'query',
                          self._endpoint('MMR_TYPED_QUERY_ENDPOINT', _DEFAULT_TRADER_QUERY),
                          method, body)

    def trader_command(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call('trader_command', 'command',
                          self._endpoint('MMR_TYPED_COMMAND_ENDPOINT', _DEFAULT_TRADER_COMMAND),
                          method, body)

    def strategy_query(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call('strategy_query', 'query',
                          self._endpoint('MMR_TYPED_STRATEGY_QUERY_ENDPOINT', _DEFAULT_STRATEGY_QUERY),
                          method, body)

    def strategy_command(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call('strategy_command', 'command',
                          self._endpoint('MMR_TYPED_STRATEGY_COMMAND_ENDPOINT', _DEFAULT_STRATEGY_COMMAND),
                          method, body)


_CLIENT: ManageRpcClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_manage_client() -> ManageRpcClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            # Default 45s: watchlist add resolves via IB (multi-second/symbol).
            # Override with MMR_MANAGE_RPC_TIMEOUT_S; IB-heavy methods still
            # floor at 45s inside ManageRpcClient._call.
            timeout_s = float(os.environ.get('MMR_MANAGE_RPC_TIMEOUT_S', '45'))
            _CLIENT = ManageRpcClient(timeout_s=timeout_s)
        return _CLIENT


def reset_manage_client_for_tests() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None
