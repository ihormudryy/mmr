import json
from pathlib import Path

import pytest

from trader.data_providers.alpaca.movers import AlpacaMovers
from trader.data_providers.capabilities import MOVER_COLUMNS
from trader.data_providers.errors import CapabilityNotSupported

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_movers_stocks.json').read_text())


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.response


def test_stock_gainers_frame():
    client = FakeClient(FIXTURE)
    frame = AlpacaMovers(client).movers('stocks', 'gainers')
    assert client.calls == [('/v1beta1/screener/stocks/movers', {'top': 50})]
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    assert frame['ticker'].tolist() == ['HPAIW', 'AMOD', 'CYCUW', 'SDEV', 'MN']
    assert frame.loc[1, 'close'] == 3.49 and frame.loc[1, 'change_pct'] == 198.29
    assert frame['volume'].isna().all() and set(frame['provider']) == {'alpaca'}
    assert frame.loc[0, 'note'] == 'as of 2026-10-02T23:59:00Z'


def test_losers_sorted_ascending():
    frame = AlpacaMovers(FakeClient(FIXTURE)).movers('stocks', 'losers')
    assert frame['ticker'].tolist() == ['ASTLW', 'SBFMW']


def test_crypto_path():
    client = FakeClient({'gainers': [], 'losers': [], 'last_updated': ''})
    AlpacaMovers(client).movers('crypto', 'gainers')
    assert client.calls[0][0] == '/v1beta1/screener/crypto/movers'


def test_unsupported_market_raises():
    with pytest.raises(CapabilityNotSupported, match='indices movers') as info:
        AlpacaMovers(FakeClient(FIXTURE)).movers('indices', 'gainers')
    assert info.value.supported == ['etf_proxy', 'massive']


def test_null_numbers_become_nan():
    payload = {'gainers': [{'symbol': 'AAPL', 'price': None, 'change': None, 'percent_change': 2.0}],
               'losers': [], 'last_updated': ''}
    frame = AlpacaMovers(FakeClient(payload)).movers('stocks', 'gainers')
    assert frame.loc[0, 'close'] != frame.loc[0, 'close'] and frame.loc[0, 'change'] != frame.loc[0, 'change']
    assert frame.loc[0, 'change_pct'] == 2.0
