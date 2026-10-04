"""Latest prices from Alpaca's free IEX feed (IEX is a few percent of US volume)."""

import datetime as dt
import math
from typing import Optional, Sequence

import pandas as pd

from trader.data_providers.alpaca._numbers import number_or_nan
from trader.data_providers.capabilities import make_quote
from trader.data_providers.symbols import to_alpaca_symbol

SNAPSHOTS_PATH = '/v2/stocks/snapshots'
CHUNK_SIZE = 100
MARKET_TIMEZONE = 'America/New_York'


class AlpacaQuotes:
    def __init__(self, client):
        self._client = client

    def quotes(self, symbols: Sequence[str]) -> list[dict]:
        requested: list[tuple[str, str]] = []
        results: dict[str, dict] = {}
        for symbol in symbols:
            try:
                requested.append((symbol, to_alpaca_symbol(symbol)))
            except ValueError as ex:
                results[symbol] = make_quote(symbol, feed='iex', error=str(ex))
        for start in range(0, len(requested), CHUNK_SIZE):
            chunk = requested[start:start + CHUNK_SIZE]
            snapshots = self._client.get_json(
                SNAPSHOTS_PATH, {'symbols': ','.join(alpaca for _, alpaca in chunk), 'feed': 'iex'})
            for symbol, alpaca in chunk:
                results[symbol] = _to_quote(symbol, alpaca, snapshots.get(alpaca))
        return [results[symbol] for symbol in symbols]


def _to_quote(symbol: str, alpaca_symbol: str, snapshot) -> dict:
    if not snapshot:
        return make_quote(symbol, feed='iex', error=f'alpaca has no snapshot for {alpaca_symbol}')
    trade = snapshot.get('latestTrade') or {}
    quote = snapshot.get('latestQuote') or {}
    day = snapshot.get('dailyBar') or {}
    previous = snapshot.get('prevDailyBar') or {}

    nan = float('nan')
    last = number_or_nan(trade, 'p')

    # Error if no latest trade price
    if math.isnan(last):
        return make_quote(symbol, feed='iex', error=f'alpaca snapshot has no latest trade for {alpaca_symbol}')

    previous_close = _previous_session_close(trade, day, previous)
    change = last - previous_close
    return make_quote(
        symbol,
        time=trade.get('t', ''),
        last=last,
        bid=number_or_nan(quote, 'bp'), ask=number_or_nan(quote, 'ap'),
        bid_size=number_or_nan(quote, 'bs'), ask_size=number_or_nan(quote, 'as'),
        open=number_or_nan(day, 'o'), high=number_or_nan(day, 'h'),
        low=number_or_nan(day, 'l'), close=number_or_nan(day, 'c'),
        volume=number_or_nan(day, 'v'),
        previous_close=previous_close,
        change=change,
        change_pct=change / previous_close * 100 if previous_close else nan,
        currency='USD',
        feed='iex',
    )


def _previous_session_close(trade: dict, day: dict, previous: dict) -> float:
    """Close of the session before the latest trade's session.

    Before Monday's open dailyBar is still Friday's bar, so Friday is the previous
    session, not prevDailyBar (Thursday). Without both dates there is no safe answer.
    """
    trade_date, bar_date = _market_date(trade.get('t')), _market_date(day.get('t'))
    if trade_date is None or bar_date is None:
        return float('nan')
    if trade_date > bar_date:
        return number_or_nan(day, 'c')
    return number_or_nan(previous, 'c')


def _market_date(timestamp) -> Optional[dt.date]:
    if not timestamp:
        return None
    try:
        # pandas, not datetime.fromisoformat: Alpaca sends nanoseconds.
        return pd.Timestamp(timestamp).tz_convert(MARKET_TIMEZONE).date()
    except (TypeError, ValueError):
        return None
