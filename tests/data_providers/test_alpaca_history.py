import datetime as dt
import json
from pathlib import Path

import pytest
import pytz

from trader.data_providers.alpaca.history import AlpacaHistoryProvider
from trader.data_providers.symbols import to_alpaca_symbol
from trader.objects import BarSize, WhatToShow

FIXTURE = Path(__file__).parent / 'fixtures' / 'alpaca_bars_aapl_1min_2023-01-03.json'
ET = pytz.timezone('US/Eastern')
A_LATER_SUNDAY = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.params = None

    def paginate(self, path, params):
        self.path, self.params = path, dict(params)
        yield from self.pages


def _provider(pages, now=A_LATER_SUNDAY):
    client = FakeClient(pages)
    return AlpacaHistoryProvider(client, now=lambda: now), client


def test_converts_bars_to_history_frame():
    provider, client = _provider([json.loads(FIXTURE.read_text())])
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert len(df) == 3
    first = df.iloc[0]
    assert df.index[0] == ET.localize(dt.datetime(2023, 1, 3, 9, 30))
    assert (first.open, first.high, first.low, first.close) == (130.25, 130.6999, 129.76, 129.76)
    assert first.volume == 2844202
    assert first.average == 130.225278
    assert first.bar_count == 23332
    assert first.bar_size == '1 min'
    assert first.what_to_show == int(WhatToShow.TRADES)


def test_request_params_use_sip_split_adjustment_and_whole_days():
    provider, client = _provider([{'bars': {}}])
    provider.get_history('AAPL', BarSize.Days1, dt.datetime(2023, 1, 3, 15, 0), dt.datetime(2023, 1, 5, 1, 0))
    assert client.path == '/v2/stocks/bars'
    assert client.params == {
        'symbols': 'AAPL', 'timeframe': '1Day', 'feed': 'sip', 'adjustment': 'split',
        'limit': 10000, 'sort': 'asc',
        'start': '2023-01-03T05:00:00Z',   # 00:00 ET
        'end': '2023-01-06T04:59:59Z',     # 23:59:59 ET on 2023-01-05
    }


def test_follows_next_page_token():
    page1 = {'bars': {'AAPL': [{'t': '2023-01-03T14:30:00Z', 'o': 1, 'h': 1, 'l': 1, 'c': 1, 'v': 1, 'vw': 1, 'n': 1}]}}
    page2 = {'bars': {'AAPL': [{'t': '2023-01-03T14:31:00Z', 'o': 2, 'h': 2, 'l': 2, 'c': 2, 'v': 2, 'vw': 2, 'n': 2}]}}
    provider, _ = _provider([page1, page2])
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert list(df.close) == [1, 2]


def test_mid_session_end_is_cut_to_previous_session():
    from unittest.mock import patch
    thursday_11am = ET.localize(dt.datetime(2026, 10, 1, 11, 0))
    provider, client = _provider([{'bars': {}}], now=thursday_11am)
    with patch('trader.data_providers.alpaca.history.logging') as log:
        provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2026, 9, 28), dt.datetime(2026, 10, 1))
    assert client.params['end'] == '2026-10-01T00:00:00Z'   # Wed 2026-09-30 20:00 ET
    assert any('end cut' in call.args[0] for call in log.info.call_args_list)


def test_window_entirely_after_last_completed_session_returns_empty_without_request():
    thursday_11am = ET.localize(dt.datetime(2026, 10, 1, 11, 0))
    provider, client = _provider([{'bars': {}}], now=thursday_11am)
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 1))
    assert df.empty
    assert client.params is None


def test_no_bars_returns_empty_frame():
    provider, _ = _provider([{'bars': {}}])
    assert provider.get_history('AAPL', BarSize.Days1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3)).empty


def test_output_timezone_follows_argument():
    provider, _ = _provider([json.loads(FIXTURE.read_text())])
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3),
                              timezone='UTC')
    assert str(df.index.tz) == 'UTC'
    assert df.index[0].hour == 14


@pytest.mark.parametrize('raw, expected', [('AAPL', 'AAPL'), (' aapl ', 'AAPL'), ('BRK B', 'BRK.B'),
                                           ('BRK.B', 'BRK.B')])
def test_symbol_mapping(raw, expected):
    assert to_alpaca_symbol(raw) == expected


@pytest.mark.parametrize('raw', ['', '  ', 'AAPL;DROP', 'BRK  B', 'A/B', 'ß', 'AAPLé'])
def test_symbol_mapping_rejects_junk(raw):
    with pytest.raises(ValueError):
        to_alpaca_symbol(raw)
