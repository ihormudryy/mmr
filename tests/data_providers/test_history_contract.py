"""Every HistoryProvider must return the same frame shape."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trader.data_providers.capabilities import HISTORY_COLUMNS
from trader.objects import BarSize, WhatToShow


def assert_history_frame(df: pd.DataFrame, bar_size: BarSize) -> None:
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == 'date'
    assert df.index.tz is not None
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates
    assert tuple(df.columns) == HISTORY_COLUMNS
    assert set(df['bar_size']) == {str(bar_size)}
    assert set(df['what_to_show']) == {int(WhatToShow.TRADES)}


def test_massive_history_frame_matches_contract():
    from trader.listeners.massive_history import MassiveHistoryWorker
    agg = SimpleNamespace(timestamp=1717250400000, open=1.0, high=2.0, low=0.5,
                          close=1.5, volume=100.0, vwap=1.2, transactions=7)
    with patch('trader.listeners.massive_history.RESTClient') as client_cls:
        client_cls.return_value.list_aggs.return_value = [agg]
        worker = MassiveHistoryWorker(massive_api_key='k')
        df = worker.get_history('AAPL', BarSize.Mins1, dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1))
    assert_history_frame(df, BarSize.Mins1)


def test_twelvedata_history_frame_matches_contract():
    from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
    raw = pd.DataFrame(
        {'open': [1.0], 'high': [2.0], 'low': [0.5], 'close': [1.5], 'volume': [100]},
        index=pd.DatetimeIndex([pd.Timestamp('2024-06-03 09:30')], name='datetime'),
    )
    with patch('trader.listeners.twelvedata_history.TDClient') as client_cls:
        client_cls.return_value.time_series.return_value.as_pandas.return_value = raw
        worker = TwelveDataHistoryWorker(twelvedata_api_key='k')
        # Two-day window: TwelveDataHistoryWorker's intraday chunk loop returns
        # nothing when start == end (pre-existing quirk, see Known findings).
        df = worker.get_history('AAPL', BarSize.Mins1, dt.datetime(2024, 6, 3), dt.datetime(2024, 6, 4))
    assert_history_frame(df, BarSize.Mins1)
