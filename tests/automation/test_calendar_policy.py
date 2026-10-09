"""P3 Task 4 — XNYS calendar policy (DST, holidays, early closes, deadlines)."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import pytest

from trader.automation.calendar_policy import XNYSCalendarPolicy

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def _et(year, month, day, hour, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ET)


def test_package_version_is_recorded():
    policy = XNYSCalendarPolicy()
    schedule = policy.resolve(_et(2026, 7, 17, 11, 0))
    assert schedule is not None
    assert schedule.calendar_name == "XNYS"
    assert schedule.calendar_version == xcals.__version__
    assert policy.calendar_version() == xcals.__version__


def test_normal_session_relative_offsets_from_official_close():
    """15:30 / 15:35 / 15:45 / 15:55 are relative to the official close."""
    policy = XNYSCalendarPolicy(opening_stabilization=dt.timedelta(minutes=5))
    schedule = policy.resolve(_et(2026, 7, 17, 11, 0))
    assert schedule is not None
    assert schedule.is_early_close is False

    close_et = schedule.close_utc.astimezone(ET)
    assert close_et.hour == 16 and close_et.minute == 0

    assert schedule.entry_cutoff_utc == schedule.close_utc - dt.timedelta(minutes=30)
    assert schedule.cancel_entries_utc == schedule.close_utc - dt.timedelta(minutes=25)
    assert schedule.flatten_start_utc == schedule.close_utc - dt.timedelta(minutes=15)
    assert schedule.flat_deadline_utc == schedule.close_utc - dt.timedelta(minutes=5)

    assert schedule.entry_cutoff_utc.astimezone(ET).hour == 15
    assert schedule.entry_cutoff_utc.astimezone(ET).minute == 30
    assert schedule.cancel_entries_utc.astimezone(ET).minute == 35
    assert schedule.flatten_start_utc.astimezone(ET).minute == 45
    assert schedule.flat_deadline_utc.astimezone(ET).minute == 55


def test_early_close_uses_same_relative_offsets():
    """Thanksgiving 2025 closes 13:00 ET — offsets shift with the close."""
    policy = XNYSCalendarPolicy(opening_stabilization=dt.timedelta(minutes=5))
    schedule = policy.resolve(_et(2025, 11, 28, 10, 0))
    assert schedule is not None
    assert schedule.is_early_close is True

    close_et = schedule.close_utc.astimezone(ET)
    assert close_et.hour == 13 and close_et.minute == 0

    assert schedule.entry_cutoff_utc == schedule.close_utc - dt.timedelta(minutes=30)
    assert schedule.cancel_entries_utc == schedule.close_utc - dt.timedelta(minutes=25)
    assert schedule.flatten_start_utc == schedule.close_utc - dt.timedelta(minutes=15)
    assert schedule.flat_deadline_utc == schedule.close_utc - dt.timedelta(minutes=5)

    assert schedule.entry_cutoff_utc.astimezone(ET).hour == 12
    assert schedule.entry_cutoff_utc.astimezone(ET).minute == 30


def test_dst_spring_forward_session_open_is_correct():
    """First Monday after US DST spring-forward (2026-03-09 week)."""
    policy = XNYSCalendarPolicy()
    # 2026-03-09 is Monday after DST change on 2026-03-08
    schedule = policy.resolve(_et(2026, 3, 9, 11, 0))
    assert schedule is not None
    open_et = schedule.open_utc.astimezone(ET)
    close_et = schedule.close_utc.astimezone(ET)
    assert open_et.hour == 9 and open_et.minute == 30
    assert close_et.hour == 16 and close_et.minute == 0
    # UTC offset should be EDT (-4)
    assert schedule.open_utc.utcoffset() == dt.timedelta(hours=0)
    assert schedule.open_utc.astimezone(UTC).hour == 13  # 09:30 EDT = 13:30 UTC


def test_dst_fall_back_session_open_is_correct():
    """First Monday after US DST fall-back (2025-11-03 week)."""
    policy = XNYSCalendarPolicy()
    schedule = policy.resolve(_et(2025, 11, 3, 11, 0))
    assert schedule is not None
    open_et = schedule.open_utc.astimezone(ET)
    assert open_et.hour == 9 and open_et.minute == 30
    assert schedule.open_utc.astimezone(UTC).hour == 14  # 09:30 EST = 14:30 UTC


def test_holiday_has_no_session():
    policy = XNYSCalendarPolicy()
    assert policy.resolve(_et(2026, 1, 1, 12, 0)) is None  # New Year's
    assert policy.resolve(_et(2026, 7, 3, 12, 0)) is None  # Independence Day observed
    assert policy.allows_new_entry(_et(2026, 1, 1, 12, 0)) is False


def test_weekend_has_no_session():
    policy = XNYSCalendarPolicy()
    assert policy.resolve(_et(2026, 7, 18, 12, 0)) is None  # Saturday
    assert policy.allows_new_entry(_et(2026, 7, 18, 12, 0)) is False


def test_opening_stabilization_blocks_entries():
    policy = XNYSCalendarPolicy(opening_stabilization=dt.timedelta(minutes=10))
    schedule = policy.resolve(_et(2026, 7, 17, 9, 35))
    assert schedule is not None
    # 09:35 is within 10-minute stabilization after 09:30 open
    assert policy.allows_new_entry(_et(2026, 7, 17, 9, 35), schedule=schedule) is False
    assert policy.allows_new_entry(_et(2026, 7, 17, 9, 41), schedule=schedule) is True


def test_no_new_entries_after_cutoff():
    policy = XNYSCalendarPolicy(opening_stabilization=dt.timedelta(minutes=0))
    schedule = policy.resolve(_et(2026, 7, 17, 15, 29))
    assert schedule is not None
    assert policy.allows_new_entry(_et(2026, 7, 17, 15, 29), schedule=schedule) is True
    assert policy.allows_new_entry(_et(2026, 7, 17, 15, 30), schedule=schedule) is False
    assert policy.allows_new_entry(_et(2026, 7, 17, 15, 45), schedule=schedule) is False


def test_outside_regular_hours_blocked():
    policy = XNYSCalendarPolicy()
    schedule = policy.resolve(_et(2026, 7, 17, 8, 0))
    # resolve may still return the day's schedule from a pre-open timestamp
    # but allows_new_entry must be false before open
    if schedule is not None:
        assert policy.allows_new_entry(_et(2026, 7, 17, 8, 0), schedule=schedule) is False
    assert policy.allows_new_entry(_et(2026, 7, 17, 8, 0)) is False


def test_injected_calendar_is_used():
    """Policy accepts an injected calendar for determinism in tests."""
    cal = xcals.get_calendar("XNYS")
    policy = XNYSCalendarPolicy(calendar=cal, opening_stabilization=dt.timedelta(0))
    schedule = policy.resolve(_et(2026, 7, 17, 12, 0))
    expected_close = cal.session_close(pd.Timestamp("2026-07-17")).to_pydatetime()
    if expected_close.tzinfo is None:
        expected_close = expected_close.replace(tzinfo=UTC)
    assert schedule.close_utc == expected_close.astimezone(UTC)


# --- SP1 Plan 5: session ranges for the scoreboard ------------------------------------------

def test_sessions_in_range_skips_the_thanksgiving_holiday_and_the_weekend():
    policy = XNYSCalendarPolicy()
    assert policy.sessions_in_range(dt.date(2026, 11, 24), dt.date(2026, 11, 30)) == [
        dt.date(2026, 11, 24), dt.date(2026, 11, 25), dt.date(2026, 11, 27), dt.date(2026, 11, 30)]
    assert policy.sessions_in_range(dt.date(2026, 11, 28), dt.date(2026, 11, 29)) == []


def test_sessions_between_is_strict_on_both_ends():
    policy = XNYSCalendarPolicy()
    assert policy.sessions_between(dt.date(2026, 11, 24), dt.date(2026, 11, 30)) == 2   # 25th, 27th
    assert policy.sessions_between(dt.date(2026, 11, 25), dt.date(2026, 11, 27)) == 0


def test_previous_session_is_strictly_before_also_across_a_holiday_and_a_weekend():
    policy = XNYSCalendarPolicy()
    assert policy.previous_session(dt.date(2026, 11, 27)) == dt.date(2026, 11, 25)   # Thanksgiving
    assert policy.previous_session(dt.date(2026, 10, 12)) == dt.date(2026, 10, 9)    # Monday -> Friday
    assert policy.previous_session(dt.date(2026, 10, 11)) == dt.date(2026, 10, 9)    # a Sunday
    assert policy.is_session(dt.date(2026, 11, 26)) is False and policy.is_session(dt.date(2026, 11, 27)) is True


def test_every_method_works_for_a_day_years_after_the_policy_was_built():
    policy = XNYSCalendarPolicy()                       # its own calendar ends about a year from today
    year = dt.date.today().year + 3
    reference = xcals.get_calendar("XNYS", start=f"{year}-01-01", end=f"{year}-12-31")
    session = reference.sessions_in_range(f"{year}-07-07", f"{year}-07-14")[2].date()
    closed_day = next(day for day in (dt.date(year, 7, d) for d in range(1, 31)) if not reference.is_session(day))

    assert policy.is_session(session) is True and policy.is_session(closed_day) is False
    expected_previous = reference.date_to_session(pd.Timestamp(session - dt.timedelta(days=1)), direction="previous")
    assert policy.previous_session(session) == expected_previous.date()
    noon_et = dt.datetime.combine(session, dt.time(12, 0), tzinfo=ZoneInfo("America/New_York"))
    schedule = policy.resolve(noon_et)
    assert schedule is not None and schedule.session_date == session
    assert schedule.close_utc == reference.session_close(pd.Timestamp(session)).to_pydatetime()
    assert policy.resolve(dt.datetime.combine(closed_day, dt.time(12, 0), tzinfo=ZoneInfo("America/New_York"))) is None
