"""One row contract, two adapters: Massive and Alpaca rows must be interchangeable."""

import datetime as dt
import math
import re
from types import SimpleNamespace as NS

import pytest

from trader.data_providers.alpaca.options import AlpacaOptions
from trader.data_providers.capabilities import OPTION_FIELDS
from trader.data_providers.massive.options import MassiveOptions
from trader.data_providers.option_symbols import parse_option_symbol

OCC = 'AAPL261120C00250000'
BARE_OCC = re.compile(r'^[A-Z]{1,6}\d{6}[CP]\d{8}$')
NUMBER_FIELDS = tuple(name for name in OPTION_FIELDS if name not in
                      ('ticker', 'type', 'expiration', 'underlying', 'quote_time', 'last_time', 'provider', 'feed'))
UNSENT_FIELDS = ('bid', 'ask', 'mid', 'last', 'volume', 'open_interest', 'iv',
                 'delta', 'gamma', 'theta', 'vega', 'break_even')


class FakeMassiveClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def list_snapshot_options_chain(self, underlying_asset, params):
        return iter([self.snapshot])

    def get_snapshot_option(self, underlying_asset, option_contract):
        return self.snapshot


class FakeAlpacaClient:
    def __init__(self, routes):
        self.routes = routes

    def get_json(self, path, params):
        return self.routes[path]

    def paginate(self, path, params):
        yield self.routes[path]


def _massive_snapshot(quoted):
    sent = dict(last_quote=NS(bid=2.0, ask=2.5), last_trade=NS(price=2.25), day=NS(volume=500.0),
                greeks=NS(delta=0.5, gamma=0.02, theta=-0.05, vega=0.15), open_interest=1000.0,
                implied_volatility=0.3, break_even_price=252.0, underlying_asset=NS(ticker='AAPL', price=150.0))
    unsent = dict(last_quote=None, last_trade=None, day=None, greeks=None, open_interest=None,
                  implied_volatility=None, break_even_price=None, underlying_asset=None)
    return NS(details=NS(ticker=f'O:{OCC}', contract_type='call', strike_price=250.0,
                         expiration_date='2026-11-20'), **(sent if quoted else unsent))


def _massive(quoted):
    adapter = MassiveOptions(FakeMassiveClient(_massive_snapshot(quoted)))
    return adapter, 'massive', 'opra'


def _alpaca(quoted):
    snapshot = {'latestQuote': {'bp': 2.0, 'ap': 2.5, 't': '2026-10-02T19:59:59Z'},
                'latestTrade': {'p': 2.25, 't': '2026-10-02T15:39:26Z'},
                'dailyBar': {'v': 500, 't': '2026-10-02T04:00:00Z'},
                'greeks': {'delta': 0.5, 'gamma': 0.02, 'theta': -0.05, 'vega': 0.15},
                'impliedVolatility': 0.3} if quoted else {}
    details = {'symbol': OCC, 'underlying_symbol': 'AAPL', 'open_interest': '1000' if quoted else None}
    data = FakeAlpacaClient({
        '/v1beta1/options/snapshots/AAPL': {'snapshots': {OCC: snapshot} if snapshot else {}, 'next_page_token': None},
        '/v1beta1/options/snapshots': {'snapshots': {OCC: snapshot} if snapshot else {}, 'next_page_token': None},
        '/v2/stocks/snapshots': {'AAPL': {'latestTrade': {'p': 150.0}}},
    })
    trading = FakeAlpacaClient({
        '/v2/options/contracts': {'option_contracts': [dict(details, type='call', strike_price='250',
                                                            expiration_date='2026-11-20')],
                                  'next_page_token': None},
        f'/v2/options/contracts/{OCC}': details,
    })
    return AlpacaOptions(data, trading, today=lambda: dt.date(2026, 10, 4)), 'alpaca', 'indicative'


def _chain_row(adapter):
    (row,) = adapter.chain('AAPL', '2026-11-20')
    return row


def _contract_row(adapter):
    return adapter.contract(parse_option_symbol(OCC))


ADAPTERS = pytest.mark.parametrize('build', [_massive, _alpaca], ids=['massive', 'alpaca'])
ROW_SOURCES = pytest.mark.parametrize('row_of', [_chain_row, _contract_row], ids=['chain', 'contract'])


@ADAPTERS
@ROW_SOURCES
@pytest.mark.parametrize('quoted', [True, False], ids=['quoted', 'unquoted'])
def test_row_shape_is_identical_across_adapters(build, row_of, quoted):
    adapter, provider, feed = build(quoted)
    row = row_of(adapter)
    assert tuple(row) == OPTION_FIELDS
    assert row['ticker'] == OCC and BARE_OCC.match(row['ticker'])
    assert (row['type'], row['strike'], row['expiration']) == ('call', 250.0, '2026-11-20')
    assert (row['provider'], row['feed']) == (provider, feed)
    assert all(isinstance(row[name], float) for name in NUMBER_FIELDS)


@ADAPTERS
@ROW_SOURCES
def test_numbers_a_provider_did_not_send_are_nan_not_zero(build, row_of):
    adapter, _, _ = build(False)
    row = row_of(adapter)
    for column in UNSENT_FIELDS:
        assert math.isnan(row[column]), column
    assert row['quote_time'] == '' and row['last_time'] == ''


@ADAPTERS
@ROW_SOURCES
def test_a_quoted_contract_carries_its_numbers(build, row_of):
    adapter, _, _ = build(True)
    row = row_of(adapter)
    assert (row['bid'], row['ask'], row['mid']) == (2.0, 2.5, 2.25)
    assert row['iv'] == pytest.approx(30.0)
    assert (row['delta'], row['gamma'], row['theta'], row['vega']) == (0.5, 0.02, -0.05, 0.15)
    assert (row['volume'], row['open_interest']) == (500.0, 1000.0)
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 150.0
