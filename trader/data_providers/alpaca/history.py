"""Historical bars from Alpaca (SIP feed, split-adjusted, completed sessions only)."""

import datetime as dt
from typing import Callable

import pandas as pd
import pytz

from trader.common.logging_helper import setup_logging
from trader.data_providers.alpaca.sessions import ET, last_completed_session_end
from trader.data_providers.alpaca.timeframes import to_alpaca_timeframe
from trader.data_providers.capabilities import HISTORY_COLUMNS
from trader.data_providers.symbols import to_alpaca_symbol
from trader.objects import BarSize, WhatToShow

logging = setup_logging(module_name='alpaca_history')

BARS_PATH = '/v2/stocks/bars'
PAGE_LIMIT = 10000


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AlpacaHistoryProvider:
    def __init__(self, client, now: Callable[[], dt.datetime] = _utc_now):
        self._client = client
        self._now = now

    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        symbol = to_alpaca_symbol(ticker)
        start = ET.localize(dt.datetime.combine(_calendar_day(start_date), dt.time(0, 0)))
        requested_end = ET.localize(dt.datetime.combine(_calendar_day(end_date), dt.time(23, 59, 59)))
        end = min(requested_end, last_completed_session_end(self._now()))
        if end < requested_end:
            logging.info('alpaca {}: end cut from {} to {} (only completed sessions are fetched)'.format(
                symbol, requested_end.isoformat(), end.isoformat()))
        if end <= start:
            return pd.DataFrame()

        params = {
            'symbols': symbol,
            'timeframe': to_alpaca_timeframe(bar_size),
            'start': _rfc3339_utc(start),
            'end': _rfc3339_utc(end),
            'feed': 'sip',
            'adjustment': 'split',
            'limit': PAGE_LIMIT,
            'sort': 'asc',
        }
        bars = [bar for page in self._client.paginate(BARS_PATH, params)
                for bar in (page.get('bars') or {}).get(symbol, [])]
        if not bars:
            logging.info('alpaca {}: no bars from {} to {}'.format(symbol, params['start'], params['end']))
            return pd.DataFrame()
        return _to_frame(bars, bar_size, timezone)


def _calendar_day(moment: dt.date) -> dt.date:
    # A datetime is also a date, so it must be checked first.
    return moment.date() if isinstance(moment, dt.datetime) else moment


def _rfc3339_utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _to_frame(bars: list[dict], bar_size: BarSize, timezone: str) -> pd.DataFrame:
    frame = pd.DataFrame({
        'date': pd.to_datetime([bar['t'] for bar in bars], utc=True).tz_convert(pytz.timezone(timezone)),
        'open': [bar['o'] for bar in bars],
        'high': [bar['h'] for bar in bars],
        'low': [bar['l'] for bar in bars],
        'close': [bar['c'] for bar in bars],
        'volume': [bar['v'] for bar in bars],
        'average': [bar.get('vw') for bar in bars],
        'bar_count': [bar.get('n') for bar in bars],
        'bar_size': str(bar_size),
        'what_to_show': int(WhatToShow.TRADES),
    }).set_index('date')
    frame = frame[~frame.index.duplicated(keep='last')].sort_index()
    return frame[list(HISTORY_COLUMNS)]
