from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import NEWS_FIELDS, Capability, make_news_item
from trader.data_providers.massive.news import MassiveBenzingaNews, MassiveTickerNews


def _polygon_article():
    return SimpleNamespace(id='p1', published_utc='2026-10-03T12:00:00Z', title='T', description='D',
                           article_url='u', author='A', tickers=['AAPL'],
                           insights=[SimpleNamespace(ticker='AAPL', sentiment='positive',
                                                     sentiment_reasoning='because')])


def test_polygon_news_item():
    client = MagicMock()
    client.list_ticker_news.return_value = iter([_polygon_article(), _polygon_article()])
    items = MassiveTickerNews(client).news('AAPL', 1)
    client.list_ticker_news.assert_called_once_with(ticker='AAPL', limit=1)
    assert len(items) == 1 and tuple(items[0]) == NEWS_FIELDS
    item = items[0]
    assert item['published'] == '2026-10-03T12:00:00' and item['summary'] == 'D' and item['source'] == 'polygon'
    assert item['sentiment'] == 'positive'
    assert item['insights'] == [{'ticker': 'AAPL', 'sentiment': 'positive', 'reasoning': 'because'}]


def test_benzinga_news_item():
    client = MagicMock()
    client.list_benzinga_news.return_value = iter([SimpleNamespace(
        benzinga_id=7, published='2026-10-03T12:00:00Z', title='T', teaser='Z', url='u', author='A',
        tickers=['MSFT'])])
    (item,) = MassiveBenzingaNews(client).news(None, 5)
    client.list_benzinga_news.assert_called_once_with(tickers=None, limit=5)
    assert item['summary'] == 'Z' and item['source'] == 'benzinga' and item['sentiment'] == ''


def _mmr(items):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    provider = MagicMock()
    provider.news.return_value = items
    mmr._provider = MagicMock(return_value=provider)
    return mmr


def test_sdk_news_frame_without_sentiment_column():
    mmr = _mmr([make_news_item(published='p', title='t', tickers=['A', 'B'], author='x', url='u', summary='s')])
    frame = mmr.news('A', limit=3)
    mmr._provider.assert_called_once_with(Capability.NEWS, None)
    assert list(frame.columns) == ['published', 'title', 'tickers', 'author', 'url', 'summary']
    assert frame.loc[0, 'tickers'] == 'A, B'


def test_sdk_news_frame_with_sentiment_column():
    mmr = _mmr([make_news_item(title='t', sentiment='negative')])
    assert 'sentiment' in mmr.news(source='polygon').columns


def test_sdk_news_detail_shape():
    mmr = _mmr([make_news_item(title='t', tickers=['A'], summary='s', insights=[{'ticker': 'A'}])])
    (article,) = mmr.news_detail('A')
    assert set(article) == {'title', 'published', 'author', 'tickers', 'url', 'summary', 'insights'}
    assert article['tickers'] == ['A'] and article['insights'] == [{'ticker': 'A'}]


def test_cli_news_prints_provider_error(capsys):
    from argparse import Namespace
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_news
    mmr = MagicMock()
    mmr.news.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    _handle_news(mmr, Namespace(ticker='AAPL', detail=False, limit=3, source=None))
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_cli_news_sources_come_from_registry():
    from trader.mmr_cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['news']).source is None
    assert parser.parse_args(['news', 'AAPL', '--source', 'benzinga']).source == 'benzinga'
