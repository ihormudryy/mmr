"""The durable Telegram outbox: one row per stable event id (Plan 5 ruling 17).

Delivery is at-least-once: a crash between send and mark-sent repeats one
message, and every text ends with its event id so a repeat is recognisable.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional

MAX_TEXT = 4000
_CUT_NOTICE = "\n[cut: message longer than 4000 characters]"


def fit_text(text: str, event_id: str) -> str:
    """Plain text ending with the event id, cut to MAX_TEXT with a visible notice."""
    tail = f"\nevent {event_id}"
    body = text[: -len(tail)] if text.endswith(tail) else text
    if len(body) + len(tail) > MAX_TEXT:
        body = body[: MAX_TEXT - len(tail) - len(_CUT_NOTICE)] + _CUT_NOTICE
    return body + tail


@dataclass(frozen=True)
class OutboxRow:
    event_id: str
    kind: str
    text: str
    created_at: dt.datetime
    status: str
    attempts: int
    next_attempt_at: dt.datetime
    last_error: Optional[str]
    sent_at: Optional[dt.datetime]
    telegram_message_id: Optional[int]


_COLUMNS = ", ".join(OutboxRow.__dataclass_fields__)


class TelegramOutbox:
    def __init__(self, db: Any, now: Callable[[], dt.datetime]):
        self.db = db
        self._now = now

    def enqueue(self, event_id: str, kind: str, text: str) -> bool:
        """False when the event id exists already (idempotent; the first text wins)."""
        if not event_id or not kind:
            raise ValueError("an outbox message needs an event id and a kind")
        now = self._now()
        message = fit_text(text, event_id)

        def tx(conn):
            if conn.execute("SELECT 1 FROM telegram_outbox WHERE event_id = ?", [event_id]).fetchone() is not None:
                return False
            conn.execute("INSERT INTO telegram_outbox (event_id, kind, text, created_at, status, attempts, "
                         "next_attempt_at) VALUES (?, ?, ?, ?, 'PENDING', 0, ?)",
                         [event_id, kind, message, now, now])
            return True
        return bool(self.db.transaction(tx))

    def row(self, event_id: str) -> Optional[OutboxRow]:
        found = self.db.execute(f"SELECT {_COLUMNS} FROM telegram_outbox WHERE event_id = ?", [event_id], fetch="one")
        return None if found is None else OutboxRow(*found)

    def due(self, limit: int = 10) -> list[OutboxRow]:
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM telegram_outbox WHERE status = 'PENDING' AND next_attempt_at <= ? "
            "ORDER BY created_at, event_id LIMIT ?", [self._now(), limit], fetch="all")
        return [OutboxRow(*row) for row in rows]

    def mark_sent(self, event_id: str, message_id: Optional[int]) -> None:
        self.db.execute("UPDATE telegram_outbox SET status = 'SENT', sent_at = ?, telegram_message_id = ?, "
                        "last_error = NULL WHERE event_id = ?", [self._now(), message_id, event_id])

    def mark_failed(self, event_id: str, error: str, *, retry_after: Optional[float] = None) -> None:
        """Back off 30 s, 60 s, ... up to an hour; Telegram's retry_after wins when it is given."""
        now = self._now()

        def tx(conn):
            found = conn.execute("SELECT attempts FROM telegram_outbox WHERE event_id = ?", [event_id]).fetchone()
            if found is None:
                return
            attempts = int(found[0])
            wait = retry_after if retry_after is not None else min(30 * 2 ** attempts, 3600)
            conn.execute("UPDATE telegram_outbox SET attempts = ?, next_attempt_at = ?, last_error = ? "
                         "WHERE event_id = ?", [attempts + 1, now + dt.timedelta(seconds=wait), error[:500],
                                                event_id])
        self.db.transaction(tx)

    def counts(self) -> dict:
        pending, last_sent = self.db.execute(
            "SELECT COUNT(*) FILTER (WHERE status = 'PENDING'), MAX(sent_at) FROM telegram_outbox", fetch="one")
        return {"pending": int(pending), "last_sent_at": last_sent}
