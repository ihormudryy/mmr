"""Real CriticalAlertPort adapter (last command-authority port)."""
from __future__ import annotations

import datetime as dt
import logging

from trader.trading.command_alerts import LoggingCriticalAlertPort

NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=dt.timezone.utc)


def test_raise_alert_logs_critical_and_records(caplog):
    port = LoggingCriticalAlertPort(now=lambda: NOW)
    with caplog.at_level(logging.CRITICAL):
        port.raise_alert("cmd-1", "approve_proposal unresolved after 15 minutes")
    assert any(r.levelno == logging.CRITICAL and "cmd-1" in r.getMessage()
               for r in caplog.records)
    recent = port.recent()
    assert len(recent) == 1
    assert recent[0]["command_id"] == "cmd-1"
    assert "15 minutes" in recent[0]["detail"]
    assert recent[0]["raised_at"] == NOW.isoformat()


def test_ring_is_bounded_and_ordered():
    port = LoggingCriticalAlertPort(capacity=3, now=lambda: NOW)
    for i in range(5):
        port.raise_alert(f"cmd-{i}", "unresolved")
    recent = port.recent()
    assert [a["command_id"] for a in recent] == ["cmd-2", "cmd-3", "cmd-4"]  # last 3, in order


def test_recent_returns_a_copy():
    port = LoggingCriticalAlertPort(now=lambda: NOW)
    port.raise_alert("cmd-1", "unresolved")
    snapshot = port.recent()
    snapshot.clear()
    assert len(port.recent()) == 1  # mutating the returned list must not affect the ring
