import json
import math
from unittest.mock import MagicMock

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from twelvedata.exceptions import BadRequestError, InvalidApiKeyError

from trader.data_providers.capabilities import FX_CONVERSION_FIELDS, FX_RATE_FIELDS
from trader.data_providers.errors import CapabilityNotSupported, ProviderEntitlementError, ProviderError
from trader.data_providers.twelvedata.forex import TwelveDataForex


class _StubTDPayload:
    def __init__(self, payload):
        self._payload = payload

    def as_json(self):
        return self._payload


QUOTE = {
    'symbol': 'EUR/USD', 'open': '1.08200', 'high': '1.08500', 'low': '1.08000', 'close': '1.08300',
    'volume': '0', 'previous_close': '1.08100', 'change': '0.00200', 'percent_change': '0.18500',
    'datetime': '2026-04-29', 'timestamp': 1777000000, 'is_market_open': True,
}


def test_rate_from_quote_payload():
    client = MagicMock()
    client.quote.return_value = _StubTDPayload(QUOTE)
    out = TwelveDataForex(client).rate('EUR', 'USD')
    client.quote.assert_called_once_with(symbol='EUR/USD')
    assert tuple(out) == FX_RATE_FIELDS
    assert out['pair'] == 'EUR/USD'
    assert out['close'] == pytest.approx(1.083) and out['last'] == pytest.approx(1.083)
    assert out['previous_close'] == pytest.approx(1.081)
    assert out['change_pct'] == pytest.approx(0.185)
    assert out['as_of'] == '2026-04-29' and out['source'] == 'twelvedata' and out['note'] == 'market open'
    assert math.isnan(out['bid']) and math.isnan(out['ask'])


def test_rate_market_closed_note_and_blank_numbers():
    client = MagicMock()
    client.quote.return_value = _StubTDPayload(dict(QUOTE, is_market_open=False, volume=''))
    out = TwelveDataForex(client).rate('EUR', 'USD')
    assert out['note'] == 'market closed' and math.isnan(out['volume'])


def test_bad_request_maps_to_provider_error():
    client = MagicMock()
    client.quote.side_effect = BadRequestError('**symbol** not found')
    with pytest.raises(ProviderError, match='not found') as info:
        TwelveDataForex(client).rate('EUR', 'XYZ')
    assert not isinstance(info.value, ProviderEntitlementError)


def test_invalid_api_key_is_entitlement_error():
    client = MagicMock()
    client.currency_conversion.side_effect = InvalidApiKeyError('apikey is invalid')
    with pytest.raises(ProviderEntitlementError, match='apikey is invalid'):
        TwelveDataForex(client).convert('EUR', 'USD', 1.0)


def test_network_failure_is_provider_error():
    client = MagicMock()
    client.quote.side_effect = RequestsConnectionError('Max retries exceeded')
    with pytest.raises(ProviderError, match='Max retries exceeded'):
        TwelveDataForex(client).rate('EUR', 'USD')


def test_invalid_json_body_is_provider_error():
    client = MagicMock()
    client.currency_conversion.side_effect = json.JSONDecodeError('Expecting value', '<html>', 0)
    with pytest.raises(ProviderError, match='Expecting value'):
        TwelveDataForex(client).convert('EUR', 'USD', 1.0)


def test_convert():
    client = MagicMock()
    client.currency_conversion.return_value = _StubTDPayload({
        'symbol': 'EUR/USD', 'rate': 1.1678, 'amount': 116.78, 'timestamp': 1777000000,
    })
    out = TwelveDataForex(client).convert('EUR', 'USD', 100.0)
    client.currency_conversion.assert_called_once_with(symbol='EUR/USD', amount=100.0)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['from'] == 'EUR' and out['to'] == 'USD' and out['amount'] == 100.0
    assert out['converted'] == pytest.approx(116.78) and out['rate'] == pytest.approx(1.1678)
    assert out['as_of'] == '1777000000' and out['source'] == 'twelvedata'


def test_rates_for_every_pair_not_supported():
    with pytest.raises(CapabilityNotSupported) as info:
        TwelveDataForex(MagicMock()).rates('USD', None)
    assert info.value.supported == ['frankfurter', 'massive']
