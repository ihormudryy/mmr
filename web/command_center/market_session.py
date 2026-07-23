"""US equity (NASDAQ / XNAS) regular-session status for the command-center banner.

Uses the exchange-calendars ``XNAS`` schedule so the dashboard warning tracks
the major NASDAQ cash session (not NYSE-only holidays/hours).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

_CALENDAR = "XNAS"


def _xnas_session_open(now: dt.datetime) -> bool:
    import exchange_calendars
    import pandas as pd

    calendar = exchange_calendars.get_calendar(_CALENDAR)
    return bool(calendar.is_open_on_minute(pd.Timestamp(now), ignore_breaks=False))


def market_session_status(now: dt.datetime | None = None) -> dict[str, Any]:
    """Return ``{calendar, open, evaluated_at}`` for NASDAQ RTH.

    ``open`` is ``True``/``False`` when the calendar answers, or ``None`` when
    the calendar cannot be evaluated (fail soft — do not claim "closed").
    """
    when = now or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    payload: dict[str, Any] = {
        "calendar": _CALENDAR,
        "exchange": "NASDAQ",
        "evaluated_at": when.isoformat(),
        "open": None,
    }
    try:
        payload["open"] = bool(_xnas_session_open(when))
    except Exception as exc:  # noqa: BLE001 — banner enrichment must not break reads
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload
