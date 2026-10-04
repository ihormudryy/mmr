"""Alpaca scan source for the shared idea scanner.

Uses the free plan's 15-minute-delayed consolidated feed (delayed_sip), so volume
and spread are market-wide, not IEX-only. Discovery cannot scan the whole market:
it uses the screener's top movers and most-actives and says so in the notice.
"""

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

import requests

from trader.data_providers.alpaca._numbers import number_or_nan
from trader.data_providers.capabilities import Discovery
from trader.data_providers.errors import (
    ProviderEntitlementError, ProviderError, ProviderNotConfigured, ProviderRateLimited)
from trader.data_providers.symbols import to_alpaca_symbol
from trader.objects import BarSize
from trader.tools.idea_scanner import IdeaScannerError, compute_ema, compute_rsi, compute_sma

logger = logging.getLogger(__name__)

SNAPSHOTS_PATH = '/v2/stocks/snapshots'
FEED = 'delayed_sip'
SCREENER_TOP = 50
SNAPSHOT_CHUNK = 100
DELAY_NOTE = 'Alpaca prices are 15-minute delayed (consolidated SIP)'
INDICATOR_LOOKBACK_DAYS = 120
FATAL_PROVIDER_ERRORS = (ProviderNotConfigured, ProviderEntitlementError, ProviderRateLimited)
ASSET_LIST_ERRORS = (ProviderError, requests.RequestException, ValueError, TypeError, AttributeError)
ASSET_LIST_FALLBACK_NOTE = 'Alpaca asset list unavailable; warrant check falls back to the ticker-suffix rule'


def _looks_like_warrant(symbol: str) -> bool:
    # Same suffix rule as the Massive scanner: a trailing W only on 5+ char tickers.
    # Used only without the asset list: the bare WS suffix also drops real stocks like NWS.
    return symbol.endswith(('.WS', 'WS', '.U', '.R')) or (len(symbol) >= 5 and symbol.endswith('W'))


def _percent(numerator: float, base: float) -> float:
    return (numerator / base) * 100.0 if base == base and base > 0 else 0.0


def _is_positive(bar: dict, key: str) -> bool:
    value = number_or_nan(bar, key)
    return value == value and value > 0


def _has_price(snapshot: Optional[dict]) -> bool:
    return _is_positive((snapshot or {}).get('dailyBar') or {}, 'c')


def _has_previous_close(snapshot: Optional[dict]) -> bool:
    return _is_positive((snapshot or {}).get('prevDailyBar') or {}, 'c')


def candidate_from_snapshot(ticker: str, snapshot: dict) -> Optional[dict]:
    snapshot = snapshot or {}
    day = snapshot.get('dailyBar') or {}
    prev = snapshot.get('prevDailyBar') or {}
    quote = snapshot.get('latestQuote') or {}
    if not _has_price(snapshot) or not _has_previous_close(snapshot):
        return None
    price, prev_close = number_or_nan(day, 'c'), number_or_nan(prev, 'c')
    day_open, high, low = number_or_nan(day, 'o'), number_or_nan(day, 'h'), number_or_nan(day, 'l')
    if day_open != day_open or high != high or low != low:
        return None
    prev_volume = number_or_nan(prev, 'v')
    volume = number_or_nan(day, 'v')
    vwap = number_or_nan(day, 'vw')
    bid, ask = number_or_nan(quote, 'bp'), number_or_nan(quote, 'ap')

    return {
        'ticker': ticker,
        'price': round(price, 2),
        'change_pct': round(_percent(price - prev_close, prev_close), 2),
        'volume': int(volume) if volume == volume else 0,
        'gap_pct': round(_percent(day_open - prev_close, prev_close), 2),
        'rel_vol': round(volume / prev_volume, 2) if prev_volume == prev_volume and prev_volume > 0 else 0.0,
        'range_pct': round(_percent(high - low, low), 2),
        'spread_pct': round(_percent(ask - bid, price), 3) if bid > 0 and ask > 0 else 0.0,
        'vwap': round(vwap, 2) if vwap == vwap else 0.0,
    }


def _indicator_values(closes: list, needed: list) -> dict:
    values = {}
    for indicator in needed:
        if indicator == 'rsi':
            values['rsi'] = compute_rsi(closes, period=14)
        elif indicator == 'ema_9':
            values['ema_9'] = compute_ema(closes, window=9)
        elif indicator == 'sma_20':
            values['sma_20'] = compute_sma(closes, window=20)
        elif indicator == 'sma_50':
            values['sma_50'] = compute_sma(closes, window=50)
    return values


class AlpacaScanSource:
    name = 'alpaca'
    supports_fundamentals = False

    def __init__(self, client, assets=None, history=None, news=None):
        self._client = client
        self._assets = assets
        self._history = history
        self._news = news
        self._asset_list_unavailable = False

    def discover(self, source, tickers, universe_symbols, use_market_scan) -> Discovery:
        if source == 'tickers' and tickers:
            return self._snapshot_discovery(tickers)
        if source == 'universe' and universe_symbols:
            return self._snapshot_discovery(universe_symbols)
        symbols = self._screener_symbols()
        discovery = self._snapshot_discovery(symbols)
        notice = (f'Alpaca discovery: {len(discovery.candidates)} symbols from top movers + most-actives '
                  f'(not the full market). {discovery.notice}')
        if self._asset_list_unavailable:
            notice = f'{notice} {ASSET_LIST_FALLBACK_NOTE}.'
        return Discovery(discovery.candidates, notice)

    def _screener_symbols(self) -> list:
        movers = self._client.get_json('/v1beta1/screener/stocks/movers', {'top': SCREENER_TOP})
        actives = self._client.get_json('/v1beta1/screener/stocks/most-actives',
                                        {'by': 'volume', 'top': SCREENER_TOP})
        rows = (movers.get('gainers') or []) + (movers.get('losers') or []) + (actives.get('most_actives') or [])
        seen, symbols = set(), []
        for row in rows:
            symbol = (row.get('symbol') or '').upper()
            if not symbol or symbol in seen or self._is_excluded(symbol):
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _is_excluded(self, symbol: str) -> bool:
        if self._assets is None:
            return _looks_like_warrant(symbol)
        try:
            return bool(self._assets.is_derivative_unit(symbol))
        except ASSET_LIST_ERRORS as ex:
            self._drop_asset_list(ex)
            return _looks_like_warrant(symbol)

    def _drop_asset_list(self, error: Exception) -> None:
        logger.warning('alpaca asset list unavailable, scan uses the ticker-suffix warrant rule: %s', error)
        self._assets = None
        self._asset_list_unavailable = True

    def _snapshot_discovery(self, symbols: Iterable[str]) -> Discovery:
        requested, invalid = [], []
        for symbol in symbols:
            try:
                normalised = to_alpaca_symbol(symbol)
            except ValueError:
                invalid.append(symbol)
                continue
            if normalised not in requested:
                requested.append(normalised)
        candidates, missing, no_previous_close, incomplete_bar = [], [], [], []
        for start in range(0, len(requested), SNAPSHOT_CHUNK):
            chunk = requested[start:start + SNAPSHOT_CHUNK]
            snapshots = self._client.get_json(SNAPSHOTS_PATH, {'symbols': ','.join(chunk), 'feed': FEED})
            for symbol in chunk:
                snapshot = snapshots.get(symbol)
                candidate = candidate_from_snapshot(symbol, snapshot)
                if candidate is not None:
                    candidates.append(candidate)
                elif not _has_price(snapshot):
                    missing.append(symbol)
                elif not _has_previous_close(snapshot):
                    no_previous_close.append(symbol)
                else:
                    incomplete_bar.append(symbol)
        parts = [f'{DELAY_NOTE}.']
        if invalid:
            parts.append(f'Not valid Alpaca symbols: {", ".join(invalid)}.')
        if missing:
            parts.append(f'No Alpaca snapshot for: {", ".join(missing)}.')
        if no_previous_close:
            parts.append(f'No previous close from Alpaca for: {", ".join(no_previous_close)} (dropped).')
        if incomplete_bar:
            parts.append(f'Incomplete Alpaca daily bar for: {", ".join(incomplete_bar)} (dropped).')
        return Discovery(candidates, ' '.join(parts))

    def indicators(self, tickers, needed):
        if not needed or not tickers:
            return {}
        if self._history is None:
            raise IdeaScannerError('Alpaca scan needs a history provider for indicators')
        end = dt.datetime.now()
        start = end - dt.timedelta(days=INDICATOR_LOOKBACK_DAYS)

        def fetch_one(ticker):
            try:
                frame = self._history.get_history(ticker, BarSize.Days1, start, end)
                closes = [float(c) for c in frame['close'].dropna()] if not frame.empty else []
            except FATAL_PROVIDER_ERRORS:
                raise
            except Exception as ex:
                logger.warning('alpaca indicator history failed for %s: %s', ticker, ex)
                return ticker, {}, ex
            return ticker, _indicator_values(closes, needed), None

        pool = ThreadPoolExecutor(max_workers=5)
        try:
            results = list(pool.map(fetch_one, tickers))
        finally:
            # On a fatal error, drop queued tickers instead of waiting for them to fail one by one.
            pool.shutdown(wait=False, cancel_futures=True)
        errors = [error for _, _, error in results if error is not None]
        if len(errors) == len(results):
            raise IdeaScannerError(f'Alpaca indicators failed for all {len(results)} tickers: {errors[0]}')
        return {ticker: values for ticker, values, _ in results}

    def names(self, tickers):
        if self._assets is None:
            return {}
        try:
            names = {ticker: self._assets.name(ticker) for ticker in tickers}
        except ASSET_LIST_ERRORS as ex:
            self._drop_asset_list(ex)
            return {}
        return {ticker: name for ticker, name in names.items() if name}

    def fundamentals(self, tickers):
        raise IdeaScannerError(
            'ideas --fundamentals has no free source yet (Finnhub ratios arrive in phase 4); '
            'use --source massive or --source twelvedata for fundamentals'
        )

    def news(self, tickers):
        if self._news is None:
            raise IdeaScannerError('Alpaca scan needs a news provider for --news')
        out = {}
        for ticker in tickers:
            try:
                items = self._news.news(ticker, 1)
            except FATAL_PROVIDER_ERRORS:
                raise
            except Exception as ex:
                logger.warning('alpaca news failed for %s: %s', ticker, ex)
                continue
            if items:
                title = items[0]['title']
                out[ticker] = {
                    'headline': title if len(title) <= 120 else title[:117] + '...',
                    'news_date': (items[0]['published'] or '')[:10],
                    'sentiment': '',
                    'catalyst': '',
                }
        return out
