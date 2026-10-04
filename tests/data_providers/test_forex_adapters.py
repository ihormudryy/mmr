import json
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from massive.exceptions import AuthError, BadResponse
from urllib3.exceptions import MaxRetryError, ProtocolError, ResponseError

from trader.data_providers.capabilities import FX_CONVERSION_FIELDS, FX_RATE_FIELDS, FX_RATES_COLUMNS, Capability
from trader.data_providers.errors import ProviderEntitlementError, ProviderError, ProviderRateLimited
from trader.data_providers.massive.forex import MassiveForex
from trader.data_providers.massive.movers import MassiveMovers
from trader.data_providers.registry import ProviderRegistry


def _snap(ticker, change_pct, close=1.1):
    return SimpleNamespace(
        ticker=ticker,
        day=SimpleNamespace(open=1.0, high=1.2, low=0.9, close=close, volume=50.0),
        prev_day=SimpleNamespace(close=1.05),
        last_quote=SimpleNamespace(bid_price=1.0999, ask_price=1.1001),
        todays_change=0.05, todays_change_percent=change_pct, updated=1759500000000)


def _raw_snapshot(ticker='C:EURUSD', **overrides):
    """The documented Polygon/Massive forex ticker snapshot, as the HTTP body."""
    snapshot = {
        'ticker': ticker,
        'day': {'o': 1.0, 'h': 1.2, 'l': 0.9, 'c': 1.1, 'v': 50.0, 'vw': 1.05},
        'prevDay': {'o': 1.0, 'h': 1.1, 'l': 0.9, 'c': 1.05, 'v': 40.0},
        'lastQuote': {'a': 1.1001, 'b': 1.0999, 'i': 0, 't': 1759500000000, 'x': 48},
        'min': {'o': 1.1, 'h': 1.1, 'l': 1.1, 'c': 1.1, 'v': 1},
        'todaysChange': 0.05, 'todaysChangePerc': 4.7, 'updated': 1759500000000,
    }
    snapshot.update(overrides)
    return SimpleNamespace(data=json.dumps({'status': 'OK', 'ticker': snapshot}).encode())


def test_rate_maps_snapshot_including_bid_ask():
    client = MagicMock()
    client.get_snapshot_ticker.return_value = _raw_snapshot()
    rate = MassiveForex(client).rate('EUR', 'USD')
    client.get_snapshot_ticker.assert_called_once_with(market_type='forex', ticker='C:EURUSD', raw=True)
    assert tuple(rate) == FX_RATE_FIELDS
    assert rate['pair'] == 'EUR/USD' and rate['bid'] == 1.0999 and rate['ask'] == 1.1001
    assert rate['close'] == 1.1 and rate['last'] == 1.1
    assert rate['open'] == 1.0 and rate['high'] == 1.2 and rate['low'] == 0.9
    assert rate['previous_close'] == 1.05 and rate['change'] == 0.05 and rate['change_pct'] == 4.7
    assert rate['volume'] == 50.0 and rate['source'] == 'massive' and rate['as_of'] == '1759500000000'


def test_rate_tolerates_missing_parts():
    client = MagicMock()
    body = {'ticker': {'ticker': 'C:EURUSD', 'day': None, 'lastQuote': {}}}
    client.get_snapshot_ticker.return_value = SimpleNamespace(data=json.dumps(body).encode())
    rate = MassiveForex(client).rate('EUR', 'USD')
    assert math.isnan(rate['close']) and math.isnan(rate['last']) and math.isnan(rate['bid'])
    assert math.isnan(rate['ask']) and math.isnan(rate['previous_close']) and rate['as_of'] == ''


def test_rate_without_ticker_in_response_is_provider_error():
    client = MagicMock()
    client.get_snapshot_ticker.return_value = SimpleNamespace(data=b'{"status": "OK"}')
    with pytest.raises(ProviderError, match='C:EURUSD'):
        MassiveForex(client).rate('EUR', 'USD')


def test_rate_with_invalid_json_is_provider_error():
    client = MagicMock()
    client.get_snapshot_ticker.return_value = SimpleNamespace(data=b'<html>gateway timeout</html>')
    with pytest.raises(ProviderError, match='not valid JSON'):
        MassiveForex(client).rate('EUR', 'USD')


def test_rates_filters_to_base_and_sorts():
    client = MagicMock()
    client.get_snapshot_all.return_value = [_snap('C:USDJPY', -0.2), _snap('C:EURUSD', 0.9), _snap('C:USDCAD', 0.4)]
    frame = MassiveForex(client).rates('USD', None)
    client.get_snapshot_all.assert_called_once_with(market_type='forex', tickers=None)
    assert tuple(frame.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS
    assert frame['pair'].tolist() == ['USD/CAD', 'USD/JPY']
    assert frame.loc[0, 'last'] == 1.1 and frame.loc[0, 'open'] == 1.0 and set(frame['source']) == {'massive'}


def test_rates_with_symbols_requests_exact_tickers():
    client = MagicMock()
    client.get_snapshot_all.return_value = [_snap('C:USDEUR', 0.1), _snap('C:USDJPY', 0.2)]
    frame = MassiveForex(client).rates('USD', ['EUR', 'JPY'])
    client.get_snapshot_all.assert_called_once_with(market_type='forex', tickers=['C:USDEUR', 'C:USDJPY'])
    assert frame['pair'].tolist() == ['USD/JPY', 'USD/EUR']


def test_rates_empty_result_is_provider_error():
    client = MagicMock()
    client.get_snapshot_all.return_value = [_snap('C:EURUSD', 0.9)]
    with pytest.raises(ProviderError, match='no forex rates'):
        MassiveForex(client).rates('USD', None)


def test_rates_all_requested_pairs_missing_is_value_error():
    client = MagicMock()
    client.get_snapshot_all.return_value = []
    with pytest.raises(ValueError, match='USD/XYZ'):
        MassiveForex(client).rates('USD', ['XYZ'])


def test_rates_names_missing_requested_pairs():
    client = MagicMock()
    client.get_snapshot_all.return_value = [_snap('C:USDJPY', 0.2)]
    with pytest.raises(ValueError, match='USD/XYZ') as info:
        MassiveForex(client).rates('USD', ['JPY', 'XYZ'])
    assert 'USD/JPY' not in str(info.value)


def test_convert():
    client = MagicMock()
    client.get_real_time_currency_conversion.return_value = SimpleNamespace(
        from_='EUR', to='USD', initial_amount=100.0, converted=112.25,
        last=SimpleNamespace(bid=1.1224, ask=1.1226, exchange=48, timestamp=1759500000000))
    out = MassiveForex(client).convert('EUR', 'USD', 100.0)
    client.get_real_time_currency_conversion.assert_called_once_with('EUR', 'USD', amount=100.0)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['converted'] == 112.25 and out['bid'] == 1.1224 and out['ask'] == 1.1226
    assert out['as_of'] == '1759500000000' and out['source'] == 'massive'
    assert out['rate'] == 1.1225


def test_convert_without_converted_amount_leaves_rate_nan():
    client = MagicMock()
    client.get_real_time_currency_conversion.return_value = SimpleNamespace(converted=None, last=None)
    out = MassiveForex(client).convert('EUR', 'USD', 100.0)
    assert math.isnan(out['rate']) and math.isnan(out['converted'])


def test_convert_rate_is_rounded_to_ten_significant_digits():
    client = MagicMock()
    client.get_real_time_currency_conversion.return_value = SimpleNamespace(converted=1.0, last=None)
    assert MassiveForex(client).convert('EUR', 'USD', 3.0)['rate'] == 0.3333333333


def test_bad_response_maps_to_provider_error_keeping_message():
    client = MagicMock()
    client.get_snapshot_ticker.side_effect = BadResponse('{"status":"ERROR","error":"upstream down"}')
    with pytest.raises(ProviderError, match='upstream down') as info:
        MassiveForex(client).rate('EUR', 'USD')
    assert not isinstance(info.value, ProviderEntitlementError)


def test_retries_exhausted_on_429_is_rate_limited():
    client = MagicMock()
    client.get_snapshot_ticker.side_effect = MaxRetryError(
        None, '/v2/snapshot', ResponseError('too many 429 error responses'))
    with pytest.raises(ProviderRateLimited):
        MassiveForex(client).rate('EUR', 'USD')


def test_retries_exhausted_for_another_reason_is_not_rate_limited():
    client = MagicMock()
    client.get_snapshot_ticker.side_effect = MaxRetryError(
        None, '/v2/snapshot?id=429', ProtocolError('Connection aborted at 0x429'))
    with pytest.raises(ProviderError) as info:
        MassiveForex(client).rate('EUR', 'USD')
    assert not isinstance(info.value, ProviderRateLimited)


def test_transport_failure_is_provider_error():
    client = MagicMock()
    client.get_snapshot_all.side_effect = ProtocolError('Connection aborted.')
    with pytest.raises(ProviderError, match='Connection aborted') as info:
        MassiveForex(client).rates('USD', None)
    assert not isinstance(info.value, ProviderRateLimited)


def test_not_authorized_bad_response_is_entitlement_error():
    client = MagicMock()
    client.get_snapshot_all.side_effect = BadResponse('{"status":"NOT_AUTHORIZED","message":"upgrade your plan"}')
    with pytest.raises(ProviderEntitlementError, match='NOT_AUTHORIZED'):
        MassiveForex(client).rates('USD', None)


def test_auth_error_is_entitlement_error():
    client = MagicMock()
    client.get_real_time_currency_conversion.side_effect = AuthError('Must specify polygon api key')
    with pytest.raises(ProviderEntitlementError, match='api key'):
        MassiveForex(client).convert('EUR', 'USD', 1.0)


def test_massive_movers_supports_forex():
    client = MagicMock()
    client.get_snapshot_direction.return_value = [_snap('C:EURUSD', 0.9)]
    frame = MassiveMovers(client).movers('forex', 'gainers')
    client.get_snapshot_direction.assert_called_once_with(market_type='forex', direction='gainers')
    assert frame.loc[0, 'ticker'] == 'C:EURUSD'


def test_registry_builds_forex_adapters():
    from trader.data_providers.builtin import forex_quote_source_choices, source_choices
    from trader.data_providers.twelvedata.forex import TwelveDataForex
    registry = ProviderRegistry.from_config({'massive_api_key': 'm', 'twelvedata_api_key': 't'})
    assert isinstance(registry.get(Capability.FOREX, 'massive'), MassiveForex)
    assert isinstance(registry.get(Capability.FOREX, 'twelvedata'), TwelveDataForex)
    assert isinstance(registry.get(Capability.MOVERS_FOREX, 'massive'), MassiveMovers)
    choices = forex_quote_source_choices()
    assert choices[0] == 'ib' and {'massive', 'twelvedata'} <= set(choices)
    assert 'massive' in source_choices(Capability.MOVERS_FOREX)


def test_twelvedata_snapshot_all_names_the_command_and_supported_sources():
    from trader.data_providers.errors import CapabilityNotSupported
    from trader.data_providers.twelvedata.forex import TwelveDataForex
    with pytest.raises(CapabilityNotSupported, match=r"does not support forex rates \(snapshot-all\)"):
        TwelveDataForex(MagicMock()).rates('USD', None)
