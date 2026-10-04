"""Massive (Polygon) scan source: snapshot discovery, server-side indicators, ratios, news."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from trader.data_providers.capabilities import Discovery
from trader.tools.idea_scanner import IdeaScannerError, is_data_entitlement_error

logger = logging.getLogger(__name__)


class MassiveScanSource:
    name = 'massive'
    supports_fundamentals = True

    def __init__(self, massive_client):
        self._client = massive_client

    def discover(self, source, tickers, universe_symbols, use_market_scan) -> Discovery:
        effective = 'market' if source == 'movers' and use_market_scan else source
        return Discovery(self._build_candidates(self._discover(effective, tickers, universe_symbols)))

    def indicators(self, tickers, needed):
        return self._fetch_indicators(tickers, needed)

    def names(self, tickers):
        return self._fetch_names(tickers)

    def fundamentals(self, tickers):
        return self._fetch_fundamentals(tickers)

    def news(self, tickers):
        return self._fetch_news(tickers)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover(
        self,
        source: str,
        tickers: Optional[List[str]],
        universe_symbols: Optional[List[str]],
    ) -> list:
        """Fetch raw snapshots from Massive.com."""
        try:
            return self._discover_raw(source, tickers, universe_symbols)
        except Exception as ex:
            if is_data_entitlement_error(ex):
                raise IdeaScannerError(
                    'Massive snapshot/movers not entitled on this API key '
                    '(Stocks Basic excludes snapshots — need Starter+ at '
                    'https://massive.com/pricing). '
                    f'Detail: {ex}. '
                    'Workaround: `ideas --source twelvedata --tickers AAPL MSFT NVDA` '
                    'or `--universe NAME` (TwelveData quotes work on Basic), '
                    'or upgrade the Massive plan.'
                ) from ex
            raise

    def _discover_raw(
        self,
        source: str,
        tickers: Optional[List[str]],
        universe_symbols: Optional[List[str]],
    ) -> list:
        if source == 'tickers' and tickers:
            return list(self._client.get_snapshot_all(
                market_type='stocks', tickers=tickers,
            ))
        elif source == 'universe' and universe_symbols:
            return list(self._client.get_snapshot_all(
                market_type='stocks', tickers=universe_symbols,
            ))
        elif source == 'market':
            # Full market scan — returns 10K+ snapshots. Filtering happens
            # in _build_candidates and _apply_filters before any indicator
            # API calls, so this is efficient (1 API call, local filtering).
            return list(self._client.get_snapshot_all(
                market_type='stocks',
            ))
        else:
            # Movers: fetch both gainers and losers for a broader pool
            gainers = list(self._client.get_snapshot_direction(
                market_type='stocks', direction='gainers',
            ))
            losers = list(self._client.get_snapshot_direction(
                market_type='stocks', direction='losers',
            ))
            return gainers + losers

    # ------------------------------------------------------------------
    # Candidate building
    # ------------------------------------------------------------------

    def _build_candidates(self, snapshots: list) -> List[Dict[str, Any]]:
        """Extract structured data from TickerSnapshot objects."""
        candidates = []
        seen = set()
        for snap in snapshots:
            ticker = snap.ticker or ''
            if not ticker or ticker in seen:
                continue
            # Skip warrants, units, rights, preferred stocks
            # (e.g. AEVAW, SRTAW, KKRpD, ACHR.U). A bare trailing 'W' only counts
            # as a warrant on a 5-char ticker (base+W) — otherwise legitimate
            # names like SNOW, DOW, LOW, GEO were being silently dropped.
            if ticker.endswith(('.WS', 'WS', '.U', '.R')) or (len(ticker) >= 5 and ticker.endswith('W')):
                continue
            if 'p' in ticker and ticker != ticker.upper():
                # Preferred stock: contains lowercase 'p' (e.g. KKRpD)
                continue
            seen.add(ticker)

            day = snap.day
            prev_day = snap.prev_day
            if not day:
                continue

            price = getattr(day, 'close', None) or 0.0
            volume = getattr(day, 'volume', None) or 0
            day_open = getattr(day, 'open', None) or 0.0
            day_high = getattr(day, 'high', None) or 0.0
            day_low = getattr(day, 'low', None) or 0.0
            vwap = getattr(day, 'vwap', None) or 0.0

            change_pct = snap.todays_change_percent or 0.0

            # Gap: (open - prev_close) / prev_close
            prev_close = 0.0
            prev_volume = 0
            if prev_day:
                prev_close = getattr(prev_day, 'close', None) or 0.0
                prev_volume = getattr(prev_day, 'volume', None) or 0

            gap_pct = 0.0
            if prev_close > 0:
                gap_pct = ((day_open - prev_close) / prev_close) * 100.0

            # Relative volume
            rel_vol = 0.0
            if prev_volume > 0:
                rel_vol = volume / prev_volume

            # Intraday range %
            range_pct = 0.0
            if day_low > 0:
                range_pct = ((day_high - day_low) / day_low) * 100.0

            # Spread — Massive API uses bid_price/ask_price fields
            spread_pct = 0.0
            if snap.last_quote and price > 0:
                bid = (getattr(snap.last_quote, 'bid_price', None)
                       or getattr(snap.last_quote, 'bid', None) or 0.0)
                ask = (getattr(snap.last_quote, 'ask_price', None)
                       or getattr(snap.last_quote, 'ask', None) or 0.0)
                if bid > 0 and ask > 0:
                    spread_pct = ((ask - bid) / price) * 100.0

            candidates.append({
                'ticker': ticker,
                'price': round(price, 2),
                'change_pct': round(change_pct, 2),
                'volume': int(volume),
                'gap_pct': round(gap_pct, 2),
                'rel_vol': round(rel_vol, 2),
                'range_pct': round(range_pct, 2),
                'spread_pct': round(spread_pct, 3),
                'vwap': round(vwap, 2),
            })

        return candidates

    # ------------------------------------------------------------------
    # Indicator fetching (parallel)
    # ------------------------------------------------------------------

    def _fetch_indicators(
        self,
        tickers: List[str],
        needed: List[str],
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """Fetch technical indicators in parallel via ThreadPoolExecutor."""
        if not needed or not tickers:
            return {}

        results: Dict[str, Dict[str, Optional[float]]] = {t: {} for t in tickers}

        def fetch_one(ticker: str, indicator: str) -> tuple:
            """Returns (ticker, indicator_name, value)."""
            try:
                if indicator == 'rsi':
                    res = self._client.get_rsi(
                        ticker, timespan='day', window=14, limit=1,
                    )
                    vals = list(res.values) if hasattr(res, 'values') else []
                    return (ticker, 'rsi', vals[0].value if vals else None)
                elif indicator == 'ema_9':
                    res = self._client.get_ema(
                        ticker, timespan='day', window=9, limit=1,
                    )
                    vals = list(res.values) if hasattr(res, 'values') else []
                    return (ticker, 'ema_9', vals[0].value if vals else None)
                elif indicator == 'sma_20':
                    res = self._client.get_sma(
                        ticker, timespan='day', window=20, limit=1,
                    )
                    vals = list(res.values) if hasattr(res, 'values') else []
                    return (ticker, 'sma_20', vals[0].value if vals else None)
                elif indicator == 'sma_50':
                    res = self._client.get_sma(
                        ticker, timespan='day', window=50, limit=1,
                    )
                    vals = list(res.values) if hasattr(res, 'values') else []
                    return (ticker, 'sma_50', vals[0].value if vals else None)
                else:
                    return (ticker, indicator, None)
            except Exception as e:
                logger.debug('indicator fetch failed: %s %s: %s', ticker, indicator, e)
                return (ticker, indicator, None)

        tasks = [(t, ind) for t in tickers for ind in needed]
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_one, t, ind): (t, ind) for t, ind in tasks}
            for future in as_completed(futures):
                ticker, indicator, value = future.result()
                results[ticker][indicator] = value

        return results

    # ------------------------------------------------------------------
    # Fundamentals fetching (parallel)
    # ------------------------------------------------------------------

    # Fields to extract from FinancialRatio objects
    _FUNDAMENTAL_FIELDS = [
        ('price_to_earnings', 'pe_ratio'),
        ('price_to_book', 'pb_ratio'),
        ('price_to_sales', 'ps_ratio'),
        ('debt_to_equity', 'debt_equity'),
        ('return_on_equity', 'roe'),
        ('return_on_assets', 'roa'),
        ('dividend_yield', 'div_yield'),
        ('ev_to_ebitda', 'ev_ebitda'),
        ('market_cap', 'mkt_cap'),
        ('earnings_per_share', 'eps'),
        ('free_cash_flow', 'fcf'),
    ]

    def _fetch_names(
        self,
        tickers: List[str],
    ) -> Dict[str, str]:
        """Fetch company names in parallel via get_ticker_details."""
        if not tickers:
            return {}

        results: Dict[str, str] = {}

        def fetch_one(ticker: str) -> tuple:
            try:
                d = self._client.get_ticker_details(ticker)
                return (ticker, d.name or '')
            except Exception:
                return (ticker, '')

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_one, t): t for t in tickers}
            for future in as_completed(futures):
                ticker, name = future.result()
                if name:
                    results[ticker] = name

        return results

    def _fetch_fundamentals(
        self,
        tickers: List[str],
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """Fetch financial ratios (TTM) in parallel for each ticker."""
        if not tickers:
            return {}

        results: Dict[str, Dict[str, Optional[float]]] = {}

        def fetch_one(ticker: str) -> tuple:
            try:
                ratios = list(self._client.list_financials_ratios(
                    ticker=ticker, limit=1,
                ))
                if not ratios:
                    return (ticker, {})
                r = ratios[0]
                data = {}
                for api_field, col_name in self._FUNDAMENTAL_FIELDS:
                    val = getattr(r, api_field, None)
                    if val is not None:
                        data[col_name] = round(float(val), 2) if col_name not in ('mkt_cap', 'fcf') else val
                    else:
                        data[col_name] = None
                return (ticker, data)
            except Exception as e:
                logger.debug('fundamentals fetch failed: %s: %s', ticker, e)
                return (ticker, {})

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_one, t): t for t in tickers}
            for future in as_completed(futures):
                ticker, data = future.result()
                if data:
                    results[ticker] = data

        return results

    # ------------------------------------------------------------------
    # News fetching (parallel)
    # ------------------------------------------------------------------

    def _fetch_news(
        self,
        tickers: List[str],
    ) -> Dict[str, Dict[str, Optional[str]]]:
        """Fetch latest news headline + sentiment in parallel for each ticker."""
        if not tickers:
            return {}

        results: Dict[str, Dict[str, Optional[str]]] = {}

        def fetch_one(ticker: str) -> tuple:
            try:
                articles = list(self._client.list_ticker_news(
                    ticker=ticker, limit=1,
                ))
                if not articles:
                    return (ticker, {})
                a = articles[0]
                # Extract sentiment for this specific ticker from insights
                sentiment = ''
                sentiment_reason = ''
                if a.insights:
                    for i in a.insights:
                        if getattr(i, 'ticker', '') == ticker:
                            sentiment = getattr(i, 'sentiment', '') or ''
                            sentiment_reason = getattr(i, 'sentiment_reasoning', '') or ''
                            break
                    # Deliberately NO fallback to a.insights[0]: that would attribute
                    # ANOTHER ticker's sentiment (a co-mentioned symbol in the same
                    # article) to this one — confidently-wrong data. If there's no
                    # insight for this ticker, sentiment stays empty (unknown).
                title = a.title or ''
                # Truncate long titles
                if len(title) > 120:
                    title = title[:117] + '...'
                # Truncate long sentiment reasons
                if len(sentiment_reason) > 200:
                    sentiment_reason = sentiment_reason[:197] + '...'
                published = (getattr(a, 'published_utc', '') or '')[:10]
                return (ticker, {
                    'headline': title,
                    'news_date': published,
                    'sentiment': sentiment,
                    'catalyst': sentiment_reason,
                })
            except Exception as e:
                logger.debug('news fetch failed: %s: %s', ticker, e)
                return (ticker, {})

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_one, t): t for t in tickers}
            for future in as_completed(futures):
                ticker, data = future.result()
                if data:
                    results[ticker] = data

        return results
