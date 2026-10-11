"""#152: the startup history window covers completed trading sessions, not only calendar days."""
import datetime as dt

import exchange_calendars
import pytest

from trader.strategy.strategy_runtime import StrategyRuntime

NYSE = exchange_calendars.get_calendar("XNYS")
TZ = "America/New_York"


def _utc(year, month, day, hour):
    return dt.datetime(year, month, day, hour, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("now, days, first_day", [
    (_utc(2026, 10, 11, 18), 1, dt.date(2026, 10, 9)),    # Sunday: Friday's session
    (_utc(2026, 10, 12, 13), 1, dt.date(2026, 10, 9)),    # Monday before the open: Friday
    (_utc(2026, 10, 13, 18), 1, dt.date(2026, 10, 12)),   # Tuesday: Monday, same as calendar days
    (_utc(2026, 10, 11, 18), 5, dt.date(2026, 10, 5)),    # Sunday, 5 sessions: Mon 5th .. Fri 9th
])
def test_the_window_starts_at_the_first_needed_session(now, days, first_day):
    start = StrategyRuntime._history_window_start(now, days, NYSE, TZ)
    assert start.date() == first_day
    assert (start.hour, start.minute) == (0, 0)


def test_the_window_is_never_shorter_than_the_calendar_days():
    now = _utc(2026, 10, 13, 18)
    start = StrategyRuntime._history_window_start(now, 10, NYSE, TZ)
    assert start.date() <= dt.date(2026, 10, 3)


def test_without_a_calendar_the_window_counts_calendar_days():
    start = StrategyRuntime._history_window_start(_utc(2026, 10, 11, 18), 1, None, TZ)
    assert start.date() == dt.date(2026, 10, 10)


def test_a_date_outside_the_calendar_falls_back_to_calendar_days():
    start = StrategyRuntime._history_window_start(_utc(2099, 6, 1, 18), 1, NYSE, TZ)
    assert start.date() == dt.date(2099, 5, 31)
