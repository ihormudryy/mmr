import datetime as dt
import json
import math
from pathlib import Path

import pytest

from trader.data_providers.alpaca.options import AlpacaOptions
from trader.data_providers.capabilities import OPTION_FIELDS, Capability, OptionsProvider
from trader.data_providers.errors import ProviderError
from trader.data_providers.option_symbols import parse_option_symbol
from trader.data_providers.registry import ProviderRegistry

FIXTURES = Path(__file__).parent / 'fixtures'
SNAPSHOTS = json.loads((FIXTURES / 'alpaca_option_snapshots_aapl.json').read_text())
CONTRACTS = json.loads((FIXTURES / 'alpaca_option_contracts_aapl.json').read_text())
STOCK = json.loads((FIXTURES / 'alpaca_snapshots_aapl.json').read_text())   # phase 3a: AAPL last 333.75
CHAIN_PATH = '/v1beta1/options/snapshots/AAPL'
TODAY = dt.date(2026, 10, 4)


class FakeClient:
    """Routes by path: a list of pages for paginate(), a dict for get_json(), or an exception to raise."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _route(self, path, params):
        self.calls.append((path, dict(params)))
        response = self.routes[path]
        if isinstance(response, Exception):
            raise response
        return response

    def get_json(self, path, params):
        return self._route(path, params)

    def paginate(self, path, params):
        yield from self._route(path, params)


def _options(snapshot_pages=None, contracts=None, stock=None):
    data = FakeClient({CHAIN_PATH: snapshot_pages or [SNAPSHOTS],
                       '/v2/stocks/snapshots': STOCK if stock is None else stock})
    trading = FakeClient({'/v2/options/contracts': contracts or [CONTRACTS]})
    return AlpacaOptions(data, trading, today=lambda: TODAY), data, trading


def _rows(**kwargs):
    options, _, _ = _options(**kwargs)
    return {row['ticker']: row for row in options.chain('AAPL', '2026-11-20')}


def _contract(symbol, strike, kind, open_interest):
    return {'symbol': symbol, 'expiration_date': '2026-11-20', 'root_symbol': 'AAPL', 'underlying_symbol': 'AAPL',
            'type': kind, 'strike_price': strike, 'open_interest': open_interest,
            'open_interest_date': '2026-10-01' if open_interest else None, 'close_price': None}


def test_expirations_from_contract_list_across_pages():
    page1 = {'option_contracts': [{'symbol': 'AAPL261120C00250000', 'expiration_date': '2026-11-20'},
                                  {'symbol': 'AAPL261016C00250000', 'expiration_date': '2026-10-16'}],
             'next_page_token': 'Mw=='}
    page2 = {'option_contracts': [{'symbol': 'AAPL261120P00250000', 'expiration_date': '2026-11-20'},
                                  {'symbol': 'AAPL270115C00250000', 'expiration_date': '2027-01-15'}],
             'next_page_token': None}
    options, data, trading = _options(contracts=[page1, page2])
    assert options.expirations('aapl') == ['2026-10-16', '2026-11-20', '2027-01-15']
    assert trading.calls == [('/v2/options/contracts', {
        'underlying_symbols': 'AAPL', 'status': 'active', 'expiration_date_gte': '2026-10-04', 'limit': 5000})]
    assert data.calls == []


def test_expirations_map_class_shares():
    options, _, trading = _options(contracts=[{'option_contracts': [], 'next_page_token': None}])
    assert options.expirations('BRK B') == []
    assert trading.calls[0][1]['underlying_symbols'] == 'BRK.B'


def test_invalid_underlying_raises_before_any_request():
    options, data, trading = _options()
    with pytest.raises(ValueError, match='not a valid Alpaca stock symbol'):
        options.expirations('AAPL;DROP')
    assert trading.calls == [] and data.calls == []


def test_chain_maps_indicative_snapshots():
    row = _rows()['AAPL261120C00250000']
    assert tuple(row) == OPTION_FIELDS
    assert (row['type'], row['strike'], row['expiration']) == ('call', 250.0, '2026-11-20')
    assert (row['bid'], row['ask'], row['last']) == (82.65, 87.45, 83.13)
    assert row['mid'] == pytest.approx(85.05)
    assert row['volume'] == 9.0 and row['open_interest'] == 464.0
    assert row['iv'] == pytest.approx(37.38)
    assert (row['delta'], row['gamma'], row['theta'], row['vega']) == (0.9879, 0.0007, -0.0411, 0.0375)
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 333.75
    assert row['quote_time'] == '2026-10-02T19:59:59.374582282Z'
    assert row['last_time'] == '2026-10-02T15:39:26.489876952Z'
    assert math.isnan(row['break_even'])


def test_missing_greeks_iv_trade_and_bar_are_nan():
    rows = _rows()
    no_greeks = rows['AAPL261120C00100000']
    assert math.isnan(no_greeks['iv']) and math.isnan(no_greeks['delta']) and math.isnan(no_greeks['vega'])
    assert no_greeks['bid'] == 230.21 and no_greeks['open_interest'] == 6.0
    greeks_only = rows['AAPL261120P00395000']
    assert greeks_only['iv'] == pytest.approx(33.31) and greeks_only['delta'] == -0.9052
    assert math.isnan(greeks_only['last']) and math.isnan(greeks_only['volume'])
    assert math.isnan(greeks_only['open_interest']) and greeks_only['last_time'] == ''
    quote_only = rows['AAPL261120P00580000']
    assert quote_only['bid'] == 248.36 and quote_only['ask'] == 251.46
    for column in ('last', 'volume', 'open_interest', 'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even'):
        assert math.isnan(quote_only[column]), column


def test_session_volume_rule():
    rows = _rows()
    assert rows['AAPL261120C00250000']['volume'] == 9.0    # daily bar from the quote's session (2026-10-02)
    assert rows['AAPL261120P00220000']['volume'] == 0.0    # last daily bar 2026-09-30: no trades since
    assert math.isnan(rows['AAPL261120P00395000']['volume'])  # no daily bar at all


def test_every_row_is_labelled_indicative():
    rows = _rows().values()
    assert len(rows) == 5
    assert all(row['provider'] == 'alpaca' and row['feed'] == 'indicative' for row in rows)


def test_rows_sorted_calls_then_strike():
    options, _, _ = _options()
    assert [r['ticker'] for r in options.chain('AAPL', '2026-11-20')] == [
        'AAPL261120C00100000', 'AAPL261120C00250000',
        'AAPL261120P00220000', 'AAPL261120P00395000', 'AAPL261120P00580000']


def test_listed_contract_without_snapshot_is_a_nan_row_keeping_open_interest():
    listed_only = _contract('AAPL261120C00300000', '300', 'call', '812')
    contracts = {**CONTRACTS, 'option_contracts': CONTRACTS['option_contracts'] + [listed_only]}
    rows = _rows(contracts=[contracts])
    assert len(rows) == 6
    row = rows['AAPL261120C00300000']
    assert tuple(row) == OPTION_FIELDS
    assert (row['type'], row['strike'], row['expiration']) == ('call', 300.0, '2026-11-20')
    assert row['open_interest'] == 812.0
    for column in ('bid', 'ask', 'mid', 'last', 'volume', 'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even'):
        assert math.isnan(row[column]), column
    assert row['quote_time'] == '' and row['last_time'] == ''
    assert row['provider'] == 'alpaca' and row['feed'] == 'indicative'
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 333.75


def test_listed_contract_without_snapshot_is_sorted_with_the_rest():
    listed_only = _contract('AAPL261120C00175000', '175', 'call', None)
    contracts = {**CONTRACTS, 'option_contracts': CONTRACTS['option_contracts'] + [listed_only]}
    options, _, _ = _options(contracts=[contracts])
    assert [r['strike'] for r in options.chain('AAPL', '2026-11-20')][:3] == [100.0, 175.0, 250.0]


def test_snapshot_without_listed_contract_has_nan_open_interest():
    contracts = {**CONTRACTS, 'option_contracts': CONTRACTS['option_contracts'][1:]}
    rows = _rows(contracts=[contracts])
    assert len(rows) == 5
    assert math.isnan(rows['AAPL261120C00100000']['open_interest'])
    assert rows['AAPL261120C00250000']['open_interest'] == 464.0


def test_contract_symbol_that_cannot_be_parsed_raises():
    contracts = {'option_contracts': [_contract('GARBAGE', '1', 'call', None)], 'next_page_token': None}
    options, _, _ = _options(contracts=[contracts])
    with pytest.raises(ProviderError, match="alpaca returned an option symbol MMR cannot parse: 'GARBAGE'"):
        options.chain('AAPL', '2026-11-20')


def test_chain_sends_filters_to_both_endpoints():
    options, data, trading = _options()
    options.chain('AAPL', '2026-11-20', contract_type='call', strike_min=240, strike_max=260.5)
    filters = {'expiration_date': '2026-11-20', 'type': 'call',
               'strike_price_gte': '240.0', 'strike_price_lte': '260.5'}
    assert trading.calls == [('/v2/options/contracts', {**filters, 'underlying_symbols': 'AAPL', 'limit': 5000})]
    assert data.calls == [
        ('/v2/stocks/snapshots', {'symbols': 'AAPL', 'feed': 'iex'}),
        (CHAIN_PATH, {**filters, 'feed': 'indicative', 'limit': 1000}),
    ]


def test_contract_type_is_case_insensitive_and_validated():
    options, _, trading = _options()
    options.chain('AAPL', '2026-11-20', contract_type=' PUT ')
    assert trading.calls[0][1]['type'] == 'put'
    for bad in ('straddle', '', 'ſ'):
        with pytest.raises(ValueError, match="contract_type must be 'call' or 'put'"):
            options.chain('AAPL', '2026-11-20', contract_type=bad)
    assert len(trading.calls) == 1


@pytest.mark.parametrize('expiration', ['foobar', '2026-3-20', '2026-02-30', ''])
def test_chain_rejects_malformed_expiration_before_any_request(expiration):
    options, data, trading = _options()
    with pytest.raises(ValueError, match='expiration must be'):
        options.chain('AAPL', expiration)
    assert trading.calls == [] and data.calls == []


@pytest.mark.parametrize('bound', [float('nan'), float('inf'), 'abc'])
def test_chain_rejects_non_finite_strike_bounds(bound):
    options, data, trading = _options()
    with pytest.raises(ValueError, match='strike_min'):
        options.chain('AAPL', '2026-11-20', strike_min=bound)
    assert trading.calls == [] and data.calls == []


def test_unknown_underlying_fails_before_snapshots():
    refused = ProviderError('alpaca /v2/options/contracts failed: HTTP 422 invalid underlying symbols: ZZZZQ')
    data = FakeClient({})
    trading = FakeClient({'/v2/options/contracts': refused})
    with pytest.raises(ProviderError, match='invalid underlying symbols: ZZZZQ'):
        AlpacaOptions(data, trading, today=lambda: TODAY).chain('ZZZZQ', '2026-11-20')
    assert data.calls == []


def test_underlying_price_nan_when_iex_has_no_trade():
    rows = _rows(stock={})
    assert len(rows) == 5 and all(math.isnan(row['underlying_price']) for row in rows.values())


def test_unexpected_contract_key_raises():
    page = {'snapshots': {'GARBAGE': {'latestQuote': {}}}, 'next_page_token': None}
    options, _, _ = _options(snapshot_pages=[page])
    with pytest.raises(ProviderError, match="alpaca returned an option symbol MMR cannot parse: 'GARBAGE'"):
        options.chain('AAPL', '2026-11-20')


def test_registry_builds_alpaca_options_with_paper_trading_client():
    from trader.data_providers.alpaca.assets import ALPACA_PAPER_TRADING_URL
    from trader.data_providers.alpaca.client import ALPACA_DATA_URL
    from trader.data_providers.builtin import source_choices
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    provider = registry.get(Capability.OPTIONS)
    assert isinstance(provider, AlpacaOptions)
    assert provider._data._base_url == ALPACA_DATA_URL
    assert provider._trading._base_url == ALPACA_PAPER_TRADING_URL
    assert source_choices(Capability.OPTIONS) == ['alpaca', 'massive']


CONTRACT_DETAILS = {   # trimmed real response, 2026-10-04
    'symbol': 'AAPL261120C00250000', 'expiration_date': '2026-11-20', 'root_symbol': 'AAPL',
    'underlying_symbol': 'AAPL', 'type': 'call', 'strike_price': '250', 'open_interest': '464',
    'open_interest_date': '2026-10-01', 'close_price': '83.7',
}
CONTRACT_PATH = '/v2/options/contracts/AAPL261120C00250000'


def _contract_options(details=CONTRACT_DETAILS, snapshots=None, contract_path=CONTRACT_PATH, stock=None):
    if snapshots is None:
        snapshots = {'AAPL261120C00250000': SNAPSHOTS['snapshots']['AAPL261120C00250000']}
    data = FakeClient({'/v1beta1/options/snapshots': {'snapshots': snapshots, 'next_page_token': None},
                       '/v2/stocks/snapshots': STOCK if stock is None else stock})
    trading = FakeClient({contract_path: details})
    return AlpacaOptions(data, trading, today=lambda: TODAY), data, trading


def test_contract_joins_details_snapshot_and_underlying():
    options, data, trading = _contract_options()
    row = options.contract(parse_option_symbol('O:AAPL261120C00250000'))
    assert trading.calls == [(CONTRACT_PATH, {})]
    assert ('/v1beta1/options/snapshots', {'symbols': 'AAPL261120C00250000', 'feed': 'indicative'}) in data.calls
    assert row['ticker'] == 'AAPL261120C00250000' and row['open_interest'] == 464.0
    assert (row['bid'], row['ask'], row['volume']) == (82.65, 87.45, 9.0)
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 333.75
    assert row['provider'] == 'alpaca' and row['feed'] == 'indicative'


def test_contract_uses_underlying_symbol_from_details():
    details = dict(CONTRACT_DETAILS, symbol='BRKB261009C00270000', root_symbol='BRKB',
                   underlying_symbol='BRK.B', open_interest=None)
    options, data, _ = _contract_options(details=details, snapshots={},
                                         contract_path='/v2/options/contracts/BRKB261009C00270000',
                                         stock={'BRK.B': STOCK['AAPL']})
    row = options.contract(parse_option_symbol('BRKB261009C00270000'))
    assert ('/v2/stocks/snapshots', {'symbols': 'BRK.B', 'feed': 'iex'}) in data.calls
    assert row['underlying'] == 'BRK.B' and math.isnan(row['open_interest'])


def test_unknown_contract_is_loud():
    missing = ProviderError('alpaca /v2/options/contracts/AAPL261120C00251234 failed: HTTP 404 '
                            'option contract AAPL261120C00251234 not found')
    options, data, _ = _contract_options(details=missing, contract_path='/v2/options/contracts/AAPL261120C00251234')
    with pytest.raises(ProviderError, match='not found'):
        options.contract(parse_option_symbol('AAPL261120C00251234'))
    assert data.calls == []


def test_contract_without_snapshot_is_a_nan_row():
    options, _, _ = _contract_options(snapshots={})
    row = options.contract(parse_option_symbol('AAPL261120C00250000'))
    assert math.isnan(row['bid']) and math.isnan(row['mid']) and math.isnan(row['iv'])
    assert row['quote_time'] == '' and row['open_interest'] == 464.0 and row['feed'] == 'indicative'


def test_details_without_underlying_raise():
    details = {key: value for key, value in CONTRACT_DETAILS.items() if key != 'underlying_symbol'}
    options, _, _ = _contract_options(details=details)
    with pytest.raises(ProviderError, match='no underlying_symbol'):
        options.contract(parse_option_symbol('AAPL261120C00250000'))


def test_alpaca_options_satisfies_protocol():
    options, _, _ = _contract_options()
    assert isinstance(options, OptionsProvider)


def test_chain_filters_the_union_client_side():
    # the contracts API honoured the filters; the snapshot endpoint returned out-of-range extras
    snapshot = SNAPSHOTS['snapshots']['AAPL261120C00250000']
    extras = {'AAPL261120P00250000': snapshot,       # wrong type
              'AAPL261120C00300000': snapshot,       # above strike_max
              'AAPL261120C00100000': snapshot,       # below strike_min
              'AAPL261218C00250000': snapshot}       # other expiration
    page = {'snapshots': {'AAPL261120C00250000': snapshot, **extras}, 'next_page_token': None}
    contracts = {'option_contracts': [_contract('AAPL261120C00250000', '250', 'call', '464')], 'next_page_token': None}
    options, _, _ = _options(snapshot_pages=[page], contracts=[contracts])
    rows = options.chain('AAPL', '2026-11-20', contract_type='call', strike_min=200, strike_max=260)
    assert [row['ticker'] for row in rows] == ['AAPL261120C00250000']


def test_chain_filters_contracts_listed_outside_the_range_too():
    contracts = {'option_contracts': [_contract('AAPL261120C00250000', '250', 'call', '464'),
                                      _contract('AAPL261120C00400000', '400', 'call', '9')], 'next_page_token': None}
    options, _, _ = _options(contracts=[contracts])
    rows = options.chain('AAPL', '2026-11-20', strike_max=300)
    assert 'AAPL261120C00400000' not in [row['ticker'] for row in rows]


def _volume(daily_bar, quote_time='2026-10-02T19:59:59Z'):
    snapshot = {'latestQuote': {'bp': 1.0, 'ap': 2.0, 't': quote_time}}
    if daily_bar is not None:
        snapshot['dailyBar'] = daily_bar
    page = {'snapshots': {'AAPL261120C00250000': snapshot}, 'next_page_token': None}
    options, _, _ = _options(snapshot_pages=[page], contracts=[{'option_contracts': [], 'next_page_token': None}])
    (row,) = options.chain('AAPL', '2026-11-20')
    return row['volume']


def test_session_volume_of_a_bar_from_a_newer_session_is_nan():
    assert math.isnan(_volume({'v': 7, 't': '2026-10-05T04:00:00Z'}))


def test_session_volume_of_a_bar_without_a_timestamp_is_nan():
    assert math.isnan(_volume({'v': 7}))


def test_session_volume_of_a_bar_from_an_older_session_is_zero():
    assert _volume({'v': 7, 't': '2026-10-01T04:00:00Z'}) == 0.0


def test_session_volume_compares_dates_in_new_york_not_utc():
    # 00:30Z on 10-03 is 20:30 ET on 10-02; the bar of 10-02 04:00Z opens that same ET session
    assert _volume({'v': 7, 't': '2026-10-02T04:00:00Z'}, quote_time='2026-10-03T00:30:00Z') == 7.0
    # and the next ET session's bar (10-03 04:00Z = 10-03 00:00 ET) is newer than that quote
    assert math.isnan(_volume({'v': 7, 't': '2026-10-03T04:00:00Z'}, quote_time='2026-10-03T00:30:00Z'))


def _contract_page(symbols, token):
    return {'option_contracts': [_contract(symbol, str(int(symbol[-8:]) / 1000), 'call' if 'C' in symbol[6:] else 'put',
                                           '1') for symbol in symbols], 'next_page_token': token}


def test_chain_follows_every_snapshot_and_contract_page():
    snapshot = SNAPSHOTS['snapshots']['AAPL261120C00250000']
    contract_pages = [_contract_page(['AAPL261120C00100000', 'AAPL261120C00110000'], 'p2'),
                      _contract_page(['AAPL261120C00120000', 'AAPL261120C00130000'], None)]
    snapshot_pages = [{'snapshots': {'AAPL261120C00110000': snapshot, 'AAPL261120C00140000': snapshot},
                       'next_page_token': 'p2'},
                      {'snapshots': {'AAPL261120C00150000': snapshot, 'AAPL261120C00130000': snapshot},
                       'next_page_token': None}]
    options, _, _ = _options(snapshot_pages=snapshot_pages, contracts=contract_pages)
    rows = options.chain('AAPL', '2026-11-20')
    assert [row['strike'] for row in rows] == [100.0, 110.0, 120.0, 130.0, 140.0, 150.0]
    assert [math.isnan(row['open_interest']) for row in rows] == [False] * 4 + [True] * 2
