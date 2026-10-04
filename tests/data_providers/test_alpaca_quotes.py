import json
import math
from pathlib import Path

import pytest

from trader.data_providers.alpaca.quotes import AlpacaQuotes

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_snapshots_aapl.json').read_text())


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.responses.pop(0)


def test_maps_snapshot_to_quote():
    client = FakeClient([FIXTURE])
    (quote,) = AlpacaQuotes(client).quotes(['aapl'])
    assert client.calls == [('/v2/stocks/snapshots', {'symbols': 'AAPL', 'feed': 'iex'})]
    assert quote['symbol'] == 'AAPL' and quote['error'] == '' and quote['feed'] == 'iex'
    assert quote['last'] == 333.75 and quote['time'] == '2026-10-02T19:59:59.099728764Z'
    assert (quote['bid'], quote['ask'], quote['bid_size'], quote['ask_size']) == (316.53, 350, 40, 40)
    assert (quote['open'], quote['high'], quote['low'], quote['close']) == (333.16, 334.525, 330.66, 333.75)
    assert quote['volume'] == 841636 and quote['previous_close'] == 330.44
    assert quote['change'] == pytest.approx(3.31)
    assert quote['change_pct'] == pytest.approx(3.31 / 330.44 * 100)
    assert quote['currency'] == 'USD'


def test_class_share_symbol_maps_back_to_request():
    client = FakeClient([{'BRK.B': FIXTURE['AAPL']}])
    (quote,) = AlpacaQuotes(client).quotes(['BRK B'])
    assert client.calls[0][1]['symbols'] == 'BRK.B'
    assert quote['symbol'] == 'BRK B' and quote['error'] == ''


def test_unknown_symbol_gets_error_row():
    client = FakeClient([FIXTURE])
    quotes = AlpacaQuotes(client).quotes(['AAPL', 'ZZZZQ'])
    assert [q['symbol'] for q in quotes] == ['AAPL', 'ZZZZQ']
    assert quotes[1]['error'] == 'alpaca has no snapshot for ZZZZQ'
    assert math.isnan(quotes[1]['last'])


def test_invalid_symbol_gets_error_row_without_request():
    client = FakeClient([])
    (quote,) = AlpacaQuotes(client).quotes(['AAPL;DROP'])
    assert client.calls == []
    assert 'not a valid Alpaca stock symbol' in quote['error']


def test_missing_prev_close_leaves_change_nan():
    snapshot = dict(FIXTURE['AAPL'])
    del snapshot['prevDailyBar']
    (quote,) = AlpacaQuotes(FakeClient([{'AAPL': snapshot}])).quotes(['AAPL'])
    assert math.isnan(quote['previous_close']) and math.isnan(quote['change']) and math.isnan(quote['change_pct'])


def test_chunks_of_100():
    symbols = [f'S{i}' for i in range(150)]
    client = FakeClient([{}, {}])
    AlpacaQuotes(client).quotes(symbols)
    assert [len(call[1]['symbols'].split(',')) for call in client.calls] == [100, 50]


def test_registered_as_default_quotes_source():
    from trader.data_providers.capabilities import Capability
    from trader.data_providers.registry import ProviderRegistry
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    assert registry.default_source(Capability.QUOTES) == 'alpaca'
    assert isinstance(registry.get(Capability.QUOTES), AlpacaQuotes)


def test_snapshot_without_trade_is_error_row():
    """Snapshot with no latestTrade should return error row, not good row with NaN last."""
    snapshot = dict(FIXTURE['AAPL'])
    del snapshot['latestTrade']
    (quote,) = AlpacaQuotes(FakeClient([{'AAPL': snapshot}])).quotes(['AAPL'])
    assert quote['error'] != '', "Should have error when no latestTrade"
    assert 'no latest trade' in quote['error']
    assert math.isnan(quote['last'])


def test_null_fields_become_nan():
    """JSON null values should become NaN, not raise TypeError."""
    snapshot = dict(FIXTURE['AAPL'])
    snapshot['latestQuote'] = dict(FIXTURE['AAPL']['latestQuote'])
    snapshot['latestQuote']['bp'] = None
    (quote,) = AlpacaQuotes(FakeClient([{'AAPL': snapshot}])).quotes(['AAPL'])
    assert quote['error'] == '', "Should be good quote despite null bp"
    assert math.isnan(quote['bid']), "bp: null should be NaN bid"
    assert quote['ask'] == 350, "ap should still be 350"
    assert quote['last'] == 333.75, "last price should be 333.75"
