"""US equity (NASDAQ / XNAS) regular-session status for the command-center banner.

Uses the exchange-calendars ``XNAS`` schedule so the dashboard warning tracks
the major NASDAQ cash session (not NYSE-only holidays/hours).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

_CALENDAR = "XNAS"


def _xnas_calendar():
    import exchange_calendars

    return exchange_calendars.get_calendar(_CALENDAR)


def _xnas_session_open(now: dt.datetime) -> bool:
    import pandas as pd

    return bool(
        _xnas_calendar().is_open_on_minute(pd.Timestamp(now), ignore_breaks=False)
    )


def _xnas_next_open(now: dt.datetime) -> dt.datetime | None:
    import pandas as pd

    nxt = _xnas_calendar().next_open(pd.Timestamp(now))
    if nxt is None or pd.isna(nxt):
        return None
    ts = pd.Timestamp(nxt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def market_session_status(now: dt.datetime | None = None) -> dict[str, Any]:
    """Return NASDAQ RTH status for the dashboard banner.

    ``open`` is ``True``/``False`` when the calendar answers, or ``None`` when
    the calendar cannot be evaluated (fail soft — do not claim "closed").
    When closed, ``next_open`` is the next regular-session open (UTC ISO).
    """
    when = now or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    payload: dict[str, Any] = {
        "calendar": _CALENDAR,
        "exchange": "NASDAQ",
        "evaluated_at": when.isoformat(),
        "open": None,
        "next_open": None,
    }
    try:
        payload["open"] = bool(_xnas_session_open(when))
        if payload["open"] is False:
            nxt = _xnas_next_open(when)
            if nxt is not None:
                payload["next_open"] = nxt.isoformat()
    except Exception as exc:  # noqa: BLE001 — banner enrichment must not break reads
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload
