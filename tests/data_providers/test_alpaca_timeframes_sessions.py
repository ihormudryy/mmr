import datetime as dt

import pytest
import pytz

from trader.data_providers.alpaca.sessions import last_completed_session_end
from trader.data_providers.alpaca.timeframes import to_alpaca_timeframe
from trader.objects import BarSize

ET = pytz.timezone('US/Eastern')


def _et(*args):
    return ET.localize(dt.datetime(*args))


@pytest.mark.parametrize('bar_size, expected', [
    (BarSize.Mins1, '1Min'), (BarSize.Mins2, '2Min'), (BarSize.Mins3, '3Min'),
    (BarSize.Mins5, '5Min'), (BarSize.Mins10, '10Min'), (BarSize.Mins15, '15Min'),
    (BarSize.Mins20, '20Min'), (BarSize.Mins30, '30Min'), (BarSize.Hours1, '1Hour'),
    (BarSize.Hours2, '2Hour'), (BarSize.Hours3, '3Hour'), (BarSize.Hours4, '4Hour'),
    (BarSize.Hours8, '8Hour'), (BarSize.Days1, '1Day'), (BarSize.Weeks1, '1Week'),
    (BarSize.Months1, '1Month'),
])
def test_timeframe_mapping(bar_size, expected):
    assert to_alpaca_timeframe(bar_size) == expected


@pytest.mark.parametrize('bar_size', [BarSize.Secs1, BarSize.Secs5, BarSize.Secs10,
                                      BarSize.Secs15, BarSize.Secs30])
def test_seconds_bars_are_rejected_with_alternative(bar_size):
    with pytest.raises(ValueError, match='use --source ib or massive'):
        to_alpaca_timeframe(bar_size)


def test_mid_session_returns_previous_session():
    # Thursday 2026-10-01 11:00 ET -> Wednesday 2026-09-30 20:00 ET
    assert last_completed_session_end(_et(2026, 10, 1, 11, 0)) == _et(2026, 9, 30, 20, 0)


def test_just_before_cutoff_returns_previous_session():
    assert last_completed_session_end(_et(2026, 10, 1, 20, 15)) == _et(2026, 9, 30, 20, 0)


def test_after_cutoff_returns_today():
    assert last_completed_session_end(_et(2026, 10, 1, 20, 16)) == _et(2026, 10, 1, 20, 0)


def test_weekend_uses_friday():
    # Sunday 2026-10-04 -> Friday 2026-10-02
    assert last_completed_session_end(_et(2026, 10, 4, 9, 0)) == _et(2026, 10, 2, 20, 0)


def test_holiday_uses_previous_session():
    # Thanksgiving Thursday 2026-11-26 is closed -> Wednesday 2026-11-25
    assert last_completed_session_end(_et(2026, 11, 26, 22, 0)) == _et(2026, 11, 25, 20, 0)


def test_utc_input_is_converted():
    now_utc = dt.datetime(2026, 10, 2, 0, 30, tzinfo=dt.timezone.utc)  # 2026-10-01 20:30 ET
    assert last_completed_session_end(now_utc) == _et(2026, 10, 1, 20, 0)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match='timezone-aware'):
        last_completed_session_end(dt.datetime(2026, 10, 1, 12, 0))
