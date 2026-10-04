"""Latest prices from Alpaca's free IEX feed (IEX is a few percent of US volume)."""

import math
from typing import Sequence

from trader.data_providers.capabilities import make_quote
from trader.data_providers.symbols import to_alpaca_symbol

SNAPSHOTS_PATH = '/v2/stocks/snapshots'
CHUNK_SIZE = 100


def _number(mapping, key) -> float:
    """Extract a number from mapping, handling missing keys and None values."""
    value = mapping.get(key)
    if value is None:
        return float('nan')
    return float(value)


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
    last = _number(trade, 'p')

    # Error if no latest trade price
    if math.isnan(last):
        return make_quote(symbol, feed='iex', error=f'alpaca snapshot has no latest trade for {alpaca_symbol}')

    previous_close = _number(previous, 'c')
    change = last - previous_close
    return make_quote(
        symbol,
        time=trade.get('t', ''),
        last=last,
        bid=_number(quote, 'bp'), ask=_number(quote, 'ap'),
        bid_size=_number(quote, 'bs'), ask_size=_number(quote, 'as'),
        open=_number(day, 'o'), high=_number(day, 'h'),
        low=_number(day, 'l'), close=_number(day, 'c'),
        volume=_number(day, 'v'),
        previous_close=previous_close,
        change=change,
        change_pct=change / previous_close * 100 if previous_close else nan,
        currency='USD',
        feed='iex',
    )
