"""Session-aligned slots for entry and position cycles (SP2 spec 5.2, Plan 5 Rulings 6-8).

Slots start at open + k x interval. Entry slots count inside SP1's entry window
(after the opening stabilization, before the cutoff 30 min before the close);
position slots run on to the session flatten start. Early closes follow from
the calendar, because SP1 derives every deadline from the official close.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional
from zoneinfo import ZoneInfo

from trader.automation.calendar_policy import XNYSCalendarPolicy

ET = ZoneInfo("America/New_York")
ENTRY = "entry"
POSITION = "position"


@dataclass(frozen=True)
class Slot:
    kind: str
    session_date: dt.date
    start: dt.datetime
    deadline: dt.datetime                 # the cycle's work is cancelled here (Ruling 8)

    @property
    def cycle_id(self) -> str:
        return f"cyc-{self.kind}-{self.session_date:%Y%m%d}-{self.start.astimezone(ET):%H%M}"


class SessionSlots:
    def __init__(self, *, calendar: Optional[Any] = None, entry_minutes: int = 15, position_minutes: int = 15,
                 grace_seconds: int = 120):
        self._calendar = calendar if calendar is not None else XNYSCalendarPolicy()
        self._interval = {ENTRY: dt.timedelta(minutes=entry_minutes), POSITION: dt.timedelta(minutes=position_minutes)}
        self._grace = dt.timedelta(seconds=grace_seconds)

    def entry_window_open(self, now: dt.datetime) -> bool:
        return self._calendar.allows_new_entry(now)

    def latest(self, kind: str, now: dt.datetime) -> Optional[Slot]:
        """The newest slot of ``kind`` that started at or before ``now`` today, or None."""
        schedule = self._calendar.resolve(now)
        if schedule is None or now < schedule.open_utc:
            return None
        interval = self._interval[kind]
        newest = int((now - schedule.open_utc) / interval)
        for k in range(newest, -1, -1):
            start = schedule.open_utc + k * interval
            end = schedule.entry_cutoff_utc if kind == ENTRY else schedule.flatten_start_utc
            if schedule.opening_stabilization_end_utc <= start < end:
                return Slot(kind, schedule.session_date, start, min(start + interval, end))
        return None

    def is_due(self, slot: Slot, now: dt.datetime) -> bool:
        return slot.start <= now < min(slot.start + self._grace, slot.deadline)
