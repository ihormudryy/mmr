import json
from pathlib import Path

import pytest

from trader.data_providers.alpaca.news import AlpacaNews
from trader.data_providers.capabilities import NEWS_FIELDS

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_news_aapl.json').read_text())


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.response


def test_maps_article_to_news_item():
    client = FakeClient(FIXTURE)
    (item,) = AlpacaNews(client).news('aapl', 3)
    assert client.calls == [('/v1beta1/news', {'symbols': 'AAPL', 'limit': 3, 'sort': 'desc'})]
    assert tuple(item) == NEWS_FIELDS
    assert item['id'] == '62152341' and item['published'] == '2026-10-04T11:00:23'
    assert item['title'].startswith('Apple, Meta') and item['summary'].startswith('Apple dominated')
    assert item['tickers'] == ['AAPL', 'META'] and item['source'] == 'alpaca/benzinga'
    assert item['sentiment'] == '' and item['insights'] == []


def test_general_news_has_no_symbols_param():
    client = FakeClient({'news': []})
    assert AlpacaNews(client).news(None, 10) == []
    assert 'symbols' not in client.calls[0][1]


def test_limit_is_capped_at_50():
    client = FakeClient({'news': []})
    AlpacaNews(client).news('AAPL', 500)
    assert client.calls[0][1]['limit'] == 50


def test_invalid_ticker_raises():
    with pytest.raises(ValueError):
        AlpacaNews(FakeClient({'news': []})).news('AAPL;DROP', 3)
