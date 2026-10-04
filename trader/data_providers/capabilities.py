"""What a market-data provider can do, as structural interfaces."""

import datetime as dt
from enum import Enum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

import pandas as pd

from trader.objects import BarSize


class Capability(str, Enum):
    HISTORY = 'history'
    QUOTES = 'quotes'
    MOVERS = 'movers'
    NEWS = 'news'


HISTORY_COLUMNS: tuple[str, ...] = (
    'open', 'high', 'low', 'close', 'volume', 'average', 'bar_count', 'bar_size', 'what_to_show',
)


@runtime_checkable
class HistoryProvider(Protocol):
    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        """Bars for whole days start_date..end_date inclusive, indexed by tz-aware `date`."""
        ...


QUOTE_FIELDS: tuple[str, ...] = (
    'symbol', 'time', 'last', 'bid', 'ask', 'bid_size', 'ask_size', 'open', 'high', 'low', 'close',
    'volume', 'previous_close', 'change', 'change_pct', 'exchange', 'currency', 'name', 'feed', 'error',
)
_QUOTE_TEXT_FIELDS = frozenset({'symbol', 'time', 'exchange', 'currency', 'name', 'feed', 'error'})

MOVER_COLUMNS: tuple[str, ...] = (
    'ticker', 'name', 'close', 'volume', 'change', 'change_pct', 'provider', 'note',
)

NEWS_FIELDS: tuple[str, ...] = (
    'id', 'published', 'title', 'summary', 'url', 'author', 'source', 'tickers', 'sentiment', 'insights',
)
_NEWS_LIST_FIELDS = frozenset({'tickers', 'insights'})


def make_quote(symbol: str, **fields: Any) -> dict:
    unknown = set(fields) - set(QUOTE_FIELDS)
    if unknown:
        raise TypeError(f'unknown quote field(s): {sorted(unknown)}')
    quote = {name: ('' if name in _QUOTE_TEXT_FIELDS else float('nan')) for name in QUOTE_FIELDS}
    quote.update(fields)
    quote['symbol'] = symbol.strip().upper()
    return quote


def make_news_item(**fields: Any) -> dict:
    unknown = set(fields) - set(NEWS_FIELDS)
    if unknown:
        raise TypeError(f'unknown news field(s): {sorted(unknown)}')
    item = {name: ([] if name in _NEWS_LIST_FIELDS else '') for name in NEWS_FIELDS}
    item.update(fields)
    return item


@runtime_checkable
class QuoteProvider(Protocol):
    def quotes(self, symbols: Sequence[str]) -> list[dict]:
        """One make_quote() dict per requested symbol, in request order; failures set `error`."""
        ...


@runtime_checkable
class MoversProvider(Protocol):
    markets: frozenset

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        """Frame starting with MOVER_COLUMNS, sorted by change_pct for `direction`."""
        ...


@runtime_checkable
class NewsProvider(Protocol):
    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        """Newest first, at most `limit` make_news_item() dicts; ticker None means general news."""
        ...
