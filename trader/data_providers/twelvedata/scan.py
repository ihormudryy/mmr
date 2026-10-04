"""TwelveData scan source: movers/quote discovery, local indicators from time_series, statistics."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import pandas as pd

from trader.data_providers.capabilities import Discovery
from trader.tools.idea_scanner import (
    LIQUID_US_FALLBACK_TICKERS, IdeaScannerError, compute_ema, compute_rsi, compute_sma,
    entitlement_fallback_notice, is_data_entitlement_error,
)

logger = logging.getLogger(__name__)


class TwelveDataScanSource:
    """TwelveData-backed scan source for US equities.

    Full-market scans ("use_market_scan" presets) require tickers or a
    universe here: TwelveData has no bulk snapshot endpoint, and /market_movers
    needs a Pro+ plan. News is not supported by TwelveData's Python client.
    """

    name = 'twelvedata'
    supports_fundamentals = True

    # TwelveData statistics → scanner canonical field-name map.
    # Source path (flattened from get_statistics) → destination column.
    # NOTE: TwelveData's top-level group is "valuations_metrics" (with the
    # 's'). Getting this wrong silently nulls every valuation column — we
    # caught that in testing and it's worth calling out here so a refactor
    # doesn't re-break it.
    _TD_FUNDAMENTAL_FIELDS: List[tuple] = [
        ('valuations_metrics.trailing_pe', 'pe_ratio'),
        ('valuations_metrics.price_to_book_mrq', 'pb_ratio'),
        ('valuations_metrics.price_to_sales_ttm', 'ps_ratio'),
        ('valuations_metrics.enterprise_to_ebitda', 'ev_ebitda'),
        ('valuations_metrics.market_capitalization', 'mkt_cap'),
        ('financials.return_on_equity_ttm', 'roe'),
        ('financials.return_on_assets_ttm', 'roa'),
        ('financials.income_statement.diluted_eps_ttm', 'eps'),
        ('financials.cash_flow.levered_free_cash_flow_ttm', 'fcf'),
        ('financials.balance_sheet.total_debt_to_equity_mrq', 'debt_equity'),
        ('dividends_and_splits.forward_annual_dividend_yield', 'div_yield'),
    ]

    def __init__(self, td_client):
        self._client = td_client

    def discover(self, source, tickers, universe_symbols, use_market_scan) -> Discovery:
        notice = ''
        try:
            quotes = self._discover(source, tickers, universe_symbols, None)
        except IdeaScannerError as ex:
            # Movers (default) Pro+-gated; quotes still work on Basic/Starter.
            if source in ('tickers', 'universe') or not is_data_entitlement_error(ex):
                raise
            notice = entitlement_fallback_notice('twelvedata', str(ex))
            logger.warning(notice)
            quotes = self._batch_quote(list(LIQUID_US_FALLBACK_TICKERS))
        return Discovery(self._build_candidates(quotes) if quotes else [], notice)

    def indicators(self, tickers, needed):
        return self._fetch_indicators(tickers, needed)

    def names(self, tickers):
        return {}  # names come with the quote payload (candidate['name'])

    def fundamentals(self, tickers):
        return self._fetch_fundamentals(tickers)

    def news(self, tickers):
        return {}  # TwelveData has no news endpoint

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover(
        self,
        source: str,
        tickers: Optional[List[str]],
        universe_symbols: Optional[List[str]],
        scan_preset: Any,
    ) -> List[Dict[str, Any]]:
        """Return a list of TwelveData quote dicts for candidate discovery."""
        if source in ('tickers', 'universe'):
            syms = tickers if source == 'tickers' else universe_symbols
            if not syms:
                return []
            return self._batch_quote(syms)

        # Default: movers. TwelveData's get_market_movers returns a compact
        # record that matches a quote for scanner purposes — we normalize it
        # into the same shape. Per MMR's "fail loudly" principle, we tolerate
        # *one* direction failing (unlikely but plausible — rate-limit on the
        # exact moment of one of two calls) but raise if BOTH fail so the
        # caller sees an auth/rate-limit/outage issue rather than an empty
        # result that looks like "no movers today."
        combined: List[Dict[str, Any]] = []
        errors: List[Exception] = []
        for direction in ('gainers', 'losers'):
            try:
                payload = self._client.get_market_movers(
                    market='stocks', direction=direction,
                ).as_json()
            except Exception as ex:
                logger.warning('td movers %s fetch failed: %s', direction, ex)
                errors.append(ex)
                continue
            entries = payload if isinstance(payload, list) else (payload or {}).get('values', [])
            for e in entries:
                combined.append({
                    'symbol': e.get('symbol'),
                    'name': e.get('name'),
                    'close': e.get('last'),
                    'high': e.get('high'),
                    'low': e.get('low'),
                    'open': e.get('open'),
                    'previous_close': e.get('previous_close'),
                    'volume': e.get('volume'),
                    'average_volume': e.get('average_volume'),
                    'change': e.get('change'),
                    'percent_change': e.get('percent_change'),
                })
        if errors and not combined:
            detail = str(errors[0])
            hint = ''
            low = detail.lower()
            if '403' in detail or 'pro or ultra' in low or 'exclusively with' in low:
                hint = (
                    ' TwelveData /market_movers requires a Pro+ plan. '
                    'Use `ideas --tickers AAPL MSFT NVDA` (quotes work on Basic), '
                    'or upgrade at https://twelvedata.com/pricing.'
                )
            raise IdeaScannerError(
                f'TwelveData movers discovery failed for all directions: '
                f'{errors[0]}.{hint}'
            ) from errors[0]
        return combined

    def _batch_quote(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """Fetch a batch of quotes. TwelveData supports comma-joined symbols
        on /quote and returns a dict keyed by symbol. We re-shape to a list."""
        # Dedupe while preserving order
        seen = set()
        unique = []
        for s in symbols:
            if s and s not in seen:
                unique.append(s); seen.add(s)
        if not unique:
            return []

        # TwelveData /quote accepts up to 120 symbols per call on most plans.
        # Chunk to be safe.
        out: List[Dict[str, Any]] = []
        CHUNK = 100
        for i in range(0, len(unique), CHUNK):
            batch = unique[i:i + CHUNK]
            try:
                payload = self._client.quote(symbol=','.join(batch)).as_json()
            except Exception as ex:
                logger.warning('td batch quote failed (%s...): %s', batch[:3], ex)
                continue
            if isinstance(payload, dict) and 'symbol' in payload:
                # Single-symbol response
                out.append(payload)
            elif isinstance(payload, dict):
                for sym, item in payload.items():
                    if isinstance(item, dict):
                        # Some plans return {"code":..., "message":...} on error for a single
                        # symbol — skip those so the rest of the batch still lands.
                        if 'symbol' in item or 'close' in item:
                            out.append(item)
            elif isinstance(payload, list):
                out.extend(payload)
        return out

    # ------------------------------------------------------------------
    # Candidate building
    # ------------------------------------------------------------------

    def _build_candidates(self, quotes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = []
        seen = set()
        for q in quotes:
            ticker = q.get('symbol', '') or ''
            if not ticker or ticker in seen:
                continue
            # Skip warrants, units, rights, preferred stocks — mirror the
            # Massive/IB scanners so a `--source` switch doesn't change
            # which tickers get surfaced (e.g. KKRpD is preferred stock;
            # 'p' lowercase inside an otherwise-uppercase ticker is the
            # convention across data providers).
            if any(ticker.endswith(s) for s in ('W', 'WS', '.U', '.R')):
                continue
            if 'p' in ticker and ticker != ticker.upper():
                continue
            seen.add(ticker)

            def _f(key, default=0.0):
                v = q.get(key)
                try:
                    return float(v) if v not in (None, '') else default
                except (TypeError, ValueError):
                    return default

            def _i(key, default=0):
                v = q.get(key)
                try:
                    return int(float(v)) if v not in (None, '') else default
                except (TypeError, ValueError):
                    return default

            price = _f('close')
            if price <= 0:
                continue
            volume = _i('volume')
            day_open = _f('open')
            day_high = _f('high')
            day_low = _f('low')
            prev_close = _f('previous_close')
            avg_volume = _i('average_volume')

            change_pct = _f('percent_change')
            gap_pct = 0.0
            if prev_close > 0 and day_open > 0:
                gap_pct = ((day_open - prev_close) / prev_close) * 100.0
            rel_vol = 0.0
            if avg_volume > 0 and volume > 0:
                rel_vol = volume / avg_volume
            range_pct = 0.0
            if day_low > 0 and day_high > day_low:
                range_pct = ((day_high - day_low) / day_low) * 100.0

            candidates.append({
                'ticker': ticker,
                'name': q.get('name', '') or '',
                'price': round(price, 2),
                'change_pct': round(change_pct, 2),
                'volume': volume,
                'gap_pct': round(gap_pct, 2),
                'rel_vol': round(rel_vol, 2),
                'range_pct': round(range_pct, 2),
                'spread_pct': 0.0,  # not in TD quote
                'vwap': 0.0,
            })
        return candidates

    # ------------------------------------------------------------------
    # Indicators — local compute from one time_series call per ticker
    # ------------------------------------------------------------------

    def _fetch_indicators(
        self,
        tickers: List[str],
        needed: List[str],
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """Fetch 50 daily bars per ticker in parallel and compute indicators
        locally. Cheaper than one call per (ticker, indicator)."""
        if not needed or not tickers:
            return {}

        results: Dict[str, Dict[str, Optional[float]]] = {t: {} for t in tickers}

        def fetch_one(ticker: str):
            try:
                ts = self._client.time_series(
                    symbol=ticker, interval='1day', outputsize=60,
                )
                df = ts.as_pandas()
                if df is None or df.empty:
                    return ticker, {}
                # TwelveData returns newest-first by default. RSI/EMA/SMA
                # are position-sensitive — they assume chronological order,
                # so sort by index (timestamp) ascending before extracting
                # closes. Sorting closes by value would silently produce
                # wrong indicator values.
                df = df.sort_index(ascending=True)
                closes = list(pd.to_numeric(df['close'], errors='coerce').dropna())
                vals: Dict[str, Optional[float]] = {}
                for indicator in needed:
                    if indicator == 'rsi':
                        vals['rsi'] = compute_rsi(closes, period=14)
                    elif indicator == 'ema_9':
                        vals['ema_9'] = compute_ema(closes, window=9)
                    elif indicator == 'sma_20':
                        vals['sma_20'] = compute_sma(closes, window=20)
                    elif indicator == 'sma_50':
                        vals['sma_50'] = compute_sma(closes, window=50)
                return ticker, vals
            except Exception as ex:
                logger.debug('td indicator fetch failed for %s: %s', ticker, ex)
                return ticker, {}

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_one, t): t for t in tickers}
            for fut in as_completed(futures):
                ticker, vals = fut.result()
                results[ticker].update(vals)
        return results

    # ------------------------------------------------------------------
    # Fundamentals — get_statistics → canonical fields
    # ------------------------------------------------------------------

    def _fetch_fundamentals(
        self,
        tickers: List[str],
    ) -> Dict[str, Dict[str, Optional[float]]]:
        # TwelveData's get_statistics call is ~100 credits each. On a Grow
        # plan (610 credits/min) that means a burst of 6-7 tickers exhausts
        # the budget for the current minute. We:
        #   1) Keep max_workers low (2) to spread requests out
        #   2) Short-circuit the remaining fetches once we see a rate-limit
        #      signal — so the scan completes with whatever fundamentals
        #      landed rather than raising mid-flight
        #   3) Warn once when rate-limiting happens so the user knows their
        #      fundamentals coverage is partial
        if not tickers:
            return {}

        import threading
        rate_limit_hit = threading.Event()
        results: Dict[str, Dict[str, Optional[float]]] = {}

        def fetch_one(ticker: str):
            if rate_limit_hit.is_set():
                return ticker, {}
            try:
                payload = self._client.get_statistics(symbol=ticker).as_json()
                stats = (payload or {}).get('statistics') or {}
                flat = _flatten(stats)
                out: Dict[str, Optional[float]] = {}
                for td_path, col in TwelveDataScanSource._TD_FUNDAMENTAL_FIELDS:
                    v = flat.get(td_path)
                    if v is None:
                        out[col] = None
                        continue
                    try:
                        out[col] = round(float(v), 4) if col not in ('mkt_cap', 'fcf') else float(v)
                    except (TypeError, ValueError):
                        out[col] = None
                return ticker, out
            except Exception as ex:
                msg = str(ex).lower()
                if 'api credits' in msg or 'rate limit' in msg or 'too many requests' in msg:
                    if not rate_limit_hit.is_set():
                        logger.warning(
                            'td fundamentals hit rate limit on %s — remaining tickers '
                            'in this scan will skip fundamentals. Wait ~60s and retry, '
                            'or upgrade TwelveData plan credits/min.', ticker)
                        rate_limit_hit.set()
                else:
                    logger.debug('td fundamentals fetch failed for %s: %s', ticker, ex)
                return ticker, {}

        # max_workers intentionally small — each get_statistics is ~100
        # credits; bursting 5 in parallel is 500 credits in a fraction of a
        # second, which crowds the 610/min Grow-plan budget.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(fetch_one, t): t for t in tickers}
            for fut in as_completed(futures):
                ticker, data = fut.result()
                if data:
                    results[ticker] = data
        return results


def _flatten(d: Dict[str, Any], prefix: str = '', sep: str = '.') -> Dict[str, Any]:
    """Module-private flatten helper — keeps ``TwelveDataScanSource``
    self-contained without an SDK import. Same semantics as
    :meth:`trader.sdk.MMR._flatten_td_dict`."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        full = f'{prefix}{sep}{k}' if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full, sep))
        else:
            out[full] = v
    return out
