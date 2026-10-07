"""Durable ai_paper experiments: table, record, state machine and store (SP1 Plan 4 Task 1).

An experiment is ``ARMED``, ``PAUSED``, ``KILLED`` or ``STOPPED``. ``KILLED``
is never resumed (owner answer, K10): it only moves to ``STOPPED`` once the
account is flat. ``STOPPED`` is final. One account has at most one experiment
that is not ``STOPPED``; the ``experiment_active`` row enforces that (K16).
Only trader_service writes these tables.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Literal, Mapping, Optional

from trader.automation.ai_paper_config import KILL_BASES, SUPPORTED_STYLES
from trader.data.schema_migrations import SchemaMigrator

EXPERIMENT_MIGRATION_VERSION = 70

ExperimentState = Literal["ARMED", "PAUSED", "KILLED", "STOPPED"]
STATES = ("ARMED", "PAUSED", "KILLED", "STOPPED")
ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("ARMED", "PAUSED"), ("PAUSED", "ARMED"), ("ARMED", "KILLED"), ("PAUSED", "KILLED"),
    ("ARMED", "STOPPED"), ("PAUSED", "STOPPED"), ("KILLED", "STOPPED"),
})
# The only columns a transition or kill progress may change besides the state (K10:
# kill_anchor_net_liquidation is set once at insert).
KILL_COLUMNS = frozenset({
    "killed_at", "kill_seq", "kill_round", "kill_root_id", "kill_flatten_root",
    "kill_net_liquidation", "kill_observed_drawdown_pct", "kill_generation_id",
    "kill_flat_state", "kill_flat_generation", "kill_flat_at", "kill_alert_state",
    "kill_session_end_state", "peak_net_liquidation", "pause_cause", "pause_generation_id",
})
FLAT_STATES = ("PENDING", "FLAT", "FAILED_SAFE")
ALERT_STATES = ("PENDING", "ENQUEUED", "NO_OUTBOX")
SESSION_END_STATES = ("PENDING", "RECORDED", "NO_SINK")
PAUSE_CAUSES = ("BROKER_DATA_OUTAGE",)
FX_SOURCES = ("base_is_usd", "ib_account_values")
_EXPERIMENT_ID = re.compile(r"exp-[0-9a-f]{20}")
_CURRENCY = re.compile(r"[A-Z]{3}")


def apply_experiment_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(EXPERIMENT_MIGRATION_VERSION, "sp1_experiments", (
        """CREATE TABLE IF NOT EXISTS experiments (
            experiment_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
            start_command_id VARCHAR NOT NULL UNIQUE, started_at TIMESTAMPTZ NOT NULL,
            start_net_liquidation DOUBLE NOT NULL, start_generation_id BIGINT NOT NULL,
            base_currency VARCHAR NOT NULL, start_usd_per_base DOUBLE NOT NULL, start_fx_source VARCHAR NOT NULL,
            config_digest VARCHAR NOT NULL, styles_json VARCHAR NOT NULL,
            kill_drawdown_pct DOUBLE, kill_basis VARCHAR NOT NULL,
            state VARCHAR NOT NULL, revision INTEGER NOT NULL,
            peak_net_liquidation DOUBLE NOT NULL, kill_anchor_net_liquidation DOUBLE NOT NULL,
            killed_at TIMESTAMPTZ, kill_seq INTEGER NOT NULL, kill_round INTEGER NOT NULL,
            kill_root_id VARCHAR, kill_flatten_root VARCHAR,
            kill_net_liquidation DOUBLE, kill_observed_drawdown_pct DOUBLE, kill_generation_id BIGINT,
            kill_flat_state VARCHAR, kill_flat_generation BIGINT, kill_flat_at TIMESTAMPTZ,
            kill_alert_state VARCHAR, kill_session_end_state VARCHAR,
            pause_cause VARCHAR, pause_generation_id BIGINT,
            stopped_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS experiment_active (
            account_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL UNIQUE)""",
        """CREATE TABLE IF NOT EXISTS experiment_transitions (
            experiment_id VARCHAR NOT NULL, revision INTEGER NOT NULL,
            from_state VARCHAR, to_state VARCHAR NOT NULL, principal VARCHAR NOT NULL,
            command_id VARCHAR, reason VARCHAR NOT NULL, detail_json VARCHAR NOT NULL,
            "at" TIMESTAMPTZ NOT NULL, PRIMARY KEY (experiment_id, revision))""",
    ))


class ExperimentRefused(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code


def experiment_id_for(account_id: str, command_id: str) -> str:
    digest = hashlib.sha256(f"{account_id}\x00{command_id}".encode("utf-8")).hexdigest()
    return "exp-" + digest[:20]


def is_experiment_id(value: object) -> bool:
    return type(value) is str and _EXPERIMENT_ID.fullmatch(value) is not None


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.timezone.utc)


def _is_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _positive(name: str, value: object) -> float:
    if not _is_number(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number, got {value!r}")
    return float(value)


def _optional_positive(name: str, value: object) -> Optional[float]:
    return None if value is None else _positive(name, value)


def _count(name: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


def _optional_count(name: str, value: object) -> Optional[int]:
    return None if value is None else _count(name, value)


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(name: str, value: object) -> Optional[str]:
    return None if value is None else _text(name, value)


def _moment(name: str, value: object) -> dt.datetime:
    if type(value) is not dt.datetime or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return _as_utc(value)


def _optional_moment(name: str, value: object) -> Optional[dt.datetime]:
    return None if value is None else _moment(name, value)


def _member(name: str, value: object, allowed: tuple, *, optional: bool = True) -> Any:
    if value is None and optional:
        return None
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
    return value


@dataclass(frozen=True)
class ExperimentRecord:
    # Plan 5 A1 fields first, same names and types.
    experiment_id: str
    account_id: str
    started_at: dt.datetime
    start_net_liquidation: float
    base_currency: str
    start_usd_per_base: float
    state: str
    killed_at: Optional[dt.datetime]
    start_command_id: str
    start_generation_id: int
    start_fx_source: str
    config_digest: str
    styles: tuple[str, ...]
    kill_drawdown_pct: Optional[float]
    kill_basis: str
    revision: int
    peak_net_liquidation: float
    kill_anchor_net_liquidation: float
    kill_seq: int = 0
    kill_round: int = 0
    kill_root_id: Optional[str] = None
    kill_flatten_root: Optional[str] = None
    kill_net_liquidation: Optional[float] = None
    kill_observed_drawdown_pct: Optional[float] = None
    kill_generation_id: Optional[int] = None
    kill_flat_state: Optional[str] = None
    kill_flat_generation: Optional[int] = None
    kill_flat_at: Optional[dt.datetime] = None
    kill_alert_state: Optional[str] = None
    kill_session_end_state: Optional[str] = None
    stopped_at: Optional[dt.datetime] = None
    pause_cause: Optional[str] = None
    pause_generation_id: Optional[int] = None

    def __post_init__(self):
        set_ = lambda name, value: object.__setattr__(self, name, value)  # noqa: E731
        if not is_experiment_id(self.experiment_id):
            raise ValueError(f"experiment_id must match exp-<20 hex>, got {self.experiment_id!r}")
        _text("account_id", self.account_id)
        _text("start_command_id", self.start_command_id)
        _text("config_digest", self.config_digest)
        set_("started_at", _moment("started_at", self.started_at))
        for name in ("start_net_liquidation", "start_usd_per_base", "peak_net_liquidation",
                     "kill_anchor_net_liquidation"):
            set_(name, _positive(name, getattr(self, name)))
        if type(self.base_currency) is not str or not _CURRENCY.fullmatch(self.base_currency):
            raise ValueError(f"base_currency must be three upper-case letters, got {self.base_currency!r}")
        _member("state", self.state, STATES, optional=False)
        _member("start_fx_source", self.start_fx_source, FX_SOURCES, optional=False)
        if (type(self.styles) is not tuple or not self.styles
                or any(style not in SUPPORTED_STYLES for style in self.styles)):
            raise ValueError(f"styles must be a non-empty tuple of {sorted(SUPPORTED_STYLES)}")
        if self.kill_drawdown_pct is not None:
            if not _is_number(self.kill_drawdown_pct) or not 0 < self.kill_drawdown_pct < 100:
                raise ValueError("kill_drawdown_pct must be None or a percent strictly between 0 and 100")
            set_("kill_drawdown_pct", float(self.kill_drawdown_pct))
        _member("kill_basis", self.kill_basis, KILL_BASES, optional=False)
        _count("start_generation_id", self.start_generation_id)
        _count("revision", self.revision, minimum=1)
        _count("kill_seq", self.kill_seq)
        _count("kill_round", self.kill_round)
        _optional_text("kill_root_id", self.kill_root_id)
        _optional_text("kill_flatten_root", self.kill_flatten_root)
        for name in ("kill_net_liquidation",):
            set_(name, _optional_positive(name, getattr(self, name)))
        if self.kill_observed_drawdown_pct is not None:
            if not _is_number(self.kill_observed_drawdown_pct):
                raise ValueError("kill_observed_drawdown_pct must be a finite number")
            set_("kill_observed_drawdown_pct", float(self.kill_observed_drawdown_pct))
        for name in ("kill_generation_id", "kill_flat_generation", "pause_generation_id"):
            _optional_count(name, getattr(self, name))
        for name in ("killed_at", "kill_flat_at", "stopped_at"):
            set_(name, _optional_moment(name, getattr(self, name)))
        _member("kill_flat_state", self.kill_flat_state, FLAT_STATES)
        _member("kill_alert_state", self.kill_alert_state, ALERT_STATES)
        _member("kill_session_end_state", self.kill_session_end_state, SESSION_END_STATES)
        _member("pause_cause", self.pause_cause, PAUSE_CAUSES)


_COLUMNS = tuple(f.name for f in fields(ExperimentRecord) if f.name != "styles") + ("styles_json",)


def _row_values(record: ExperimentRecord, now: dt.datetime) -> list:
    values = [getattr(record, name) for name in _COLUMNS[:-1]]
    return values + [json.dumps(list(record.styles)), now]


def _record_from_row(row: tuple) -> ExperimentRecord:
    data = dict(zip(_COLUMNS, row))
    data["styles"] = tuple(json.loads(data.pop("styles_json")))
    for name in ("start_generation_id", "revision", "kill_seq", "kill_round", "kill_generation_id",
                 "kill_flat_generation", "pause_generation_id"):
        if data[name] is not None:
            data[name] = int(data[name])
    return ExperimentRecord(**data)


_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM experiments"
_INSERT = (f"INSERT INTO experiments ({', '.join(_COLUMNS)}, updated_at) "
           f"VALUES ({', '.join('?' for _ in _COLUMNS)}, ?)")


def _check_changes(changes: Mapping[str, Any]) -> None:
    unknown = sorted(set(changes) - KILL_COLUMNS)
    if unknown:
        raise ValueError(f"only kill, peak and pause columns may change here, not {unknown}")


class ExperimentStore:
    """Every write is one transaction under the journal's per-database lock."""

    def __init__(self, db: Any, account_id: str, now: Callable[[], dt.datetime]):
        self._db = db
        self._account_id = account_id
        self._now = now

    @property
    def account_id(self) -> str:
        return self._account_id

    def _now_utc(self) -> dt.datetime:
        return _as_utc(self._now())

    # -- reads ---------------------------------------------------------------

    @staticmethod
    def _get_in_tx(conn, experiment_id: str) -> Optional[ExperimentRecord]:
        row = conn.execute(f"{_SELECT} WHERE experiment_id = ?", [experiment_id]).fetchone()
        return None if row is None else _record_from_row(row)

    def get(self, experiment_id: str) -> Optional[ExperimentRecord]:
        return self._db.transaction(lambda conn: self._get_in_tx(conn, experiment_id))

    def latest(self) -> Optional[ExperimentRecord]:
        def read(conn):
            row = conn.execute(f"{_SELECT} WHERE account_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                               [self._account_id]).fetchone()
            return None if row is None else _record_from_row(row)
        return self._db.transaction(read)

    def active(self) -> Optional[ExperimentRecord]:
        def read(conn):
            row = conn.execute("SELECT experiment_id FROM experiment_active WHERE account_id = ?",
                               [self._account_id]).fetchone()
            return None if row is None else self._get_in_tx(conn, row[0])
        return self._db.transaction(read)

    def transitions(self, experiment_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT revision, from_state, to_state, principal, command_id, reason, detail_json, \"at\" "
            "FROM experiment_transitions WHERE experiment_id = ? ORDER BY revision", [experiment_id], fetch="all")
        keys = ("revision", "from_state", "to_state", "principal", "command_id", "reason", "detail", "at")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            item["detail"] = json.loads(item["detail"])
            item["at"] = _as_utc(item["at"])
            out.append(item)
        return out

    # -- writes --------------------------------------------------------------

    def insert_armed(self, record: ExperimentRecord, *, principal: str, reason: str) -> ExperimentRecord:
        if record.state != "ARMED" or record.revision != 1 or record.account_id != self._account_id:
            raise ValueError("insert_armed takes a revision-1 ARMED record of this account")
        now = self._now_utc()

        def tx(conn):
            own = self._get_in_tx(conn, record.experiment_id)
            if own is not None:
                return own
            active = conn.execute("SELECT experiment_id FROM experiment_active WHERE account_id = ?",
                                  [record.account_id]).fetchone()
            if active is not None:
                raise ExperimentRefused("EXPERIMENT_ACTIVE", f"experiment {active[0]} is not stopped")
            conn.execute(_INSERT, _row_values(record, now))
            conn.execute("INSERT INTO experiment_active VALUES (?, ?)", [record.account_id, record.experiment_id])
            self._append_in_tx(conn, record.experiment_id, 1, None, "ARMED", principal,
                               record.start_command_id, reason, {}, now)
            return self._get_in_tx(conn, record.experiment_id)
        try:
            return self._db.transaction(tx)
        except ExperimentRefused:
            raise
        except Exception as exc:
            if "constraint" in str(exc).lower():   # lost a race on experiment_active
                raise ExperimentRefused("EXPERIMENT_ACTIVE", "another experiment was armed first") from exc
            raise

    def transition(self, experiment_id: str, *, expected: frozenset[str], to: str, principal: str,
                   command_id: Optional[str], reason: str,
                   changes: Mapping[str, Any] = {}) -> ExperimentRecord:  # noqa: B006 - read only
        _check_changes(changes)
        _member("to", to, STATES, optional=False)
        now = self._now_utc()

        def tx(conn):
            current = self._require_in_tx(conn, experiment_id)
            if current.state not in expected:
                raise ExperimentRefused("EXPERIMENT_STATE_CHANGED",
                                        f"experiment {experiment_id} is {current.state}")
            if (current.state, to) not in ALLOWED_TRANSITIONS:
                raise ExperimentRefused("ILLEGAL_TRANSITION", f"{current.state} -> {to} is not allowed")
            extra = {"stopped_at": now} if to == "STOPPED" else {}
            updated = replace(current, state=to, revision=current.revision + 1, **changes, **extra)
            self._write_in_tx(conn, updated, now, expect_state=current.state, expect_revision=current.revision)
            if to == "STOPPED":
                conn.execute("DELETE FROM experiment_active WHERE experiment_id = ?", [experiment_id])
            self._append_in_tx(conn, experiment_id, updated.revision, current.state, to, principal,
                               command_id, reason, dict(changes), now)
            return self._get_in_tx(conn, experiment_id)
        return self._db.transaction(tx)

    def update_kill_progress(self, experiment_id: str, *, expected_state: str,
                             changes: Mapping[str, Any]) -> ExperimentRecord:
        _check_changes(changes)
        now = self._now_utc()

        def tx(conn):
            current = self._require_in_tx(conn, experiment_id)
            if current.state != expected_state:
                raise ExperimentRefused("EXPERIMENT_STATE_CHANGED",
                                        f"experiment {experiment_id} is {current.state}")
            updated = replace(current, **changes)
            self._write_in_tx(conn, updated, now, expect_state=current.state, expect_revision=current.revision)
            return self._get_in_tx(conn, experiment_id)
        return self._db.transaction(tx)

    def raise_peak(self, experiment_id: str, net_liquidation: float) -> float:
        value = _positive("net_liquidation", net_liquidation)
        now = self._now_utc()

        def tx(conn):
            current = self._require_in_tx(conn, experiment_id)
            if value > current.peak_net_liquidation:
                conn.execute("UPDATE experiments SET peak_net_liquidation = ?, updated_at = ? "
                             "WHERE experiment_id = ?", [value, now, experiment_id])
                return value
            return current.peak_net_liquidation
        return self._db.transaction(tx)

    # -- helpers -------------------------------------------------------------

    def _require_in_tx(self, conn, experiment_id: str) -> ExperimentRecord:
        current = self._get_in_tx(conn, experiment_id)
        if current is None or current.account_id != self._account_id:
            raise ExperimentRefused("NO_EXPERIMENT", f"no experiment {experiment_id}")
        return current

    @staticmethod
    def _write_in_tx(conn, record: ExperimentRecord, now: dt.datetime, *, expect_state: str,
                     expect_revision: int) -> None:
        mutable = ("state", "revision", "stopped_at", *sorted(KILL_COLUMNS))
        assignments = ", ".join(f"{name} = ?" for name in mutable)
        changed = conn.execute(
            f"UPDATE experiments SET {assignments}, updated_at = ? "
            "WHERE experiment_id = ? AND state = ? AND revision = ? RETURNING experiment_id",
            [getattr(record, name) for name in mutable] + [now, record.experiment_id, expect_state,
                                                            expect_revision]).fetchall()
        if not changed:
            raise ExperimentRefused("EXPERIMENT_STATE_CHANGED", f"experiment {record.experiment_id} changed")

    @staticmethod
    def _append_in_tx(conn, experiment_id, revision, from_state, to_state, principal, command_id,
                      reason, detail, now) -> None:
        conn.execute("INSERT INTO experiment_transitions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [experiment_id, revision, from_state, to_state, principal, command_id, reason,
                      json.dumps(detail, default=str, sort_keys=True), now])
