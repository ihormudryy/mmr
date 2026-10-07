"""Sealed inserts, the round-trip projection and incidents in the journal DuckDB file.

Every write is one ``db.transaction``; nothing inside it calls back into the
non-reentrant database lock.
"""
from __future__ import annotations

import datetime as dt
import decimal
import logging
import math
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.scoreboard.seal import GENESIS, chain_digest, row_digest

logger = logging.getLogger(__name__)

SEALED_TABLES: dict[str, tuple[str, ...]] = {
    "equity_daily": ("experiment_id", "session_date"),
    "equity_adjustments": ("adjustment_id",),
    "benchmark_versions": ("version",),
    "benchmark_prices": ("version", "bar_date"),
    "ai_costs": ("call_id",),
    "simulated_books": ("book_id",),
}
WORKING_TABLES: dict[str, tuple[str, ...]] = {
    "round_trips": ("opened_at", "round_trip_id"),
    "equity_session_peak": ("experiment_id", "session_date"),
    "scoreboard_incidents": ("recorded_at", "kind", "key"),
    "scoreboard_seals": ("seal_id",),
    "telegram_outbox": ("created_at", "event_id"),
}
_ORDER = {**SEALED_TABLES, **WORKING_TABLES}


class ScoreboardConflict(Exception):
    """A sealed row with this key exists already; sealed rows are never replaced."""


def row_key(row: Mapping[str, Any], key_columns: Sequence[str]) -> str:
    return "|".join(str(row[column]) for column in key_columns)


def _coerce(table: str, column: str, kind: str, value: Any) -> Any:
    if value is None:
        return None
    where = f"{table}.{column}"
    if kind == "DOUBLE":
        if isinstance(value, bool) or not isinstance(value, (int, float, decimal.Decimal)):
            raise ValueError(f"{where} must be a number, got {value!r}")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{where} must be finite or None (unknown), got {value!r}")
        return number
    if kind in ("INTEGER", "BIGINT"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} must be an integer, got {value!r}")
        return value
    if kind == "BOOLEAN":
        if not isinstance(value, bool):
            raise ValueError(f"{where} must be a bool, got {value!r}")
        return value
    if kind == "VARCHAR":
        if not isinstance(value, str):
            raise ValueError(f"{where} must be text, got {value!r}")
        return value
    if kind == "DATE":
        if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
            raise ValueError(f"{where} must be a date, got {value!r}")
        return value
    if kind == "TIMESTAMP WITH TIME ZONE":
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            raise ValueError(f"{where} must be a timezone-aware datetime, got {value!r}")
        return value
    raise ValueError(f"{where}: unsupported column type {kind}")


class ScoreboardStore:
    def __init__(self, db: Any, *, now: Callable[[], dt.datetime]):
        self._db = db
        self._now = now
        self._columns: dict[str, dict[str, str]] = {}

    @property
    def db(self) -> Any:
        return self._db

    # -- schema --------------------------------------------------------------

    def columns(self, table: str) -> dict[str, str]:
        if table not in _ORDER:
            raise ValueError(f"{table!r} is not a scoreboard table")
        if table not in self._columns:
            rows = self._db.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? "
                "ORDER BY ordinal_position", [table], fetch="all")
            if not rows:
                raise ValueError(f"table {table!r} does not exist; apply the scoreboard migrations")
            self._columns[table] = {name: kind for name, kind in rows}
        return self._columns[table]

    def prepare(self, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """The row as the database will store it, so its digest survives a round trip."""
        columns = self.columns(table)
        unknown = sorted(set(row) - set(columns))
        if unknown:
            raise ValueError(f"{table}: unknown column(s) {unknown}")
        return {name: _coerce(table, name, columns[name], row.get(name)) for name in columns}

    # -- sealed inserts ------------------------------------------------------

    def insert_sealed(self, table: str, row: Mapping[str, Any]) -> None:
        if table not in SEALED_TABLES:
            raise ValueError(f"{table!r} is not a sealed scoreboard table")
        prepared = self.prepare(table, row)
        key_columns = SEALED_TABLES[table]
        key = row_key(prepared, key_columns)
        digest = row_digest(prepared)
        sealed_at = self._now()

        def tx(conn):
            where = " AND ".join(f"{column} = ?" for column in key_columns)
            if conn.execute(f"SELECT 1 FROM {table} WHERE {where}",
                            [prepared[c] for c in key_columns]).fetchone() is not None:
                raise ScoreboardConflict(f"{table} row {key} exists already")
            names = list(prepared)
            conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                         [prepared[name] for name in names])
            last = conn.execute(
                "SELECT seal_id, chain FROM scoreboard_seals ORDER BY seal_id DESC LIMIT 1").fetchone()
            seal_id, prev = (1, GENESIS) if last is None else (int(last[0]) + 1, last[1])
            conn.execute(
                "INSERT INTO scoreboard_seals (seal_id, table_name, row_key, row_digest, prev_chain, chain, "
                "sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [seal_id, table, key, digest, prev, chain_digest(prev, table, key, digest), sealed_at])
        self._db.transaction(tx)

    # -- reads ---------------------------------------------------------------

    def fetch(self, table: str, where: Mapping[str, Any]) -> list[dict]:
        columns = self.columns(table)
        unknown = sorted(set(where) - set(columns))
        if unknown:
            raise ValueError(f"{table}: unknown filter column(s) {unknown}")
        clause = " AND ".join(f"{name} = ?" for name in where)
        sql = (f"SELECT {', '.join(columns)} FROM {table}" + (f" WHERE {clause}" if clause else "")
               + f" ORDER BY {', '.join(_ORDER[table])}")
        rows = self._db.execute(sql, list(where.values()), fetch="all") if where else \
            self._db.execute(sql, fetch="all")
        return [dict(zip(columns, values)) for values in rows]

    # -- incidents -----------------------------------------------------------

    def record_incident(self, kind: str, key: str, detail: str) -> bool:
        """Idempotent on (kind, key); True when newly recorded. Every new incident is logged loudly."""
        recorded_at = self._now()

        def tx(conn):
            if conn.execute("SELECT 1 FROM scoreboard_incidents WHERE kind = ? AND key = ?",
                            [kind, key]).fetchone() is not None:
                return False
            conn.execute("INSERT INTO scoreboard_incidents VALUES (?, ?, ?, ?)", [kind, key, detail, recorded_at])
            return True
        created = bool(self._db.transaction(tx))
        if created:
            logger.error("scoreboard incident %s %s: %s", kind, key, detail)
        return created

    def incidents(self) -> list[dict]:
        return self.fetch("scoreboard_incidents", {})

    # -- integrity -----------------------------------------------------------

    def verify_seals(self) -> list[dict]:
        """Mismatch dicts (``check``, ``table``, ``key`` ...); an empty list means intact."""
        mismatches: list[dict] = []
        seals = self.fetch("scoreboard_seals", {})
        prev = GENESIS
        for expected_id, seal in enumerate(seals, start=1):
            link = chain_digest(seal["prev_chain"], seal["table_name"], seal["row_key"], seal["row_digest"])
            if int(seal["seal_id"]) != expected_id or seal["prev_chain"] != prev or seal["chain"] != link:
                mismatches.append({"check": "CHAIN_BROKEN", "table": "scoreboard_seals",
                                   "key": str(seal["seal_id"]), "expected_seal_id": expected_id})
                break
            prev = seal["chain"]
        sealed: dict[tuple[str, str], str] = {(s["table_name"], s["row_key"]): s["row_digest"] for s in seals}
        for table, key_columns in SEALED_TABLES.items():
            live = {row_key(row, key_columns): row for row in self.fetch(table, {})}
            for (seal_table, key), digest in sealed.items():
                if seal_table != table:
                    continue
                row = live.get(key)
                if row is None:
                    mismatches.append({"check": "ROW_MISSING", "table": table, "key": key})
                elif row_digest(row) != digest:
                    mismatches.append({"check": "ROW_EDITED", "table": table, "key": key})
            for key in sorted(set(live) - {k for t, k in sealed if t == table}):
                mismatches.append({"check": "ROW_UNSEALED", "table": table, "key": key})
        return mismatches

    def seal_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM scoreboard_seals", fetch="one")
        return int(row[0])
