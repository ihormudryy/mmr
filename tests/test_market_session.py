"""NASDAQ (XNAS) market-session payload for the command-center banner."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from web.command_center.market_session import market_session_status


def test_market_session_open_true_when_calendar_says_open():
    now = dt.datetime(2026, 7, 23, 15, 0, tzinfo=dt.timezone.utc)  # Thu 11:00 ET
    with patch(
        "web.command_center.market_session._xnas_session_open", return_value=True
    ):
        payload = market_session_status(now)
    assert payload["calendar"] == "XNAS"
    assert payload["exchange"] == "NASDAQ"
    assert payload["open"] is True
    assert payload["evaluated_at"] == now.isoformat()
    assert payload["next_open"] is None
    assert "error" not in payload


def test_market_session_open_false_when_calendar_says_closed():
    now = dt.datetime(2026, 7, 23, 2, 0, tzinfo=dt.timezone.utc)
    with patch(
        "web.command_center.market_session._xnas_session_open", return_value=False
    ):
        payload = market_session_status(now)
    assert payload["open"] is False
    # 02:00 UTC Thu is still Wednesday evening ET — next RTH is 13:30 UTC Thu.
    assert payload["next_open"] == "2026-07-23T13:30:00+00:00"


def test_market_session_next_open_skips_weekend():
    now = dt.datetime(2026, 8, 15, 16, 0, tzinfo=dt.timezone.utc)  # Saturday
    payload = market_session_status(now)
    assert payload["open"] is False
    assert payload["next_open"] == "2026-08-17T13:30:00+00:00"


def test_market_session_open_none_on_calendar_error():
    with patch(
        "web.command_center.market_session._xnas_session_open",
        side_effect=RuntimeError("no calendars"),
    ):
        payload = market_session_status(
            dt.datetime(2026, 7, 23, 15, 0, tzinfo=dt.timezone.utc)
        )
    assert payload["open"] is None
    assert payload["next_open"] is None
    assert "RuntimeError" in payload["error"]
