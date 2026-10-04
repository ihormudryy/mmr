import datetime as dt
import math

import pytest

from trader.data_providers.capabilities import (
    OPTION_FIELDS, Capability, OptionsProvider, make_option_row, option_mid, sort_option_rows,
)
from trader.data_providers.option_symbols import OptionSymbol
from trader.data_providers.registry import ProviderRegistry

NAN = float('nan')
CALL = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'C', 250_000)
PUT = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'P', 220_000)


def test_capability_value():
    assert Capability.OPTIONS.value == 'options'


def test_option_fields_keep_todays_chain_columns_first():
    assert OPTION_FIELDS[:17] == (
        'ticker', 'type', 'strike', 'expiration', 'bid', 'ask', 'mid', 'last', 'volume', 'open_interest',
        'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even', 'underlying_price')
    assert OPTION_FIELDS[17:] == ('underlying', 'quote_time', 'last_time', 'provider', 'feed')


def test_make_option_row_takes_identity_from_the_symbol():
    row = make_option_row(CALL, bid=82.65, ask=87.45, provider='alpaca', feed='indicative')
    assert tuple(row) == OPTION_FIELDS
    assert (row['ticker'], row['type'], row['strike'], row['expiration']) == (
        'AAPL261120C00250000', 'call', 250.0, '2026-11-20')
    assert row['mid'] == pytest.approx(85.05)
    assert math.isnan(row['iv']) and math.isnan(row['delta']) and math.isnan(row['break_even'])
    assert math.isnan(row['volume']) and math.isnan(row['open_interest'])
    assert row['quote_time'] == '' and row['underlying'] == '' and row['feed'] == 'indicative'


def test_make_option_row_rejects_unknown_and_derived_fields():
    with pytest.raises(TypeError, match='unknown option field'):
        make_option_row(CALL, greek=1.0)
    for derived in ('ticker', 'type', 'strike', 'expiration', 'mid'):
        with pytest.raises(TypeError, match='come from the option symbol'):
            make_option_row(CALL, **{derived: 1.0})


@pytest.mark.parametrize('bid, ask, expected', [
    (0.06, 0.17, 0.115),
    (0.0, 0.05, 0.025),
    (NAN, 1.0, NAN),
    (1.0, NAN, NAN),
    (1.2, 1.0, NAN),     # crossed
    (0.0, 0.0, NAN),
    (-0.1, 1.0, NAN),
])
def test_option_mid(bid, ask, expected):
    mid = option_mid(bid, ask)
    assert (math.isnan(mid) and math.isnan(expected)) or mid == pytest.approx(expected)


def test_make_option_row_turns_none_into_nan_before_the_mid():
    row = make_option_row(CALL, bid=None, ask=1.0, iv=None, provider=None, quote_time=None)
    assert math.isnan(row['bid']) and math.isnan(row['mid']) and math.isnan(row['iv'])
    assert row['provider'] == '' and row['quote_time'] == ''


def test_make_option_row_rejects_non_numeric_strings():
    with pytest.raises(ValueError, match='bid'):
        make_option_row(CALL, bid='1', ask=2.0)
    with pytest.raises(ValueError, match='iv'):
        make_option_row(CALL, iv='n/a')


def test_sort_option_rows_calls_first_then_strike():
    low_call = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'C', 100_000)
    rows = [make_option_row(PUT), make_option_row(CALL), make_option_row(low_call)]
    assert [r['ticker'] for r in sort_option_rows(rows)] == [
        'AAPL261120C00100000', 'AAPL261120C00250000', 'AAPL261120P00220000']


def test_protocol_is_structural():
    class Options:
        def expirations(self, underlying):
            return []

        def chain(self, underlying, expiration, contract_type=None, strike_min=None, strike_max=None):
            return []

        def contract(self, option):
            return {}

    assert isinstance(Options(), OptionsProvider)


def test_options_never_inherit_default_data_source():
    for global_default in ('massive', 'twelvedata', 'alpaca'):
        registry = ProviderRegistry.from_config({'default_data_source': global_default})
        assert registry.default_source(Capability.OPTIONS) == 'alpaca'


def test_data_providers_override_moves_options():
    registry = ProviderRegistry.from_config({'data_providers': {'options': 'massive'}})
    assert registry.default_source(Capability.OPTIONS) == 'massive'
