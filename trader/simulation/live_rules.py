"""Live paper-automation rules the backtester mirrors, so evidence is measured
under the same constraints paper trading runs under.

The limits are imported from the live modules (``session_risk``,
``calendar_policy``) so the two cannot drift apart.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Optional

from trader.automation.calendar_policy import ET, SessionSchedule, XNYSCalendarPolicy
from trader.automation.session_risk import (
    MAX_DAILY_LOSS_FRACTION,
    MAX_DRAWDOWN_FRACTION,
    MAX_GROSS_FRACTION,
    MAX_POSITION_FRACTION,
    MAX_POSITIONS,
)

# Live checks a bar backtest cannot reproduce; evaluation reports list them.
NOT_MIRRORED = (
    'protective stop orders and the stop-distance trade-risk cap (MAX_TRADE_RISK_FRACTION)',
    'broker quotes: fills use the next bar open plus estimated costs',
    'the multi-strategy portfolio budget',
)


def _as_utc(ts) -> dt.datetime:
    value = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class PaperAutomationRules:
    """Entry gates and the end-of-day flatten of live paper automation.

    Stateful for one backtest run: ``reset`` at the start, then ``mark`` once
    per bar with the equity known before that bar's fills.
    """

    def __init__(self, *, max_gross_allocation: float,
                 calendar: Optional[XNYSCalendarPolicy] = None):
        self._gross_cap = min(MAX_GROSS_FRACTION, max_gross_allocation)
        self._calendar = calendar or XNYSCalendarPolicy()
        self._schedules: dict[dt.date, Optional[SessionSchedule]] = {}
        self.reset(0.0)

    def reset(self, equity: float) -> None:
        self._high_water_mark = equity
        self._session_date: Optional[dt.date] = None
        self._session_start_equity = equity

    def mark(self, ts, equity: float) -> None:
        schedule = self._schedule(ts)
        session_date = schedule.session_date if schedule is not None else None
        if session_date != self._session_date:
            self._session_date = session_date
            self._session_start_equity = equity
        self._high_water_mark = max(self._high_water_mark, equity)

    def entry_block_reason(self, *, ts, conid: int, order_notional: float,
                           position_values: Mapping[int, float],
                           equity: float) -> Optional[str]:
        now = _as_utc(ts)
        schedule = self._schedule(now)
        if schedule is None or not self._calendar.allows_new_entry(now, schedule):
            return 'ENTRY_WINDOW'
        if equity <= 0:
            return 'EQUITY_INVALID'
        if (self._session_start_equity - equity) / equity >= MAX_DAILY_LOSS_FRACTION:
            return 'DAILY_LOSS'
        if (self._high_water_mark > 0
                and (self._high_water_mark - equity) / self._high_water_mark >= MAX_DRAWDOWN_FRACTION):
            return 'DRAWDOWN'
        open_conids = {c for c, value in position_values.items() if value > 0}
        if conid not in open_conids and len(open_conids) >= MAX_POSITIONS:
            return 'MAX_POSITIONS'
        if (position_values.get(conid, 0.0) + order_notional) / equity > MAX_POSITION_FRACTION:
            return 'POSITION_PCT'
        if (sum(position_values.values()) + order_notional) / equity > self._gross_cap:
            return 'GROSS'
        return None

    def flatten_due(self, ts) -> bool:
        now = _as_utc(ts)
        schedule = self._schedule(now)
        return schedule is not None and now >= schedule.flatten_start_utc

    def _schedule(self, ts) -> Optional[SessionSchedule]:
        now = _as_utc(ts)
        key = now.astimezone(ET).date()
        if key not in self._schedules:
            self._schedules[key] = self._calendar.resolve(now)
        return self._schedules[key]
