"""News from Massive: its own feed (with per-ticker sentiment) and Benzinga."""

from itertools import islice
from typing import Optional

from trader.data_providers.capabilities import make_news_item


class MassiveTickerNews:
    def __init__(self, client):
        self._client = client

    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        articles = islice(self._client.list_ticker_news(ticker=ticker, limit=limit), limit)
        return [_ticker_news_item(a) for a in articles]


class MassiveBenzingaNews:
    def __init__(self, client):
        self._client = client

    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        articles = islice(self._client.list_benzinga_news(tickers=ticker, limit=limit), limit)
        return [make_news_item(
            id=str(getattr(a, 'benzinga_id', '') or ''),
            published=(a.published or '')[:19],
            title=a.title or '',
            summary=a.teaser or '',
            url=a.url or '',
            author=a.author or '',
            source='benzinga',
            tickers=list(a.tickers or []),
        ) for a in articles]


def _ticker_news_item(article) -> dict:
    insights = [{'ticker': i.ticker, 'sentiment': i.sentiment, 'reasoning': i.sentiment_reasoning}
                for i in (article.insights or [])]
    return make_news_item(
        id=str(getattr(article, 'id', '') or ''),
        published=(article.published_utc or '')[:19],
        title=article.title or '',
        summary=article.description or '',
        url=article.article_url or '',
        author=article.author or '',
        source='polygon',
        tickers=list(article.tickers or []),
        sentiment=', '.join(i['sentiment'] for i in insights if i['sentiment']),
        insights=insights,
    )
