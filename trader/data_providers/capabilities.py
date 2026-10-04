"""What a market-data provider can do, as structural interfaces."""

import datetime as dt
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd

from trader.objects import BarSize


class Capability(str, Enum):
    HISTORY = 'history'


HISTORY_COLUMNS: tuple[str, ...] = (
    'open', 'high', 'low', 'close', 'volume', 'average', 'bar_count', 'bar_size', 'what_to_show',
)


@runtime_checkable
class HistoryProvider(Protocol):
    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        """Bars for whole days start_date..end_date inclusive, indexed by tz-aware `date`."""
        ...
