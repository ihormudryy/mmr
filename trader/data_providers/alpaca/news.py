"""News headlines from Alpaca (Benzinga content). Alpaca has no sentiment — none is invented."""

from typing import Optional

from trader.data_providers.capabilities import make_news_item
from trader.data_providers.symbols import to_alpaca_symbol

NEWS_PATH = '/v1beta1/news'
MAX_LIMIT = 50


class AlpacaNews:
    def __init__(self, client):
        self._client = client

    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        params = {'limit': min(limit, MAX_LIMIT), 'sort': 'desc'}
        if ticker:
            params = {'symbols': to_alpaca_symbol(ticker), **params}
        articles = self._client.get_json(NEWS_PATH, params).get('news') or []
        return [make_news_item(
            id=str(a.get('id', '')),
            published=(a.get('created_at') or '')[:19],
            title=a.get('headline') or '',
            summary=a.get('summary') or '',
            url=a.get('url') or '',
            author=a.get('author') or '',
            source=f"alpaca/{a.get('source') or 'unknown'}",
            tickers=list(a.get('symbols') or []),
        ) for a in articles[:limit]]
