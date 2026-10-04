import math
from unittest.mock import MagicMock

import pytest

from trader.data_providers.twelvedata.quotes import TwelveDataQuotes


class _StubTDPayload:
    def __init__(self, payload):
        self._payload = payload

    def as_json(self):
        return self._payload


AAPL = {'symbol': 'AAPL', 'name': 'Apple Inc', 'exchange': 'NASDAQ', 'currency': 'USD',
        'datetime': '2026-04-17', 'open': '268.0', 'high': '271.0', 'low': '267.5', 'close': '270.19',
        'volume': '41234567', 'previous_close': '270.71', 'change': '-0.51999', 'percent_change': '-0.19209'}
MSFT = dict(AAPL, symbol='MSFT', name='Microsoft', close='420.10')


def _quotes(payload, symbols):
    client = MagicMock()
    client.quote.return_value = _StubTDPayload(payload)
    return TwelveDataQuotes(client).quotes(symbols), client


def test_basic_quote_payload():
    (quote,), _ = _quotes(AAPL, ['AAPL'])
    assert quote['symbol'] == 'AAPL' and quote['last'] == pytest.approx(270.19)
    assert quote['previous_close'] == pytest.approx(270.71)
    assert quote['change'] == pytest.approx(-0.51999)
    assert math.isnan(quote['bid']) and math.isnan(quote['ask'])
    assert quote['exchange'] == 'NASDAQ' and quote['name'] == 'Apple Inc' and quote['feed'] == 'twelvedata'


def test_missing_field_yields_nan():
    (quote,), _ = _quotes({'symbol': 'AAPL'}, ['AAPL'])
    assert math.isnan(quote['last']) and math.isnan(quote['volume'])


def test_batch_uses_comma_join():
    quotes, client = _quotes({'AAPL': AAPL, 'MSFT': MSFT}, ['AAPL', 'MSFT'])
    assert [q['symbol'] for q in quotes] == ['AAPL', 'MSFT']
    assert quotes[1]['last'] == pytest.approx(420.10)
    client.quote.assert_called_once()
    assert client.quote.call_args.kwargs['symbol'] == 'AAPL,MSFT'


def test_single_symbol_flat_payload_is_keyed_by_requested_symbol():
    quotes, _ = _quotes(AAPL, ['aapl'])
    assert len(quotes) == 1 and quotes[0]['symbol'] == 'AAPL'
    assert quotes[0]['last'] == pytest.approx(270.19)


def test_chunks_above_120_symbols():
    symbols = [f'S{i}' for i in range(150)]
    _, client = _quotes({}, symbols)
    assert client.quote.call_count == 2
    assert len(client.quote.call_args_list[0].kwargs['symbol'].split(',')) == 120
    assert len(client.quote.call_args_list[1].kwargs['symbol'].split(',')) == 30


def test_missing_symbol_returns_error_row():
    quotes, _ = _quotes({'AAPL': AAPL, 'MISSING': {}}, ['AAPL', 'MISSING'])
    assert quotes[1]['symbol'] == 'MISSING'
    assert math.isnan(quotes[1]['last'])
    assert 'twelvedata returned no quote' in quotes[1]['error']


def test_batch_error_payload_becomes_error_row():
    bad = {'code': 400, 'message': '**symbol** not found: BADSYM', 'status': 'error'}
    quotes, _ = _quotes({'AAPL': AAPL, 'BADSYM': bad}, ['AAPL', 'BADSYM'])
    assert quotes[0]['error'] == '' and quotes[0]['last'] == pytest.approx(270.19)
    assert quotes[1]['symbol'] == 'BADSYM' and 'not found' in quotes[1]['error']
    assert math.isnan(quotes[1]['last'])


def test_single_symbol_error_response_becomes_error_row():
    bad = {'code': 400, 'message': '**symbol** not found: BADSYM', 'status': 'error'}
    quotes, _ = _quotes(bad, ['BADSYM'])
    assert len(quotes) == 1 and quotes[0]['symbol'] == 'BADSYM'
    assert 'not found' in quotes[0]['error']
