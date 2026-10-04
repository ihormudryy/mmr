"""ECB daily reference rates from Frankfurter (free, no key).

One rate per TARGET business day (about 16:00 CET) — not live quotes. Every call
pins providers=ECB: Frankfurter's unpinned v2 rate blends ~90 central banks and
stamps the blend with today's date, which would mislabel the data.
"""

import datetime as dt
import math
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Sequence

import pandas as pd
import requests

from trader.data_providers.capabilities import (
    FX_RATES_COLUMNS, make_fx_conversion, make_fx_rate, round_significant, sort_fx_rates)
from trader.data_providers.errors import ProviderError
from trader.data_providers.rate_limit import RateLimiter, call_with_retry
from trader.data_providers.symbols import parse_currency, parse_forex_codes
from trader.data_providers.twelvedata.quotes import _to_float

FRANKFURTER_URL = 'https://api.frankfurter.dev'
RATES_PATH = '/v2/rates'
ECB_PROVIDER = 'ECB'
PIVOT_CURRENCY = 'EUR'
LOOKBACK_DAYS = 14
REQUEST_TIMEOUT_SECS = 30
SOURCE = 'frankfurter'
ECB_NOTE = 'ECB daily reference rate, not a live quote'

# Spec §7: polite pacing, shared by every client in this process.
FRANKFURTER_LIMITER = RateLimiter(5, 1.0)


class FrankfurterClient:
    def __init__(self, session: Optional[requests.Session] = None, limiter: Optional[RateLimiter] = None,
                 base_url: str = FRANKFURTER_URL):
        self._session = session or requests.Session()
        self._limiter = limiter or FRANKFURTER_LIMITER
        self._base_url = base_url

    def get_json(self, path: str, params: Mapping[str, Any]) -> Any:
        try:
            response = call_with_retry(lambda: self._send(path, params), provider=SOURCE)
        except requests.RequestException as ex:
            raise ProviderError(f'frankfurter {path} request failed: {ex}') from ex
        if response.status_code in (404, 422):
            raise ValueError(f'frankfurter rejected the request: {_message(response)}')
        if response.status_code >= 400:
            raise ProviderError(f'frankfurter {path} failed: HTTP {response.status_code} {_message(response)}')
        try:
            return response.json()
        except ValueError as ex:
            raise ProviderError(f'frankfurter {path} returned a body that is not JSON') from ex

    def _send(self, path: str, params: Mapping[str, Any]):
        self._limiter.acquire()
        return self._session.get(self._base_url + path, params=dict(params), timeout=REQUEST_TIMEOUT_SECS)


def _message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ''
    return payload.get('message', '') if isinstance(payload, dict) else ''


def percent_change(previous: float, latest: float) -> float:
    return (latest - previous) / previous * 100


def rounded_change(previous: float, latest: float) -> float:
    return round_significant(latest - previous)


class DailyRates(NamedTuple):
    """ECB rates in units per 1 EUR (EUR itself = 1.0) on the two latest complete dates."""
    previous_date: str
    latest_date: str
    previous: dict
    latest: dict

    def pair(self, base: str, quote: str) -> tuple[float, float]:
        """(previous, latest) price of one `base` in `quote`; NaN on a date that lacks either currency."""
        return _cross(self.previous, base, quote), _cross(self.latest, base, quote)


def _cross(rates: Mapping[str, float], base: str, quote: str) -> float:
    if base not in rates or quote not in rates:
        return float('nan')
    # ECB publishes up to 7 significant digits (EUR/IDR 18226.43); 10 keeps all of them
    # exact for EUR pairs and drops the float noise from the division for crosses.
    return round_significant(rates[quote] / rates[base])


class FrankfurterForex:
    def __init__(self, client, today: Callable[[], dt.date] = dt.date.today):
        self._client = client
        self._today = today

    def rate(self, base: str, quote: str) -> dict:
        base, quote = parse_forex_codes(base, quote)
        rates = self.daily_rates({base, quote})
        previous, latest = rates.pair(base, quote)
        return make_fx_rate(base, quote, last=latest, close=latest, previous_close=previous,
                            change=rounded_change(previous, latest), change_pct=percent_change(previous, latest),
                            as_of=rates.latest_date, source=SOURCE, note=ECB_NOTE)

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        base = parse_currency(base)
        if symbols:
            pairs = [parse_forex_codes(base, symbol) for symbol in symbols]
            rates = self.daily_rates({currency for pair in pairs for currency in pair})
        else:
            rates = self.daily_rates()
            if base not in rates.latest or base not in rates.previous:
                raise ValueError(f'ECB publishes no daily rate for {base} (frankfurter)')
            published = sorted(set(rates.previous) | set(rates.latest))
            pairs = [(base, quote) for quote in published if quote != base]
        rows = []
        for pair_base, quote in pairs:
            previous, latest = rates.pair(pair_base, quote)
            rows.append({'pair': f'{pair_base}/{quote}', 'last': latest, 'previous_close': previous,
                         'change': rounded_change(previous, latest), 'change_pct': percent_change(previous, latest),
                         'as_of': rates.latest_date, 'source': SOURCE, 'note': _rates_note(rates, quote)})
        frame = sort_fx_rates(pd.DataFrame(rows, columns=list(FX_RATES_COLUMNS)))
        frame.attrs['as_of'] = rates.latest_date
        return frame

    def convert(self, base: str, quote: str, amount: float) -> dict:
        base, quote = parse_forex_codes(base, quote)
        rates = self.daily_rates({base, quote})
        rate = rates.pair(base, quote)[1]
        return make_fx_conversion(base, quote, amount, converted=amount * rate, rate=rate,
                                  as_of=rates.latest_date, source=SOURCE, note=ECB_NOTE)

    def daily_rates(self, currencies: Optional[Iterable[str]] = None) -> DailyRates:
        """ECB rates on the two latest dates that have every requested currency (None = all)."""
        wanted = None if currencies is None else sorted({parse_currency(c) for c in currencies} - {PIVOT_CURRENCY})
        end = self._today()
        start = end - dt.timedelta(days=LOOKBACK_DAYS)
        params = {'base': PIVOT_CURRENCY, 'providers': ECB_PROVIDER, 'from': start.isoformat(), 'to': end.isoformat()}
        if wanted:
            params['quotes'] = ','.join(wanted)
        by_date = _rates_by_date(self._client.get_json(RATES_PATH, params))
        if not by_date:
            raise ProviderError(f'frankfurter returned no ECB rates between {start} and {end}')
        if wanted:
            published = set().union(*by_date.values())
            missing = [currency for currency in wanted if currency not in published]
            if missing:
                raise ValueError(f"ECB publishes no daily rate for {', '.join(missing)} (frankfurter)")
            dates = [day for day in sorted(by_date) if set(wanted) <= set(by_date[day])]
        else:
            dates = sorted(by_date)
        if len(dates) < 2:
            raise ProviderError(f'frankfurter returned {len(dates)} complete ECB rate date(s) '
                                f'between {start} and {end}; need 2')
        previous_date, latest_date = dates[-2], dates[-1]
        return DailyRates(previous_date, latest_date,
                          {PIVOT_CURRENCY: 1.0, **by_date[previous_date]},
                          {PIVOT_CURRENCY: 1.0, **by_date[latest_date]})


def _rates_note(rates: DailyRates, quote: str) -> str:
    """ECB_NOTE, plus the date a currency was missing so its blank numbers are explained."""
    for day, published in ((rates.latest_date, rates.latest), (rates.previous_date, rates.previous)):
        if quote not in published:
            return f'{ECB_NOTE}; {quote} not published on {day}'
    return ECB_NOTE


def _rates_by_date(rows: Any) -> dict[str, dict[str, float]]:
    if not isinstance(rows, list):
        raise ProviderError(f'frankfurter returned an unexpected payload: {str(rows)[:200]}')
    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        if not (isinstance(row, dict) and {'date', 'base', 'quote', 'rate'} <= set(row)):
            raise ProviderError(f'frankfurter returned an unexpected rate row: {str(row)[:200]}')
        if row['base'] != PIVOT_CURRENCY:
            raise ProviderError(f"frankfurter returned a rate against {row['base']!r}, expected {PIVOT_CURRENCY}")
        if row['quote'] == PIVOT_CURRENCY:
            continue
        rate = _to_float(row['rate'])
        if not (math.isfinite(rate) and rate > 0):
            raise ProviderError(f"frankfurter returned an unusable rate for {row['quote']} on {row['date']}: "
                                f"{row['rate']!r}")
        by_date.setdefault(row['date'], {})[row['quote']] = rate
    return by_date
