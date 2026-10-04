"""Thin authenticated client for Alpaca's market-data REST API."""

from typing import Any, Iterator, Mapping, Optional

import requests

from trader.data_providers.errors import ProviderEntitlementError, ProviderError
from trader.data_providers.rate_limit import RateLimiter, call_with_retry

ALPACA_DATA_URL = 'https://data.alpaca.markets'
REQUEST_TIMEOUT_SECS = 30

# Basic plan: 200 REST calls per minute, shared by every client in this process.
ALPACA_LIMITER = RateLimiter(200, 60.0)


class AlpacaClient:
    """One requests.Session per client, and one client per provider instance.

    data_service builds a fresh provider (and so a fresh client) for each download
    task. requests.Session is not formally thread-safe, so keep per-call state out
    of this class."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        session: Optional[requests.Session] = None,
        limiter: Optional[RateLimiter] = None,
        base_url: str = ALPACA_DATA_URL,
    ):
        self._headers = {'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret_key}
        self._session = session or requests.Session()
        self._limiter = limiter or ALPACA_LIMITER
        self._base_url = base_url

    def get_json(self, path: str, params: Mapping[str, Any]) -> dict:
        response = call_with_retry(
            lambda: self._send(path, params),
            provider='alpaca',
        )
        if response.status_code >= 400:
            raise _error_for(path, response)
        return response.json()

    def _send(self, path: str, params: Mapping[str, Any]):
        self._limiter.acquire()
        return self._session.get(
            self._base_url + path,
            params=dict(params),
            headers=self._headers,
            timeout=REQUEST_TIMEOUT_SECS,
        )

    def paginate(self, path: str, params: Mapping[str, Any]) -> Iterator[dict]:
        page_params = dict(params)
        while True:
            page = self.get_json(path, page_params)
            yield page
            token = page.get('next_page_token')
            if not token:
                return
            page_params['page_token'] = token


def _error_for(path: str, response) -> ProviderError:
    try:
        message = response.json().get('message', '')
    except ValueError:
        message = ''
    if response.status_code == 401:
        return ProviderEntitlementError(
            'alpaca rejected the API key (HTTP 401); check ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY'
        )
    if response.status_code == 403:
        return ProviderEntitlementError(f'alpaca refused {path}: {message}')
    return ProviderError(f'alpaca {path} failed: HTTP {response.status_code} {message}')
