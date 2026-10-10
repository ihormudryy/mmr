"""Durable strategy signal record with a monotonic cursor (SP2 spec amendment 6.1).

The strategy service writes one row per BUY/SELL signal into ``duckdb_path``
(the file it already writes ``trading_events`` to); the trader reads it for
``read_ai_signals``. Cursors come from a counter row updated in the same
transaction as the insert, so a rolled-back write leaves no hole. Retention
deletes a prefix of cursors and raises ``retention_watermark`` in the same
transaction; ``gap`` is computed from that watermark only (Plan 1 Ruling 10).
Each record has a random ``record_generation``, created with the record and never
changed, so a reader can tell a replaced record from the same one even when both
reached the same cursor (SP2 Plan 5 review, PR #84).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import numbers
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.bar_size import BarSize

DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 365
MAX_READ_LIMIT = 500
SIGNAL_ACTIONS = ("BUY", "SELL")
SOURCE_EVENT_ID = re.compile(r"^sig-[0-9a-f]{32}$")
RECORD_GENERATION = re.compile(r"^gen-[0-9a-f]{32}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

_CREATE = (
    """CREATE TABLE IF NOT EXISTS strategy_signal_record (
        cursor BIGINT PRIMARY KEY, source_event_id VARCHAR NOT NULL UNIQUE, strategy_name VARCHAR NOT NULL,
        conid BIGINT NOT NULL, action VARCHAR NOT NULL, probability DOUBLE,
        signal_time TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
        deployment_digest VARCHAR, deployment_version VARCHAR, source_digest VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS strategy_signal_record_state (
        last_cursor BIGINT NOT NULL, retention_watermark BIGINT NOT NULL)""",
    """INSERT INTO strategy_signal_record_state SELECT 0, 0
        WHERE NOT EXISTS (SELECT 1 FROM strategy_signal_record_state)""",
    # Its own table, so a record created before it existed gains one without an ALTER.
    """CREATE TABLE IF NOT EXISTS strategy_signal_record_generation (record_generation VARCHAR NOT NULL)""",
    # The signal's bar size (issue #146): signal_time labels the bar's start, so a reader needs it to know when
    # the bar closed. Rows written before it existed read NULL.
    "ALTER TABLE strategy_signal_record ADD COLUMN IF NOT EXISTS bar_size VARCHAR",
)
_COLUMNS = ("cursor, source_event_id, strategy_name, conid, action, probability, signal_time, recorded_at, "
            "deployment_digest, deployment_version, source_digest, bar_size")


class SignalCursorAhead(ValueError):
    code = "SIGNAL_CURSOR_AHEAD"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError("signal times must be timezone-aware datetimes")
    return value.astimezone(dt.timezone.utc)


def _is_count(value: object, low: int, high: Optional[int] = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


def completed_bar_time(frame: Any) -> dt.datetime:
    """The completed bar's time; a naive index is read as UTC (same rule as the intent emitter)."""
    last_bar = frame.index[-1]
    bar_time = last_bar.to_pydatetime() if hasattr(last_bar, "to_pydatetime") else last_bar
    if not isinstance(bar_time, dt.datetime):
        raise ValueError(f"signal frame index must hold datetimes, got {type(bar_time).__name__}")
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=dt.timezone.utc)
    return bar_time.astimezone(dt.timezone.utc)


def source_event_id_for(strategy_name: str, conid: int, action: str, signal_time: dt.datetime) -> str:
    material = "\x00".join((strategy_name, str(conid), action, signal_time.isoformat()))
    return "sig-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class SignalEntry:
    source_event_id: str
    strategy_name: str
    conid: int
    action: str
    probability: Optional[float]
    signal_time: dt.datetime
    deployment_digest: Optional[str] = None
    deployment_version: Optional[str] = None
    source_digest: Optional[str] = None
    bar_size: Optional[str] = None                   # the strategy's bar size; signal_time is that bar's start

    @classmethod
    def create(cls, *, strategy_name: str, conid: object, action: str, probability: object,
               signal_time: dt.datetime, deployment_digest: Optional[str] = None,
               deployment_version: Optional[str] = None, source_digest: Optional[str] = None,
               bar_size: Optional[str] = None) -> "SignalEntry":
        if not isinstance(strategy_name, str) or not strategy_name:
            raise ValueError("strategy_name must be a non-empty string")
        if isinstance(conid, bool) or not isinstance(conid, numbers.Integral) or conid <= 0:
            raise ValueError("conid must be an integer > 0")
        if action not in SIGNAL_ACTIONS:
            raise ValueError(f"action must be one of {SIGNAL_ACTIONS}")
        binding = (deployment_digest, deployment_version, source_digest)
        if any(value is not None for value in binding):
            if not all(isinstance(value, str) and DIGEST.fullmatch(value) for value in binding):
                raise ValueError("deployment_digest, deployment_version and source_digest are all set "
                                 "(each sha256:<64 hex>) or all null")
        if bar_size is not None and bar_size not in BarSize.bar_sizes():
            raise ValueError(f"bar_size must be one of {BarSize.bar_sizes()} or null, got {bar_size!r}")
        exact_conid = int(conid)                     # numpy integers are exact; strings were refused above
        when = _as_utc(signal_time)
        finite = (isinstance(probability, (int, float)) and not isinstance(probability, bool)
                  and math.isfinite(probability))
        return cls(source_event_id_for(strategy_name, exact_conid, action, when), strategy_name, exact_conid,
                   action, float(probability) if finite else None, when, deployment_digest, deployment_version,
                   source_digest, bar_size)


@dataclass(frozen=True)
class RecordedSignal:
    cursor: int
    entry: SignalEntry
    recorded_at: dt.datetime

    def to_json(self) -> dict:
        return {"cursor": self.cursor, "source_event_id": self.entry.source_event_id,
                "strategy_name": self.entry.strategy_name, "conid": self.entry.conid,
                "action": self.entry.action, "probability": self.entry.probability,
                "signal_time": self.entry.signal_time.isoformat(), "recorded_at": self.recorded_at.isoformat(),
                "deployment_digest": self.entry.deployment_digest,
                "deployment_version": self.entry.deployment_version, "source_digest": self.entry.source_digest,
                "bar_size": self.entry.bar_size}


@dataclass(frozen=True)
class SignalPage:
    signals: tuple[RecordedSignal, ...]
    next_cursor: int
    oldest_retained_cursor: int
    gap: bool
    record_generation: str


class StrategySignalRecord:
    def __init__(self, db: Any, *, retention_days: int = DEFAULT_RETENTION_DAYS,
                 now: Callable[[], dt.datetime] = _utc_now):
        if not _is_count(retention_days, 1, MAX_RETENTION_DAYS):
            raise ValueError(f"strategy_signal_record_retention_days must be an integer in 1..{MAX_RETENTION_DAYS}")
        self._db = db
        self._retention = dt.timedelta(days=retention_days)
        self._now = now
        self._db.transaction(self._create_in_tx)

    @staticmethod
    def _create_in_tx(conn: Any) -> None:
        for statement in _CREATE:
            conn.execute(statement)
        if conn.execute("SELECT 1 FROM strategy_signal_record_generation").fetchone() is None:
            conn.execute("INSERT INTO strategy_signal_record_generation VALUES (?)", [f"gen-{uuid.uuid4().hex}"])

    def append(self, entry: SignalEntry) -> int:
        return self._db.transaction(lambda conn: self.append_in_tx(conn, entry))

    def append_in_tx(self, conn: Any, entry: SignalEntry) -> int:
        existing = conn.execute("SELECT cursor FROM strategy_signal_record WHERE source_event_id = ?",
                                [entry.source_event_id]).fetchone()
        if existing is not None:
            return int(existing[0])
        last_cursor, watermark = conn.execute(
            "SELECT last_cursor, retention_watermark FROM strategy_signal_record_state").fetchone()
        now = _as_utc(self._now())
        cursor = int(last_cursor) + 1
        conn.execute(f"INSERT INTO strategy_signal_record ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [cursor, entry.source_event_id, entry.strategy_name, entry.conid, entry.action,
                      entry.probability, entry.signal_time, now, entry.deployment_digest,
                      entry.deployment_version, entry.source_digest, entry.bar_size])
        watermark = self._prune_in_tx(conn, int(watermark), now)
        conn.execute("UPDATE strategy_signal_record_state SET last_cursor = ?, retention_watermark = ?",
                     [cursor, watermark])
        return cursor

    def _prune_in_tx(self, conn: Any, watermark: int, now: dt.datetime) -> int:
        """Delete a prefix of cursors older than the retention; return the new watermark."""
        (newest_expired,) = conn.execute("SELECT MAX(cursor) FROM strategy_signal_record WHERE recorded_at < ?",
                                         [now - self._retention]).fetchone()
        if newest_expired is None or newest_expired <= watermark:
            return watermark
        conn.execute("DELETE FROM strategy_signal_record WHERE cursor <= ?", [newest_expired])
        return int(newest_expired)

    def read(self, after_cursor: int, limit: int) -> SignalPage:
        if not _is_count(after_cursor, 0):
            raise ValueError("after_cursor must be an integer >= 0")
        if not _is_count(limit, 1, MAX_READ_LIMIT):
            raise ValueError(f"limit must be an integer in 1..{MAX_READ_LIMIT}")
        return self._db.transaction(lambda conn: self._read_in_tx(conn, after_cursor, limit))

    def _read_in_tx(self, conn: Any, after_cursor: int, limit: int) -> SignalPage:
        last_cursor, watermark = (int(v) for v in conn.execute(
            "SELECT last_cursor, retention_watermark FROM strategy_signal_record_state").fetchone())
        if after_cursor > last_cursor:
            raise SignalCursorAhead(f"after_cursor {after_cursor} is beyond the newest cursor {last_cursor}; "
                                    "the signal record was reset")
        rows = conn.execute(f"SELECT {_COLUMNS} FROM strategy_signal_record WHERE cursor > ? "
                            "ORDER BY cursor LIMIT ?", [after_cursor, limit]).fetchall()
        (oldest,) = conn.execute("SELECT MIN(cursor) FROM strategy_signal_record").fetchone()
        signals = tuple(
            RecordedSignal(int(row[0]), SignalEntry(row[1], row[2], int(row[3]), row[4], row[5], _as_utc(row[6]),
                                                    row[8], row[9], row[10], row[11]),
                           _as_utc(row[7]))
            for row in rows)
        next_cursor = signals[-1].cursor if signals else max(after_cursor, watermark)
        (generation,) = conn.execute("SELECT record_generation FROM strategy_signal_record_generation").fetchone()
        return SignalPage(signals, next_cursor, int(oldest) if oldest is not None else watermark + 1,
                          after_cursor < watermark, generation)
