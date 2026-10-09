"""Regular-session bars for research: what an evaluation reads, checks and backtests.

The Alpaca SIP refresh stores pre- and post-market bars as well. The store keeps them; the evaluation drops them
here so the sealed dataset, the pre-claim check and the backtest all see the same regular-session bars.
"""
from __future__ import annotations

from typing import Any

import exchange_calendars as xcals
import pandas as pd

from trader.objects import BarSize

_DAILY_OR_LONGER = ("day", "week", "month")


def is_intraday(bar_size: Any) -> bool:
    return not any(unit in str(bar_size) for unit in _DAILY_OR_LONGER)


def regular_session_bars(frame: pd.DataFrame, calendar_name: str) -> pd.DataFrame:
    """The rows whose bar starts inside a regular session: open <= start < close (early closes included).

    A naive index is read as UTC. The kept rows are returned unchanged.
    """
    if frame is None or frame.empty:
        return frame
    calendar = xcals.get_calendar(calendar_name)
    index = pd.DatetimeIndex(frame.index)
    utc = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    session_labels = utc.tz_convert(calendar.tz).normalize().tz_localize(None)
    opens = pd.DatetimeIndex(calendar.schedule["open"].reindex(session_labels))
    closes = pd.DatetimeIndex(calendar.schedule["close"].reindex(session_labels))
    inside = (utc >= opens) & (utc < closes)           # a non-session day has no open: NaT compares False
    return frame[inside]


class RegularSessionTickData:
    """A TickData whose reads return only regular-session bars; everything else is the wrapped object's."""

    def __init__(self, tickdata: Any, calendar_name: str):
        self._tickdata, self._calendar_name = tickdata, calendar_name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tickdata, name)

    def read(self, *args, **kwargs) -> pd.DataFrame:
        return regular_session_bars(self._tickdata.read(*args, **kwargs), self._calendar_name)

    def get_data(self, *args, **kwargs) -> pd.DataFrame:
        return regular_session_bars(self._tickdata.get_data(*args, **kwargs), self._calendar_name)

    def history(self, *args, **kwargs) -> pd.DataFrame:
        return regular_session_bars(self._tickdata.history(*args, **kwargs), self._calendar_name)


class RegularSessionStorage:
    """A TickStorage whose intraday libraries read only regular-session bars; daily bars pass through."""

    def __init__(self, storage: Any, calendar_name: str):
        self._storage, self._calendar_name = storage, calendar_name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def get_tickdata(self, bar_size: BarSize) -> Any:
        tickdata = self._storage.get_tickdata(bar_size)
        return RegularSessionTickData(tickdata, self._calendar_name) if is_intraday(bar_size) else tickdata
