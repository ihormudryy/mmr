"""The fixed shadow tracking window of one judgment (SP2c spec 7). Shared by research and the trader.

The window is anchored at the judgment's sealed ``recorded_at``, the same clock the REJECT cooldown and the
deployment sessions use: it starts on the first XNYS session strictly after the New York date of
``recorded_at``. A REJECT window ends 10 sessions after the sealed cooldown end. A DEPLOY or SHADOW window
runs ``deploy_expiry_sessions`` sessions from the start.

Known limit (accepted): DEPLOY and SHADOW lengths come from the trader config at the time of each call, because
a judgment does not store them. A config edit moves the end of the window of an older judgment. Rows already
stored are never changed.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

REJECT_EXTRA_SESSIONS = 10
TRACKED_VERDICTS = ("DEPLOY", "SHADOW", "REJECT")
NEW_YORK = ZoneInfo("America/New_York")
# A window is at most 130 sessions long; the calendar covers the anchor year and the two after it.
_YEARS_AHEAD = 2


@lru_cache(maxsize=8)
def _calendar_for_year(year: int):
    """Built per anchor year, not once per process: a default calendar ends a year after it was built."""
    return xcals.get_calendar("XNYS", start=dt.date(year - 1, 1, 1), end=dt.date(year + _YEARS_AHEAD, 12, 31))


def is_xnys_session(day: dt.date) -> bool:
    return bool(_calendar_for_year(day.year).is_session(pd.Timestamp(day)))


def session_open_utc(day: dt.date) -> dt.datetime:
    """The official open of an XNYS session."""
    return _calendar_for_year(day.year).session_open(pd.Timestamp(day)).to_pydatetime().astimezone(dt.timezone.utc)


def session_close_utc(day: dt.date) -> dt.datetime:
    """The official close (early closes included) of an XNYS session."""
    return _calendar_for_year(day.year).session_close(pd.Timestamp(day)).to_pydatetime().astimezone(dt.timezone.utc)


def xnys_sessions(first: dt.date, last: dt.date) -> list[dt.date]:
    """Every XNYS session from ``first`` to ``last``, both included."""
    if last < first:
        return []
    calendar = _calendar_for_year(first.year)
    return [session.date() for session in calendar.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))]


def sessions_before(session: dt.date, count: int) -> dt.date:
    """The XNYS session ``count`` sessions before ``session`` (itself a session)."""
    return _calendar_for_year(session.year).session_offset(pd.Timestamp(session), -count).date()


def shadow_window(recorded_at: dt.datetime, verdict: str, *, deploy_expiry_sessions: int,
                  cooldown_until_session: Optional[dt.date] = None) -> tuple[dt.date, dt.date]:
    """First and last session of the window. A REJECT needs the sealed ``cooldown_until_session``."""
    if verdict not in TRACKED_VERDICTS:
        raise ValueError(f"{verdict!r} has no shadow window")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    anchor_day = recorded_at.astimezone(NEW_YORK).date()
    calendar = _calendar_for_year(anchor_day.year)
    first = calendar.date_to_session(pd.Timestamp(anchor_day + dt.timedelta(days=1)), direction="next")
    if verdict == "REJECT":
        if cooldown_until_session is None:
            raise ValueError("a REJECT window needs the sealed cooldown end")
        cooldown_end = calendar.date_to_session(pd.Timestamp(cooldown_until_session), direction="next")
        return first.date(), calendar.session_offset(cooldown_end, REJECT_EXTRA_SESSIONS).date()
    if deploy_expiry_sessions < 1:
        raise ValueError("a shadow window needs at least one session")
    return first.date(), calendar.session_offset(first, deploy_expiry_sessions - 1).date()
