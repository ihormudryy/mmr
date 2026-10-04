import pytest

from trader.data_providers.alpaca.client import AlpacaClient
from trader.data_providers.errors import ProviderEntitlementError, ProviderError


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({'url': url, 'params': dict(params or {}), 'headers': headers, 'timeout': timeout})
        return self._responses.pop(0)


class NoWaitLimiter:
    def acquire(self):
        pass


class CountingLimiter:
    def __init__(self):
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1


def _client(*responses):
    session = FakeSession(responses)
    return AlpacaClient('kid', 'secret', session=session, limiter=NoWaitLimiter()), session


def test_sends_auth_headers_and_timeout():
    client, session = _client(FakeResponse(200, {'ok': True}))
    assert client.get_json('/v2/x', {'a': 1}) == {'ok': True}
    sent = session.requests[0]
    assert sent['url'] == 'https://data.alpaca.markets/v2/x'
    assert sent['headers'] == {'APCA-API-KEY-ID': 'kid', 'APCA-API-SECRET-KEY': 'secret'}
    assert sent['params'] == {'a': 1}
    assert sent['timeout'] == 30


def test_403_raises_entitlement_error_with_message():
    client, _ = _client(FakeResponse(403, {'message': 'subscription does not permit querying recent SIP data'}))
    with pytest.raises(ProviderEntitlementError, match='recent SIP data'):
        client.get_json('/v2/stocks/bars', {})


def test_401_raises_entitlement_error():
    client, _ = _client(FakeResponse(401, {'message': 'unauthorized'}))
    with pytest.raises(ProviderEntitlementError, match='ALPACA_API_KEY_ID'):
        client.get_json('/v2/stocks/bars', {})


def test_other_http_errors_raise_provider_error():
    client, _ = _client(FakeResponse(400, {'message': 'invalid timeframe: 1Sec'}))
    with pytest.raises(ProviderError, match='HTTP 400 invalid timeframe'):
        client.get_json('/v2/stocks/bars', {})


def test_paginate_follows_next_page_token():
    client, session = _client(
        FakeResponse(200, {'bars': {'AAPL': [1]}, 'next_page_token': 'p2'}),
        FakeResponse(200, {'bars': {'AAPL': [2]}, 'next_page_token': None}),
    )
    pages = list(client.paginate('/v2/stocks/bars', {'symbols': 'AAPL'}))
    assert [p['bars']['AAPL'] for p in pages] == [[1], [2]]
    assert 'page_token' not in session.requests[0]['params']
    assert session.requests[1]['params']['page_token'] == 'p2'
    assert session.requests[1]['params']['symbols'] == 'AAPL'


def test_limiter_acquired_on_429_retry():
    """Verify that the limiter is acquired for each HTTP attempt, including retries on 429."""
    limiter = CountingLimiter()
    session = FakeSession([
        FakeResponse(429, {'message': 'rate limited'}, headers={'Retry-After': '0'}),
        FakeResponse(200, {'ok': True}),
    ])
    client = AlpacaClient('kid', 'secret', session=session, limiter=limiter)

    result = client.get_json('/v2/stocks/bars', {})

    assert result == {'ok': True}
    assert limiter.acquire_count == 2, f"Expected limiter.acquire() to be called 2 times, got {limiter.acquire_count}"
