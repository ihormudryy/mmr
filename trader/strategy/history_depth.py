"""How many days of history a live strategy needs before it may see bars (issue #119).

A strategy declares its warm-up as the upper-case tunable ``MIN_BARS``: the number of
bars it needs before it can signal. The runtime turns that into calendar days to load
for the strategy's bar size and refuses to call the strategy while it has fewer bars.
A strategy without ``MIN_BARS`` keeps the configured depth and is never held back.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trader.bar_size import BarSize

WARMUP_ATTRIBUTE = 'MIN_BARS'
HISTORY_BELOW_WARMUP = 'HISTORY_BELOW_WARMUP'
HISTORY_DEPTH_CAPPED = 'HISTORY_DEPTH_CAPPED'

# The shortest regular session among the markets MMR trades (TSE, 5 h). Assuming a short
# session over-asks for longer ones (US 6.5 h, extended hours), never under-asks.
SESSION_MINUTES = 300
CALENDAR_DAYS_PER_SESSION = 1.5      # weekends (7/5) plus exchange holidays
HOLIDAY_MARGIN_DAYS = 4              # a long weekend before the first session
SESSIONS_PER_WEEK_BAR = 5
SESSIONS_PER_MONTH_BAR = 23

# IB serves bars of 30 seconds or less only for the last six months ("Historical Data
# Limitations" in the TWS API docs). Other bar sizes have no IB age limit; MMR bounds them
# so one strategy cannot turn a restart into hours of paced history requests.
IB_SMALL_BAR_MAX_DAYS = 180
INTRADAY_MAX_DAYS = 365
DAILY_MAX_DAYS = 3650

_BAR_SECONDS = {
    BarSize.Secs1: 1, BarSize.Secs5: 5, BarSize.Secs10: 10, BarSize.Secs15: 15, BarSize.Secs30: 30,
    BarSize.Mins1: 60, BarSize.Mins2: 120, BarSize.Mins3: 180, BarSize.Mins5: 300, BarSize.Mins10: 600,
    BarSize.Mins15: 900, BarSize.Mins20: 1200, BarSize.Mins30: 1800, BarSize.Hours1: 3600,
    BarSize.Hours2: 7200, BarSize.Hours3: 10800, BarSize.Hours4: 14400, BarSize.Hours8: 28800,
}


# The span of one IB history request per bar size (IBHistoryWorker walks the window in these steps):
# IB duration string and its length in days. Bigger spans mean fewer requests before IB paces.
IB_REQUEST_SPANS = {
    BarSize.Mins1: ('1 W', 7),
    BarSize.Mins5: ('1 M', 30),
    BarSize.Mins15: ('1 M', 30),
    BarSize.Hours1: ('4 Y', 1460),
    BarSize.Hours2: ('1 Y', 365),
    BarSize.Days1: ('10 Y', 3650),
}
DEFAULT_IB_REQUEST_SPAN = ('86400 S', 1)
_IB_REQUEST_SPANS_BY_NAME = {str(size): span for size, span in IB_REQUEST_SPANS.items()}


class InvalidWarmupDeclaration(ValueError):
    """``MIN_BARS`` is set but is not a positive whole number of bars."""


@dataclass(frozen=True)
class HistoryDepth:
    days: int             # to fetch: the configured depth, raised to warmup_days
    warmup_bars: int      # MIN_BARS; 0 when the strategy declares none
    warmup_days: int      # calendar days that hold warmup_bars, capped
    capped: bool          # the warm-up asked for more days than the bar size's cap
    cap_days: int


def declared_warmup_bars(strategy: Any) -> int:
    """The strategy's ``MIN_BARS`` (after param overrides); 0 when it declares none."""
    value = getattr(strategy, WARMUP_ATTRIBUTE, None)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidWarmupDeclaration(
            f'{WARMUP_ATTRIBUTE} must be a positive whole number of bars, got {value!r}')
    return value


def sessions_for_bars(bar_size: BarSize, bars: int) -> int:
    if bars <= 0:
        return 0
    if bar_size == BarSize.Days1:
        return bars
    if bar_size == BarSize.Weeks1:
        return bars * SESSIONS_PER_WEEK_BAR
    if bar_size == BarSize.Months1:
        return bars * SESSIONS_PER_MONTH_BAR
    bars_per_session = max(1, SESSION_MINUTES * 60 // _BAR_SECONDS[bar_size])
    return math.ceil(bars / bars_per_session)


def calendar_days_for_sessions(sessions: int) -> int:
    if sessions <= 0:
        return 0
    return math.ceil(sessions * CALENDAR_DAYS_PER_SESSION) + HOLIDAY_MARGIN_DAYS


def max_history_days(bar_size: BarSize) -> int:
    if bar_size <= BarSize.Secs30:
        return IB_SMALL_BAR_MAX_DAYS
    if bar_size >= BarSize.Days1:
        return DAILY_MAX_DAYS
    return INTRADAY_MAX_DAYS


def history_depth(bar_size: BarSize, warmup_bars: int, configured_days: int) -> HistoryDepth:
    """Days to load: the configured depth, raised to cover the warm-up, the warm-up part capped."""
    cap_days = max_history_days(bar_size)
    uncapped_days = calendar_days_for_sessions(sessions_for_bars(bar_size, warmup_bars))
    warmup_days = min(uncapped_days, cap_days)
    return HistoryDepth(days=max(configured_days, warmup_days), warmup_bars=warmup_bars, warmup_days=warmup_days,
                        capped=uncapped_days > cap_days, cap_days=cap_days)


def ib_request_span(bar_size: BarSize | str) -> tuple[str, int]:
    """Matched by IB bar-size string, so a caller passing '1 day' gets the same span as BarSize.Days1."""
    return _IB_REQUEST_SPANS_BY_NAME.get(str(bar_size), DEFAULT_IB_REQUEST_SPAN)


def ib_requests_for(bar_size: BarSize, days: int) -> int:
    """IB history requests one instrument needs to cover ``days``."""
    return max(1, math.ceil(days / ib_request_span(bar_size)[1]))
