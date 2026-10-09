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
RESEARCH = "research"
PRE_OPEN_QUIET = dt.timedelta(minutes=30)
MAX_CLOSED_DAYS = 10


@dataclass(frozen=True)
class Slot:
    kind: str
    session_date: dt.date
    start: dt.datetime
    deadline: dt.datetime                 # the cycle's work is cancelled here (Ruling 8)

    @property
    def cycle_id(self) -> str:
        return f"cyc-{self.kind}-{self.session_date:%Y%m%d}-{self.start.astimezone(ET):%H%M}"


@dataclass(frozen=True)
class ResearchSlot:
    session_date: dt.date
    start: dt.datetime                    # the official close + after_close_minutes
    closes_at: dt.datetime                # 30 minutes before the next session's open

    @property
    def cycle_id(self) -> str:
        return f"rcy-{self.session_date:%Y%m%d}"


class SessionSlots:
    def __init__(self, *, calendar: Optional[Any] = None, entry_minutes: int = 15, position_minutes: int = 15,
                 grace_seconds: int = 120, research_after_close_minutes: int = 30):
        self._calendar = calendar if calendar is not None else XNYSCalendarPolicy()
        self._interval = {ENTRY: dt.timedelta(minutes=entry_minutes), POSITION: dt.timedelta(minutes=position_minutes)}
        self._grace = dt.timedelta(seconds=grace_seconds)
        self._after_close = dt.timedelta(minutes=research_after_close_minutes)

    def entry_window_open(self, now: dt.datetime) -> bool:
        return self._calendar.allows_new_entry(now)

    def _slots_of(self, kind: str, schedule: Any) -> list[Slot]:
        """Every slot of ``kind`` in one session, oldest first."""
        interval = self._interval[kind]
        end = schedule.entry_cutoff_utc if kind == ENTRY else schedule.flatten_start_utc
        slots, start = [], schedule.open_utc
        while start < end:
            if start >= schedule.opening_stabilization_end_utc:
                slots.append(Slot(kind, schedule.session_date, start, min(start + interval, end)))
            start += interval
        return slots

    def latest(self, kind: str, now: dt.datetime) -> Optional[Slot]:
        """The newest slot of ``kind`` that started at or before ``now`` today, or None."""
        schedule = self._calendar.resolve(now)
        if schedule is None or now < schedule.open_utc:
            return None
        started = [slot for slot in self._slots_of(kind, schedule) if slot.start <= now]
        return started[-1] if started else None

    def elapsed(self, kind: str, after: dt.datetime, now: dt.datetime) -> list[Slot]:
        """Every slot of ``kind`` that started in (after, now], across sessions, oldest first."""
        found = []
        day, last_day = after.astimezone(ET).date(), now.astimezone(ET).date()
        while day <= last_day:
            schedule = self._calendar.resolve(dt.datetime.combine(day, dt.time(12), tzinfo=ET))
            if schedule is not None:
                found += [slot for slot in self._slots_of(kind, schedule) if after < slot.start <= now]
            day += dt.timedelta(days=1)
        return found

    def is_due(self, slot: Slot, now: dt.datetime) -> bool:
        return slot.start <= now < min(slot.start + self._grace, slot.deadline)

    def _schedule_on(self, day: dt.date) -> Any:
        return self._calendar.resolve(dt.datetime.combine(day, dt.time(12), tzinfo=ET))

    def _next_open(self, day: dt.date) -> dt.datetime:
        for offset in range(1, MAX_CLOSED_DAYS + 1):
            schedule = self._schedule_on(day + dt.timedelta(days=offset))
            if schedule is not None:
                return schedule.open_utc
        raise RuntimeError(f"no XNYS session within {MAX_CLOSED_DAYS} days after {day}")

    def research_slot(self, now: dt.datetime) -> Optional[ResearchSlot]:
        """The newest research slot that started at or before ``now``."""
        day = now.astimezone(ET).date()
        for _ in range(MAX_CLOSED_DAYS):
            schedule = self._schedule_on(day)
            if schedule is not None and schedule.close_utc + self._after_close <= now:
                return ResearchSlot(schedule.session_date, schedule.close_utc + self._after_close,
                                    self._next_open(schedule.session_date) - PRE_OPEN_QUIET)
            day -= dt.timedelta(days=1)
        return None

    def research_start_after_next_midnight(self, now: dt.datetime) -> dt.datetime:
        """The first research slot start on a later New York date than ``now``."""
        day = now.astimezone(ET).date()
        for offset in range(1, MAX_CLOSED_DAYS + 1):
            schedule = self._schedule_on(day + dt.timedelta(days=offset))
            if schedule is not None:
                return schedule.close_utc + self._after_close
        raise RuntimeError(f"no XNYS session within {MAX_CLOSED_DAYS} days after {day}")

    def research_due(self, slot: ResearchSlot, now: dt.datetime) -> bool:
        return slot.start <= now < slot.closes_at

    def research_window_open(self, now: dt.datetime) -> bool:
        slot = self.research_slot(now)
        return slot is not None and self.research_due(slot, now)
