import datetime as dt
import json
import math
from pathlib import Path

import pytest
import requests

from trader.data_providers.capabilities import FX_CONVERSION_FIELDS, FX_RATE_FIELDS, FX_RATES_COLUMNS, Capability
from trader.data_providers.errors import ProviderError, ProviderRateLimited
from trader.data_providers.frankfurter import ECB_NOTE, FrankfurterClient, FrankfurterForex, percent_change
from trader.data_providers.registry import ProviderRegistry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'frankfurter_ecb_majors.json').read_text())
SUNDAY = dt.date(2026, 10, 4)


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.requests.append({'url': url, 'params': dict(params or {}), 'timeout': timeout})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class NoWaitLimiter:
    def acquire(self):
        pass


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.rows


def _client(*responses):
    session = FakeSession(responses)
    return FrankfurterClient(session=session, limiter=NoWaitLimiter()), session


def _forex(rows=FIXTURE):
    client = FakeClient(rows)
    return FrankfurterForex(client, today=lambda: SUNDAY), client


def test_client_sends_params_and_timeout():
    client, session = _client(FakeResponse(200, []))
    assert client.get_json('/v2/rates', {'base': 'EUR'}) == []
    assert session.requests == [{'url': 'https://api.frankfurter.dev/v2/rates', 'params': {'base': 'EUR'},
                                 'timeout': 30}]


@pytest.mark.parametrize('status', [404, 422])
def test_client_maps_bad_input_to_value_error(status):
    client, _ = _client(FakeResponse(status, {'status': status, 'message': 'invalid currency: XXX'}))
    with pytest.raises(ValueError, match='invalid currency: XXX'):
        client.get_json('/v2/rates', {})


def test_client_other_http_error_is_provider_error():
    client, _ = _client(FakeResponse(500, {'message': 'boom'}))
    with pytest.raises(ProviderError, match='HTTP 500 boom'):
        client.get_json('/v2/rates', {})


@pytest.mark.parametrize('failure', [requests.ConnectionError('down'), requests.Timeout('slow')])
def test_client_network_error_is_provider_error(failure):
    client, _ = _client(failure)
    with pytest.raises(ProviderError, match='request failed'):
        client.get_json('/v2/rates', {})


def test_client_non_json_body_is_provider_error():
    client, _ = _client(FakeResponse(200, ValueError('Expecting value')))
    with pytest.raises(ProviderError, match='not JSON'):
        client.get_json('/v2/rates', {})


def test_client_429_retries_then_rate_limited():
    limited = FakeResponse(429, {'message': 'slow down'}, headers={'Retry-After': '0'})
    client, session = _client(limited, limited, limited)
    with pytest.raises(ProviderRateLimited):
        client.get_json('/v2/rates', {})
    assert len(session.requests) == 3


def test_percent_change():
    assert percent_change(1.1298, 1.1225) == pytest.approx(-0.6461, abs=1e-4)


def test_rate_uses_two_latest_complete_ecb_dates():
    forex, client = _forex()
    rate = forex.rate('EUR', 'USD')
    assert client.calls == [('/v2/rates', {'base': 'EUR', 'quotes': 'USD', 'providers': 'ECB',
                                           'from': '2026-09-20', 'to': '2026-10-04'})]
    assert tuple(rate) == FX_RATE_FIELDS
    assert rate['last'] == 1.1225 and rate['close'] == 1.1225 and rate['previous_close'] == 1.1298
    assert rate['change'] == pytest.approx(-0.0073)
    assert rate['change_pct'] == pytest.approx(-0.6461, abs=1e-4)
    assert rate['as_of'] == '2026-10-02' and rate['source'] == 'frankfurter' and rate['note'] == ECB_NOTE
    assert math.isnan(rate['bid']) and math.isnan(rate['ask'])


def test_cross_rate_via_eur():
    forex, client = _forex()
    rate = forex.rate('usd', 'jpy')
    assert client.calls[0][1]['quotes'] == 'JPY,USD'
    assert rate['pair'] == 'USD/JPY'
    assert rate['last'] == pytest.approx(176.99 / 1.1225, rel=1e-9)
    assert rate['previous_close'] == pytest.approx(178.49 / 1.1298, rel=1e-9)


def test_eur_quote_is_kept_exactly_when_ecb_publishes_many_digits():
    rows = [{'date': '2026-10-01', 'base': 'EUR', 'quote': 'IDR', 'rate': 18101.5},
            {'date': '2026-10-02', 'base': 'EUR', 'quote': 'IDR', 'rate': 18226.43}]
    rate = _forex(rows)[0].rate('EUR', 'IDR')
    assert rate['last'] == 18226.43 and rate['previous_close'] == 18101.5


def test_date_missing_a_currency_is_skipped():
    rows = [r for r in FIXTURE if not (r['date'] == '2026-10-02' and r['quote'] == 'JPY')]
    rate = _forex(rows)[0].rate('USD', 'JPY')
    assert rate['as_of'] == '2026-10-01'
    assert rate['last'] == pytest.approx(178.49 / 1.1298, rel=1e-9)
    assert rate['previous_close'] == pytest.approx(178.27 / 1.1355, rel=1e-9)


def test_fewer_than_two_dates_raises():
    rows = [r for r in FIXTURE if r['date'] == '2026-10-02']
    with pytest.raises(ProviderError, match='need 2'):
        _forex(rows)[0].rate('EUR', 'USD')


def test_currency_ecb_does_not_publish_raises():
    with pytest.raises(ValueError, match='ECB publishes no daily rate for COP'):
        _forex()[0].rate('USD', 'COP')


def test_empty_response_is_provider_error():
    with pytest.raises(ProviderError, match='no ECB rates'):
        _forex([])[0].rate('EUR', 'USD')


def test_rate_against_other_base_is_provider_error():
    rows = [dict(r, base='USD') for r in FIXTURE]
    with pytest.raises(ProviderError, match="'USD'"):
        _forex(rows)[0].rate('EUR', 'JPY')


@pytest.mark.parametrize('payload', [{'message': 'x'}, ['not a row'], [{'date': '2026-10-02', 'base': 'EUR'}]])
def test_malformed_payload_is_provider_error(payload):
    with pytest.raises(ProviderError, match='unexpected'):
        _forex(payload)[0].rate('EUR', 'USD')


@pytest.mark.parametrize('bad_rate', [None, 'n/a', 0, -1.2, float('inf'), float('nan')])
def test_unusable_rate_is_provider_error(bad_rate):
    rows = [dict(r, rate=bad_rate) if (r['date'], r['quote']) == ('2026-10-02', 'USD') else r for r in FIXTURE]
    with pytest.raises(ProviderError, match='unusable rate'):
        _forex(rows)[0].rate('EUR', 'USD')


def test_invalid_currency_raises_before_request():
    forex, client = _forex()
    for base, quote in (('EURO', 'USD'), ('EUR', 'EUR'), ('', 'USD')):
        with pytest.raises(ValueError):
            forex.rate(base, quote)
    assert client.calls == []


def test_convert_matches_frankfurter_amount_math():
    out = _forex()[0].convert('EUR', 'USD', 1000.0)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['converted'] == pytest.approx(1122.5) and out['rate'] == 1.1225
    assert out['as_of'] == '2026-10-02' and out['source'] == 'frankfurter' and out['note'] == ECB_NOTE
    assert math.isnan(out['bid'])


def test_rates_for_symbols():
    frame = _forex()[0].rates('USD', ['JPY', 'CAD'])
    assert tuple(frame.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS
    assert frame['pair'].tolist() == ['USD/CAD', 'USD/JPY']
    assert set(frame['as_of']) == {'2026-10-02'} and set(frame['source']) == {'frankfurter'}
    assert set(frame['note']) == {ECB_NOTE}


def test_rates_without_symbols_covers_every_ecb_currency():
    rows = FIXTURE + [{'date': d, 'base': 'EUR', 'quote': 'EUR', 'rate': 1.0} for d in ('2026-10-01', '2026-10-02')]
    forex, client = _forex(rows)
    frame = forex.rates('USD', None)
    assert 'quotes' not in client.calls[0][1]
    assert sorted(frame['pair']) == ['USD/AUD', 'USD/CAD', 'USD/CHF', 'USD/EUR', 'USD/GBP', 'USD/JPY', 'USD/NZD']


def test_rates_base_not_published_raises():
    with pytest.raises(ValueError, match='COP'):
        _forex()[0].rates('COP', None)


def test_registry_frankfurter_needs_no_key_and_is_forex_default():
    from trader.data_providers.builtin import forex_quote_source_choices
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.FOREX) == 'frankfurter'
    assert isinstance(registry.get(Capability.FOREX), FrankfurterForex)
    assert forex_quote_source_choices() == ['ib', 'frankfurter', 'massive', 'twelvedata']


def test_change_is_rounded_to_ten_significant_digits():
    rate = _forex()[0].rate('EUR', 'USD')
    assert rate['change'] == -0.0073


def test_rates_frame_carries_the_ecb_date():
    assert _forex()[0].rates('USD', ['JPY']).attrs['as_of'] == '2026-10-02'


def test_rates_without_symbols_keeps_a_currency_missing_on_one_date():
    rows = [r for r in FIXTURE if (r['date'], r['quote']) not in {('2026-10-02', 'NZD'), ('2026-10-01', 'AUD')}]
    frame = _forex(rows)[0].rates('USD', None).set_index('pair')
    assert sorted(frame.index) == ['USD/AUD', 'USD/CAD', 'USD/CHF', 'USD/EUR', 'USD/GBP', 'USD/JPY', 'USD/NZD']
    nzd, aud = frame.loc['USD/NZD'], frame.loc['USD/AUD']
    assert math.isnan(nzd['last']) and math.isnan(nzd['change']) and math.isnan(nzd['change_pct'])
    assert nzd['note'] == f'{ECB_NOTE}; NZD not published on 2026-10-02'
    assert aud['last'] == pytest.approx(1.6176 / 1.1225) and math.isnan(aud['previous_close'])
    assert math.isnan(aud['change_pct']) and aud['note'] == f'{ECB_NOTE}; AUD not published on 2026-10-01'
    assert frame.loc['USD/JPY', 'note'] == ECB_NOTE
