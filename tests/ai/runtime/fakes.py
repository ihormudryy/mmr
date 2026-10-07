"""Shared fakes for the Plan 5 runtime tests. Later tasks append their own fakes here."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
FRIDAY = dt.date(2026, 7, 17)
AAPL, MSFT = 265598, 272093


def et(hour, minute, second=0, day=FRIDAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ET).astimezone(UTC)


class FakeSocket:
    """Stands in for one TypedRpcClient: records calls, raises ``error`` or returns ``reply``."""

    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error, self.closed = [], reply, error, False

    def call(self, method, body, response_model, timeout=None, **options):
        self.calls.append((method, body, timeout, options))
        if self.error is not None:
            raise self.error
        return {"method": method} if self.reply is None else self.reply

    def close(self):
        self.closed = True
