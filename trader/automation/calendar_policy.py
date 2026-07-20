"""XNYS session calendar policy — trader-owned deadlines from exchange_calendars."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

# Relative offsets from the official session close (normal or early).
_ENTRY_CUTOFF_BEFORE_CLOSE = dt.timedelta(minutes=30)   # 15:30 on a 16:00 close
_CANCEL_ENTRIES_BEFORE_CLOSE = dt.timedelta(minutes=25)  # 15:35
_FLATTEN_START_BEFORE_CLOSE = dt.timedelta(minutes=15)   # 15:45
_FLAT_DEADLINE_BEFORE_CLOSE = dt.timedelta(minutes=5)    # 15:55


@dataclass(frozen=True)
class SessionSchedule:
    session_date: dt.date
    open_utc: dt.datetime
    close_utc: dt.datetime
    entry_cutoff_utc: dt.datetime
    cancel_entries_utc: dt.datetime
    flatten_start_utc: dt.datetime
    flat_deadline_utc: dt.datetime
    opening_stabilization_end_utc: dt.datetime
    is_early_close: bool
    calendar_name: str
    calendar_version: str


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_pydatetime(ts) -> dt.datetime:
    if isinstance(ts, dt.datetime):
        return _as_utc(ts)
    py = ts.to_pydatetime()
    return _as_utc(py)


class XNYSCalendarPolicy:
    """Resolve XNYS session boundaries and entry windows.

    All absolute deadlines are derived from the official close so early-close
    sessions inherit the same relative offsets (30/25/15/5 minutes before close).
    """

    def __init__(
        self,
        *,
        calendar: Any | None = None,
        opening_stabilization: dt.timedelta = dt.timedelta(minutes=5),
        calendar_name: str = "XNYS",
    ):
        self._calendar_name = calendar_name
        self._calendar = calendar if calendar is not None else xcals.get_calendar(calendar_name)
        self._opening_stabilization = opening_stabilization

    def calendar_version(self) -> str:
        return xcals.__version__

    def resolve(
        self,
        now: dt.datetime,
        *,
        opening_stabilization: dt.timedelta | None = None,
    ) -> Optional[SessionSchedule]:
        """Return the XNYS schedule for ``now``'s session date, or None if closed."""
        now_utc = _as_utc(now)
        session_ts = pd.Timestamp(now_utc.astimezone(ET).date())
        if not bool(self._calendar.is_session(session_ts)):
            return None

        open_utc = _to_pydatetime(self._calendar.session_open(session_ts))
        close_utc = _to_pydatetime(self._calendar.session_close(session_ts))
        # Regular XNYS close is 16:00 ET; anything earlier is an early close.
        close_et = close_utc.astimezone(ET)
        is_early = not (close_et.hour == 16 and close_et.minute == 0)

        stab = (
            self._opening_stabilization
            if opening_stabilization is None
            else opening_stabilization
        )
        return SessionSchedule(
            session_date=session_ts.date(),
            open_utc=open_utc,
            close_utc=close_utc,
            entry_cutoff_utc=close_utc - _ENTRY_CUTOFF_BEFORE_CLOSE,
            cancel_entries_utc=close_utc - _CANCEL_ENTRIES_BEFORE_CLOSE,
            flatten_start_utc=close_utc - _FLATTEN_START_BEFORE_CLOSE,
            flat_deadline_utc=close_utc - _FLAT_DEADLINE_BEFORE_CLOSE,
            opening_stabilization_end_utc=open_utc + stab,
            is_early_close=is_early,
            calendar_name=self._calendar_name,
            calendar_version=self.calendar_version(),
        )

    def allows_new_entry(
        self,
        now: dt.datetime,
        schedule: SessionSchedule | None = None,
    ) -> bool:
        now_utc = _as_utc(now)
        sched = schedule if schedule is not None else self.resolve(now_utc)
        if sched is None:
            return False
        if now_utc < sched.opening_stabilization_end_utc:
            return False
        if now_utc >= sched.entry_cutoff_utc:
            return False
        if now_utc < sched.open_utc or now_utc >= sched.close_utc:
            return False
        return True
