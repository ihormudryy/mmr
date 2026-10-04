import math

import pandas as pd
import pytest

from trader.data_providers.capabilities import (
    FX_CONVERSION_FIELDS, FX_RATE_FIELDS, FX_RATES_COLUMNS, Capability, ForexProvider,
    make_fx_conversion, make_fx_rate, movers_capability, sort_fx_rates,
)
from trader.data_providers.symbols import parse_currency, parse_forex_codes, parse_forex_pair


def test_new_capability_values():
    assert [c.value for c in (Capability.FOREX, Capability.MOVERS_FOREX, Capability.MOVERS_INDICES)] == \
        ['forex', 'movers_forex', 'movers_indices']


def test_make_fx_rate_fills_every_field():
    rate = make_fx_rate('eur', 'usd', last=1.1225, source='frankfurter')
    assert tuple(rate) == FX_RATE_FIELDS
    assert rate['pair'] == 'EUR/USD' and rate['base'] == 'EUR' and rate['quote'] == 'USD'
    assert rate['last'] == 1.1225 and math.isnan(rate['bid']) and math.isnan(rate['change_pct'])
    assert rate['as_of'] == '' and rate['note'] == '' and rate['source'] == 'frankfurter'


def test_make_fx_rate_rejects_unknown_and_identity_fields():
    with pytest.raises(TypeError, match='unknown fx rate field'):
        make_fx_rate('EUR', 'USD', rate=1.0)
    with pytest.raises(TypeError, match='unknown fx rate field'):
        make_fx_rate('EUR', 'USD', pair='GBP/USD')


def test_make_fx_conversion():
    out = make_fx_conversion('eur', 'usd', 1000, converted=1122.5, rate=1.1225)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['from'] == 'EUR' and out['to'] == 'USD' and out['amount'] == 1000.0
    assert out['converted'] == 1122.5 and math.isnan(out['bid']) and out['source'] == ''
    with pytest.raises(TypeError, match='unknown fx conversion field'):
        make_fx_conversion('EUR', 'USD', 1, timestamp=1)


def test_sort_fx_rates_orders_columns_and_rows():
    frame = pd.DataFrame([{'pair': 'USD/JPY', 'change_pct': -0.2, 'open': 1.0},
                          {'pair': 'USD/CAD', 'change_pct': 0.5, 'open': 2.0}])
    out = sort_fx_rates(frame)
    assert tuple(out.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS
    assert list(out.columns[len(FX_RATES_COLUMNS):]) == ['open']
    assert out['pair'].tolist() == ['USD/CAD', 'USD/JPY']
    assert out.loc[0, 'source'] == '' and math.isnan(out.loc[0, 'last'])


def test_movers_capability_per_market():
    assert movers_capability('stocks') is Capability.MOVERS
    assert movers_capability('crypto') is Capability.MOVERS
    assert movers_capability('options') is Capability.MOVERS
    assert movers_capability('indices') is Capability.MOVERS_INDICES
    assert movers_capability('forex') is Capability.MOVERS_FOREX


def test_forex_protocol_is_structural():
    class Fx:
        def rate(self, base, quote):
            return {}

        def rates(self, base, symbols):
            return pd.DataFrame()

        def convert(self, base, quote, amount):
            return {}

    assert isinstance(Fx(), ForexProvider)


@pytest.mark.parametrize('text, expected', [
    ('EURUSD', ('EUR', 'USD')), ('eur/usd', ('EUR', 'USD')), ('C:GBPJPY', ('GBP', 'JPY')), (' usdcad ', ('USD', 'CAD')),
])
def test_parse_forex_pair_accepts_exact_spellings(text, expected):
    assert parse_forex_pair(text) == expected


@pytest.mark.parametrize('text', ['EUR', 'EURUSDX', 'EUR//USD', 'EU/RUSD', 'EUR1SD', 'USDUSD', '€URUSD', '', 'X:EURUSD',
                                  'EURUß', 'ßAUSD', 'ﬁUUSD', 'EURUSı', 'C:EUR/USD'])
def test_parse_forex_pair_is_strict(text):
    with pytest.raises(ValueError):
        parse_forex_pair(text)


def test_parse_currency_and_codes():
    assert parse_currency(' eur ') == 'EUR'
    for bad in ('EURO', 'E1R', '', 'EU', 'ßA'):
        with pytest.raises(ValueError, match='3-letter currency code'):
            parse_currency(bad)
    assert parse_forex_codes('eur', 'jpy') == ('EUR', 'JPY')
    with pytest.raises(ValueError, match='same currency'):
        parse_forex_codes('USD', 'usd')
