"""Fail-closed semantic readiness report for automated trading."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    evaluated_at: dt.datetime
    checks: dict[str, bool]

    def to_payload(self) -> dict:
        return {
            "ready": self.ready,
            "evaluated_at": self.evaluated_at.isoformat(),
            "checks": self.checks,
            "failed": [name for name, passed in self.checks.items() if not passed],
        }


class SemanticReadiness:
    def __init__(
        self,
        *,
        ib_connected: Callable[[], bool],
        account_pinned: Callable[[], bool],
        broker_current: Callable[[], bool],
        journal_writable: Callable[[], bool],
        reconciliation_safe: Callable[[], bool],
        control_readable: Callable[[], bool],
        breaker_clear: Callable[[], bool],
        session_open: Callable[[dt.datetime], bool],
        command_stack_active: Callable[[], bool],
        quotes_ready: Callable[[], bool],
    ):
        self._checks = (
            ("ib_connected", ib_connected),
            ("account_pinned", account_pinned),
            ("broker_generation_current", broker_current),
            ("journal_writable", journal_writable),
            ("reconciliation_safe", reconciliation_safe),
            ("control_readable", control_readable),
            ("breaker_clear", breaker_clear),
            ("command_stack_active", command_stack_active),
            ("quotes_ready", quotes_ready),
        )
        self._session_open = session_open

    @staticmethod
    def _safe(check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except Exception:
            return False

    def evaluate(self, now: dt.datetime) -> ReadinessReport:
        checks = {name: self._safe(check) for name, check in self._checks}
        checks["xnys_session_open"] = self._safe(lambda: self._session_open(now))
        return ReadinessReport(all(checks.values()), now, checks)


def xnys_session_open(now: dt.datetime) -> bool:
    """Official exchange-calendars XNYS open-minute determination."""
    import exchange_calendars
    import pandas as pd

    calendar = exchange_calendars.get_calendar("XNYS")
    return bool(calendar.is_open_on_minute(pd.Timestamp(now), ignore_breaks=False))


def xnys_session_key(now: dt.datetime) -> str:
    """Resolved XNYS session label; weekends map to the prior session."""
    import exchange_calendars
    import pandas as pd

    calendar = exchange_calendars.get_calendar("XNYS")
    label = calendar.minute_to_session(pd.Timestamp(now), direction="previous")
    return label.date().isoformat()
