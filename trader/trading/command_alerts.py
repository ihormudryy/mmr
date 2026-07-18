"""Real ``CriticalAlertPort`` for the command authority.

Design: docs/superpowers/specs/2026-07-18-command-plane-activation-design.md
(the last unbuilt port). The coordinator's Task-9 reconciler escalates a command
still unresolved after 15 minutes; this adapter surfaces that escalation to the
operator (log at CRITICAL) and retains a bounded in-memory ring so a health
surface (M1-R) can render the recent alerts. Not a fake no-op.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from typing import Callable

_LOGGER = logging.getLogger("trader.command_authority.alerts")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class LoggingCriticalAlertPort:
    """Logs each unresolved-command escalation at CRITICAL and keeps the most
    recent ``capacity`` in a ring for the health surface to read."""

    def __init__(self, *, capacity: int = 100,
                 logger: logging.Logger | None = None,
                 now: Callable[[], dt.datetime] = _utcnow):
        self._logger = logger or _LOGGER
        self._now = now
        self._alerts: deque[dict] = deque(maxlen=capacity)

    def raise_alert(self, command_id: str, detail: str) -> None:
        self._logger.critical(
            "command-authority CRITICAL: command %s unresolved: %s",
            command_id, detail)
        self._alerts.append({
            "command_id": command_id,
            "detail": detail,
            "raised_at": self._now().isoformat(),
        })

    def recent(self) -> list[dict]:
        """A copy of the retained alerts, oldest first."""
        return list(self._alerts)
