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
from urllib.parse import urlparse

from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRemoteError,
    load_service_hmac_key,
)

logger = logging.getLogger(__name__)

_DEFAULT_TRADER_QUERY = 'tcp://127.0.0.1:42101'
_DEFAULT_TRADER_COMMAND = 'tcp://127.0.0.1:42102'
_DEFAULT_STRATEGY_QUERY = 'tcp://127.0.0.1:42105'
_DEFAULT_STRATEGY_COMMAND = 'tcp://127.0.0.1:42104'


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint)
    if parsed.scheme != 'tcp' or not parsed.hostname or parsed.port is None:
        raise ValueError(f'typed endpoint must be tcp://host:port, got {endpoint!r}')
    return f'tcp://{parsed.hostname}', parsed.port


def _authenticator(env: os._Environ = os.environ) -> HmacServiceAuthenticator:
    key_path = env.get('MMR_SERVICE_HMAC_KEY_FILE', '~/.config/mmr/service_hmac.key')
    return HmacServiceAuthenticator(load_service_hmac_key(key_path))


class ManageRpcClient:
    """Lazy, thread-safe typed clients for manage-page operations."""

    def __init__(self, *, client_factory: Callable[[str, str], TypedRpcClient] | None = None,
                 timeout_s: float = 15.0, env: os._Environ = os.environ):
        self._timeout_s = timeout_s
        self._env = env
        self._client_factory = client_factory or self._default_client_factory
        self._clients: dict[str, TypedRpcClient | None] = {
            'trader_query': None, 'trader_command': None,
            'strategy_query': None, 'strategy_command': None,
        }
        self._lock = threading.Lock()

    def _default_client_factory(self, role: str, endpoint: str) -> TypedRpcClient:
        address, port = _split_endpoint(endpoint)
        return TypedRpcClient(role, _authenticator(self._env), address=address,
                              port=port, timeout=self._timeout_s)

    def _endpoint(self, var: str, default: str) -> str:
        return self._env.get(var, default)

    def _call(self, bucket: str, role: str, endpoint: str, method: str,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = body if body is not None else {}
        with self._lock:
            client = self._clients[bucket]
            if client is None:
                client = self._client_factory(role, endpoint)
                client.connect()
                self._clients[bucket] = client
        try:
            return client.call(method, payload, dict)
        except (TypedRpcRemoteError, TimeoutError, OSError) as exc:
            logger.warning('manage typed call %s failed: %s', method, exc)
            with self._lock:
                client = self._clients[bucket]
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._clients[bucket] = None
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
            timeout_s = float(os.environ.get('MMR_MANAGE_RPC_TIMEOUT_S', '10'))
            _CLIENT = ManageRpcClient(timeout_s=timeout_s)
        return _CLIENT


def reset_manage_client_for_tests() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None
