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
    assert "error" not in payload


def test_market_session_open_false_when_calendar_says_closed():
    now = dt.datetime(2026, 7, 23, 2, 0, tzinfo=dt.timezone.utc)
    with patch(
        "web.command_center.market_session._xnas_session_open", return_value=False
    ):
        payload = market_session_status(now)
    assert payload["open"] is False


def test_market_session_open_none_on_calendar_error():
    with patch(
        "web.command_center.market_session._xnas_session_open",
        side_effect=RuntimeError("no calendars"),
    ):
        payload = market_session_status(
            dt.datetime(2026, 7, 23, 15, 0, tzinfo=dt.timezone.utc)
        )
    assert payload["open"] is None
    assert "RuntimeError" in payload["error"]
