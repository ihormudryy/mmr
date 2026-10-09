"""BarSize: the IB bar sizes. Standard library only, so research and ai code can use it without ib_async."""
from enum import IntEnum
from typing import Tuple


class BarSize(IntEnum):
    Secs1 = 0
    Secs5 = 1
    Secs10 = 2
    Secs15 = 3
    Secs30 = 4
    Mins1 = 5
    Mins2 = 6
    Mins3 = 7
    Mins5 = 8
    Mins10 = 9
    Mins15 = 10
    Mins20 = 11
    Mins30 = 12
    Hours1 = 13
    Hours2 = 14
    Hours3 = 15
    Hours4 = 16
    Hours8 = 17
    Days1 = 18
    Weeks1 = 19
    Months1 = 20

    @staticmethod
    def bar_sizes():
        return [
            '1 secs', '5 secs', '10 secs', '15 secs', '30 secs', '1 min', '2 mins', '3 mins', '5 mins',
            '10 mins', '15 mins', '20 mins', '30 mins', '1 hour', '2 hours', '3 hours', '4 hours', '8 hours',
            '1 day', '1 week', '1 month'
        ]

    @staticmethod
    def parse_str(bar_size_str: str):
        return BarSize(BarSize.bar_sizes().index(bar_size_str))  # type: ignore

    def __str__(self):
        return BarSize.bar_sizes()[int(self.value)]

    @staticmethod
    def to_massive_timespan(bar_size: 'BarSize') -> Tuple[int, str]:
        mapping = {
            BarSize.Secs1: (1, 'second'),
            BarSize.Secs5: (5, 'second'),
            BarSize.Secs10: (10, 'second'),
            BarSize.Secs15: (15, 'second'),
            BarSize.Secs30: (30, 'second'),
            BarSize.Mins1: (1, 'minute'),
            BarSize.Mins2: (2, 'minute'),
            BarSize.Mins3: (3, 'minute'),
            BarSize.Mins5: (5, 'minute'),
            BarSize.Mins10: (10, 'minute'),
            BarSize.Mins15: (15, 'minute'),
            BarSize.Mins20: (20, 'minute'),
            BarSize.Mins30: (30, 'minute'),
            BarSize.Hours1: (1, 'hour'),
            BarSize.Hours2: (2, 'hour'),
            BarSize.Hours3: (3, 'hour'),
            BarSize.Hours4: (4, 'hour'),
            BarSize.Hours8: (8, 'hour'),
            BarSize.Days1: (1, 'day'),
            BarSize.Weeks1: (1, 'week'),
            BarSize.Months1: (1, 'month'),
        }
        if bar_size not in mapping:
            raise ValueError(f'unsupported BarSize for Massive API: {bar_size}')
        return mapping[bar_size]

    @staticmethod
    def to_pandas_freq(bar_size: 'BarSize') -> str:
        """Pandas resample frequency string for a BarSize (e.g. '1min', '1D').

        Used to resample the live per-tick stream into proper OHLCV bars before
        dispatching to bar-based strategies.
        """
        mapping = {
            BarSize.Secs1: '1s', BarSize.Secs5: '5s', BarSize.Secs10: '10s',
            BarSize.Secs15: '15s', BarSize.Secs30: '30s',
            BarSize.Mins1: '1min', BarSize.Mins2: '2min', BarSize.Mins3: '3min',
            BarSize.Mins5: '5min', BarSize.Mins10: '10min', BarSize.Mins15: '15min',
            BarSize.Mins20: '20min', BarSize.Mins30: '30min',
            BarSize.Hours1: '1h', BarSize.Hours2: '2h', BarSize.Hours3: '3h',
            BarSize.Hours4: '4h', BarSize.Hours8: '8h',
            BarSize.Days1: '1D', BarSize.Weeks1: '1W', BarSize.Months1: '1MS',
        }
        if bar_size not in mapping:
            raise ValueError(f'no pandas freq for BarSize: {bar_size}')
        return mapping[bar_size]

    @staticmethod
    def to_twelvedata_interval(bar_size: 'BarSize') -> str:
        # TwelveData interval strings:
        # https://twelvedata.com/docs#time-series
        # Supported: 1min, 5min, 15min, 30min, 45min, 1h, 2h, 4h, 1day, 1week, 1month
        mapping = {
            BarSize.Mins1: '1min',
            BarSize.Mins5: '5min',
            BarSize.Mins15: '15min',
            BarSize.Mins30: '30min',
            BarSize.Hours1: '1h',
            BarSize.Hours2: '2h',
            BarSize.Hours4: '4h',
            BarSize.Days1: '1day',
            BarSize.Weeks1: '1week',
            BarSize.Months1: '1month',
        }
        if bar_size not in mapping:
            raise ValueError(
                f'unsupported BarSize for TwelveData API: {bar_size}. '
                f'Supported: 1 min, 5 mins, 15 mins, 30 mins, 1 hour, 2 hours, 4 hours, '
                f'1 day, 1 week, 1 month.'
            )
        return mapping[bar_size]
