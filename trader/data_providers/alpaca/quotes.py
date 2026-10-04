"""Latest prices from Alpaca's free IEX feed (IEX is a few percent of US volume)."""

from typing import Sequence

from trader.data_providers.capabilities import make_quote
from trader.data_providers.symbols import to_alpaca_symbol

SNAPSHOTS_PATH = '/v2/stocks/snapshots'
CHUNK_SIZE = 100


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
    last = float(trade.get('p', nan))
    previous_close = float(previous.get('c', nan))
    change = last - previous_close
    return make_quote(
        symbol,
        time=trade.get('t', ''),
        last=last,
        bid=float(quote.get('bp', nan)), ask=float(quote.get('ap', nan)),
        bid_size=float(quote.get('bs', nan)), ask_size=float(quote.get('as', nan)),
        open=float(day.get('o', nan)), high=float(day.get('h', nan)),
        low=float(day.get('l', nan)), close=float(day.get('c', nan)),
        volume=float(day.get('v', nan)),
        previous_close=previous_close,
        change=change,
        change_pct=change / previous_close * 100 if previous_close else nan,
        currency='USD',
        feed='iex',
    )
