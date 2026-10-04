from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader.data_providers.capabilities import Capability, make_fx_conversion, make_fx_rate
from trader.sdk import MMR


def _mmr(provider=None):
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock(return_value=provider or MagicMock())
    return mmr


def test_forex_snapshot_rest_source_uses_forex_capability_not_stock_quotes():
    provider = MagicMock()
    provider.rate.return_value = make_fx_rate('EUR', 'USD', last=1.1)
    mmr = _mmr(provider)
    out = mmr.forex_snapshot('eur/usd', source='massive')
    mmr._provider.assert_called_once_with(Capability.FOREX, 'massive')
    provider.rate.assert_called_once_with('EUR', 'USD')
    assert out['pair'] == 'EUR/USD' and out['last'] == 1.1


def test_forex_quote_rest_source():
    provider = MagicMock()
    provider.rate.return_value = make_fx_rate('EUR', 'JPY', last=176.99)
    mmr = _mmr(provider)
    assert mmr.forex_quote('eur', 'jpy', source='twelvedata')['last'] == 176.99
    mmr._provider.assert_called_once_with(Capability.FOREX, 'twelvedata')
    provider.rate.assert_called_once_with('EUR', 'JPY')


def _ib_mmr():
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock()
    mmr._resolve_contract = MagicMock(return_value=SimpleNamespace(conId=12087792))
    # _typed_query is a read-only property over these two clients (built lazily when both are None).
    mmr._typed_query_client = MagicMock()
    mmr._typed_command_client = MagicMock()
    mmr._typed_query_client.call.return_value = {'snapshot': {'bid': 1.1, 'ask': 1.2, 'last': 1.15}}
    return mmr


def test_forex_snapshot_ib_resolves_exact_idealpro_cash_contract():
    mmr = _ib_mmr()
    out = mmr.forex_snapshot('C:EURUSD')
    mmr._resolve_contract.assert_called_once_with('EUR', sec_type='CASH', exchange='IDEALPRO', currency='USD')
    mmr._typed_query_client.call.assert_called_once_with(
        'get_snapshot', {'instrument_id': 12087792, 'delayed': False}, dict)
    mmr._provider.assert_not_called()
    assert out['pair'] == 'EUR/USD' and out['bid'] == 1.1 and out['ask'] == 1.2


def test_forex_quote_ib_resolves_exact_contract():
    mmr = _ib_mmr()
    out = mmr.forex_quote('eur', 'usd')
    mmr._resolve_contract.assert_called_once_with('EUR', sec_type='CASH', exchange='IDEALPRO', currency='USD')
    assert out == {'pair': 'EUR/USD', 'bid': 1.1, 'ask': 1.2, 'last': 1.15, 'time': None}


@pytest.mark.parametrize('pair', ['EUR', 'EURUSDX', 'USDUSD'])
def test_bad_pair_raises_before_any_call(pair):
    mmr = _ib_mmr()
    with pytest.raises(ValueError):
        mmr.forex_snapshot(pair, source='massive')
    with pytest.raises(ValueError):
        mmr.forex_snapshot(pair)
    with pytest.raises(ValueError):
        mmr.forex_quote(pair[:3], pair[3:] or 'EU')
    mmr._provider.assert_not_called()
    mmr._resolve_contract.assert_not_called()


def test_forex_convert_default_source_and_validation():
    provider = MagicMock()
    provider.convert.return_value = make_fx_conversion('EUR', 'USD', 100, converted=112.25)
    mmr = _mmr(provider)
    assert mmr.forex_convert('eur', 'usd', 100)['converted'] == 112.25
    mmr._provider.assert_called_once_with(Capability.FOREX, None)
    provider.convert.assert_called_once_with('EUR', 'USD', 100.0)
    for bad in (0, -5, float('nan'), float('inf')):
        with pytest.raises(ValueError, match='positive'):
            mmr.forex_convert('EUR', 'USD', bad)
    with pytest.raises(ValueError, match='3-letter'):
        mmr.forex_convert('EUR', 'EURO', 10)


def test_forex_snapshot_all_passes_base_and_symbols():
    provider = MagicMock()
    provider.rates.return_value = pd.DataFrame()
    mmr = _mmr(provider)
    mmr.forex_snapshot_all(base='usd', symbols=['eur', 'jpy'], source='massive')
    mmr._provider.assert_called_once_with(Capability.FOREX, 'massive')
    provider.rates.assert_called_once_with('USD', ['EUR', 'JPY'])
    mmr.forex_snapshot_all()
    provider.rates.assert_called_with('USD', None)
    with pytest.raises(ValueError, match='same currency'):
        mmr.forex_snapshot_all(base='USD', symbols=['USD'])


def test_forex_snapshot_all_drops_duplicate_symbols_keeping_order():
    provider = MagicMock()
    provider.rates.return_value = pd.DataFrame()
    _mmr(provider).forex_snapshot_all(base='USD', symbols=['jpy', 'EUR', 'JPY', 'eur'], source='frankfurter')
    provider.rates.assert_called_once_with('USD', ['JPY', 'EUR'])


def test_forex_snapshot_all_bad_base_raises_before_provider_call():
    mmr = _mmr()
    with pytest.raises(ValueError, match='3-letter'):
        mmr.forex_snapshot_all(base='DOLLAR')
    mmr._provider.assert_not_called()


def test_forex_movers_routes_through_movers_forex():
    provider = MagicMock()
    provider.movers.return_value = pd.DataFrame({'ticker': ['EURUSD']})
    mmr = _mmr(provider)
    mmr.forex_movers('losers', source='massive')
    mmr._provider.assert_called_once_with(Capability.MOVERS_FOREX, 'massive')
    provider.movers.assert_called_once_with('forex', 'losers')
