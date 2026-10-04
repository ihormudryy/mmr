"""Which NYSE session is fully available on Alpaca's free (Basic) plan.

Basic blocks SIP data newer than ~15 minutes, and post-market runs to
20:00 ET. Writing a half-finished session would make TickData.missing()
treat the day as present and never backfill it, so only completed sessions
are fetched.
"""

import datetime as dt

import exchange_calendars
import pandas as pd
import pytz

ET = pytz.timezone('US/Eastern')
SESSION_END_ET = dt.time(20, 0)
SESSION_COMPLETE_ET = dt.time(20, 16)
_LOOKBACK_DAYS = 14


def last_completed_session_end(now: dt.datetime, calendar=None) -> dt.datetime:
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    calendar = calendar or exchange_calendars.get_calendar('XNYS')
    now_et = now.astimezone(ET)
    today = now_et.date()
    sessions = calendar.sessions_in_range(
        pd.Timestamp(today - dt.timedelta(days=_LOOKBACK_DAYS)), pd.Timestamp(today)
    )
    for session in reversed(sessions):
        day = session.date()
        if day < today or now_et.time() >= SESSION_COMPLETE_ET:
            return ET.localize(dt.datetime.combine(day, SESSION_END_ET))
    raise ValueError(f'no NYSE session in the {_LOOKBACK_DAYS} days before {today}')
