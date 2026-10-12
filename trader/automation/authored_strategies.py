"""Authored-strategy records, their states and the cap (SP3 spec 7.3, 8).

One journal row per digest. ``COUNTED_STATES`` count toward ``ai_paper.authored_strategies.max_active``;
every other state is terminal. A row moves only along ``TRANSITIONS`` and each move appends to its
history. A move or note is compare-and-set: it changes the row only while the row still holds the state
and history of the record the caller read, so a stale record can never overwrite a newer one. Functions
ending ``_in_tx`` run inside the caller's ``DuckDBConnection.transaction`` and never call the store's own
``execute`` or ``transaction``.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from trader.authored.keys import authored_key, source_digest
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_refusals import (
    AUTHORED_CAP_FULL, AUTHORED_STRATEGIES_DISABLED, AuthoredRefused, AuthoredStateConflict,
)
from trader.automation.authored_submission import check_submission
from trader.automation.authored_wire import SubmitAuthoredStrategyRequest
from trader.automation.evaluation_claims import utc
from trader.research.canonical import canonical_json_bytes

FILE_SCHEMA = "mmr.authored-strategy.v1"


class AuthoredState(StrEnum):
    STORED = "STORED"
    EVALUATING = "EVALUATING"
    SHADOW = "SHADOW"
    DEPLOYED = "DEPLOYED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


STORED, EVALUATING, SHADOW, DEPLOYED = (
    AuthoredState.STORED, AuthoredState.EVALUATING, AuthoredState.SHADOW, AuthoredState.DEPLOYED)
REJECTED, RETIRED, EXPIRED, FAILED = (
    AuthoredState.REJECTED, AuthoredState.RETIRED, AuthoredState.EXPIRED, AuthoredState.FAILED)
COUNTED_STATES = frozenset({STORED, EVALUATING, SHADOW, DEPLOYED})
# Spec 7.3, plus SHADOW -> DEPLOYED for a DEPLOY verdict that waited for an isolation proof (spec 8, ruling 10).
TRANSITIONS: Mapping[AuthoredState, frozenset[AuthoredState]] = MappingProxyType({
    STORED: frozenset({EVALUATING}),
    EVALUATING: frozenset({REJECTED, SHADOW, DEPLOYED, STORED, FAILED}),
    SHADOW: frozenset({RETIRED, EXPIRED, DEPLOYED}),
    DEPLOYED: frozenset({RETIRED, EXPIRED}),
})
UNAVAILABLE_ATTEMPTS = 3

# Reasons a record carries (spec 7.3; rulings 9 and 11).
SANDBOX_ERROR = "SANDBOX_ERROR"
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
JEV_REJECT = "JEV_REJECT"
JEV_SHADOW = "JEV_SHADOW"
JEV_NO_VERDICT = "JEV_NO_VERDICT"
AWAITING_REGISTRATION = "AWAITING_REGISTRATION"
OWNER_RETIRED = "OWNER_RETIRED"
SHADOW_EXPIRED = "SHADOW_EXPIRED"
DEPLOY_EXPIRED = "DEPLOY_EXPIRED"
RENEWAL_ENDED = "RENEWAL_ENDED"

_COLUMNS = ("digest", "class_name", "bar_size", "tunables_json", "note", "author_model", "state", "reason",
            "attempts", "evaluation_request_id", "sandbox_image_digest", "sandbox_image_seen_at", "verdict",
            "judgment_id", "deployment_version", "created_at", "state_changed_at", "history_json")
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM authored_strategies"
# What a move or a note may change beside its own state, reason and history; identity never changes.
CHANGEABLE_FIELDS = frozenset({"attempts", "evaluation_request_id", "sandbox_image_digest", "sandbox_image_seen_at",
                               "verdict", "judgment_id", "deployment_version"})


@dataclass(frozen=True)
class AuthoredRecord:
    digest: str
    class_name: str
    bar_size: str
    tunables: Mapping[str, Mapping[str, float]]
    note: str
    author_model: str
    state: AuthoredState
    reason: Optional[str]
    attempts: int
    evaluation_request_id: Optional[str]
    sandbox_image_digest: Optional[str]
    sandbox_image_seen_at: Optional[dt.datetime]
    verdict: Optional[str]
    judgment_id: Optional[str]
    deployment_version: Optional[str]
    created_at: dt.datetime
    state_changed_at: dt.datetime
    history: tuple[dict, ...]

    @property
    def key(self) -> str:
        return authored_key(self.digest, self.class_name)

    def _tunables_json(self) -> dict:
        return {name: dict(bounds) for name, bounds in self.tunables.items()}

    def to_json(self) -> dict:
        return {"digest": self.digest, "key": self.key, "class_name": self.class_name, "bar_size": self.bar_size,
                "tunables": self._tunables_json(), "note": self.note, "author_model": self.author_model,
                "state": self.state.value, "reason": self.reason, "attempts": self.attempts,
                "evaluation_request_id": self.evaluation_request_id,
                "sandbox_image_digest": self.sandbox_image_digest, "verdict": self.verdict,
                "judgment_id": self.judgment_id, "deployment_version": self.deployment_version,
                "created_at": self.created_at.isoformat(), "state_changed_at": self.state_changed_at.isoformat(),
                "history": [dict(entry) for entry in self.history]}

    def file_metadata(self) -> dict:
        """The ``<hex>.json`` beside the file (spec 7.3). States live in the journal only."""
        return {"schema": FILE_SCHEMA, "digest": self.digest, "class_name": self.class_name,
                "tunables": self._tunables_json(), "bar_size": self.bar_size, "note": self.note,
                "author_model": self.author_model, "created_at": self.created_at.isoformat()}


def _entry(now: dt.datetime, *, event: str, source: Optional[AuthoredState], target: AuthoredState,
           reason: Optional[str], request_id: Optional[str] = None, detail: Optional[str] = None,
           image: Optional[str] = None) -> dict:
    return {"at": now.isoformat(), "event": event, "from": None if source is None else source.value,
            "to": target.value, "reason": reason, "request_id": request_id, "detail": detail, "image": image}


def new_record(*, digest: str, class_name: str, bar_size: str, tunables: Mapping[str, Mapping[str, float]],
               note: str, author_model: str, now: dt.datetime) -> AuthoredRecord:
    now = utc(now)
    entry = _entry(now, event="SUBMITTED", source=None, target=STORED, reason=None)
    return AuthoredRecord(digest, class_name, bar_size, {n: dict(b) for n, b in tunables.items()}, note,
                          author_model, STORED, None, 0, None, None, None, None, None, None, now, now, (entry,))


def _text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _values(record: AuthoredRecord) -> list:
    return [record.digest, record.class_name, record.bar_size, _text(record._tunables_json()), record.note,
            record.author_model, record.state.value, record.reason, record.attempts, record.evaluation_request_id,
            record.sandbox_image_digest, record.sandbox_image_seen_at, record.verdict, record.judgment_id,
            record.deployment_version, record.created_at, record.state_changed_at, _text(list(record.history))]


def _record(row: tuple) -> AuthoredRecord:
    seen_at = None if row[11] is None else utc(row[11])
    return AuthoredRecord(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5], AuthoredState(row[6]),
                          row[7], row[8], row[9], row[10], seen_at, row[12], row[13], row[14], utc(row[15]),
                          utc(row[16]), tuple(json.loads(row[17])))


def record_in_tx(conn: Any, digest: str) -> Optional[AuthoredRecord]:
    row = conn.execute(f"{_SELECT} WHERE digest = ?", [digest]).fetchone()
    return None if row is None else _record(row)


def records_in_tx(conn: Any) -> tuple[AuthoredRecord, ...]:
    return tuple(_record(row) for row in conn.execute(f"{_SELECT} ORDER BY created_at, digest").fetchall())


def counted_in_tx(conn: Any) -> int:
    states = sorted(state.value for state in COUNTED_STATES)
    marks = ", ".join("?" for _ in states)
    return conn.execute(f"SELECT COUNT(*) FROM authored_strategies WHERE state IN ({marks})", states).fetchone()[0]


def judged_count_in_tx(conn: Any) -> int:
    """Spec 7.7 and ruling 19: authored strategies that reached a judgment, whatever the verdict."""
    return conn.execute("SELECT COUNT(*) FROM authored_strategies WHERE verdict IS NOT NULL").fetchone()[0]


def insert_in_tx(conn: Any, record: AuthoredRecord) -> None:
    marks = ", ".join("?" for _ in _COLUMNS)
    conn.execute(f"INSERT INTO authored_strategies ({', '.join(_COLUMNS)}) VALUES ({marks})", _values(record))


def _require_changeable(changes: Mapping[str, Any]) -> None:
    refused = sorted(set(changes) - CHANGEABLE_FIELDS)
    if refused:
        raise ValueError(f"a move or note cannot change {', '.join(refused)}")


def _replace_in_tx(conn: Any, expected: AuthoredRecord, updated: AuthoredRecord) -> None:
    assignments = ", ".join(f"{name} = ?" for name in _COLUMNS[1:])
    changed = conn.execute(
        f"UPDATE authored_strategies SET {assignments} WHERE digest = ? AND state = ? AND history_json = ? "
        "RETURNING digest",
        _values(updated)[1:] + [expected.digest, expected.state.value, _text(list(expected.history))]).fetchall()
    if not changed:
        raise AuthoredStateConflict(f"{expected.digest} is no longer {expected.state.value} as read "
                                    f"(history of {len(expected.history)} entries)")


def move_in_tx(conn: Any, record: AuthoredRecord, target: AuthoredState, *, event: str, reason: Optional[str],
               now: dt.datetime, request_id: Optional[str] = None, detail: Optional[str] = None,
               **changes: Any) -> AuthoredRecord:
    _require_changeable(changes)
    if target not in TRANSITIONS.get(record.state, frozenset()):
        raise AuthoredStateConflict(f"{record.state.value} cannot move to {target.value}")
    now = utc(now)
    entry = _entry(now, event=event, source=record.state, target=target, reason=reason, request_id=request_id,
                   detail=detail)
    moved = replace(record, state=target, reason=reason, state_changed_at=now, history=record.history + (entry,),
                    **changes)
    _replace_in_tx(conn, record, moved)
    return moved


def note_in_tx(conn: Any, record: AuthoredRecord, *, event: str, now: dt.datetime,
               request_id: Optional[str] = None, detail: Optional[str] = None, image: Optional[str] = None,
               **changes: Any) -> AuthoredRecord:
    """A history entry without a state change (research's STARTED report)."""
    _require_changeable(changes)
    entry = _entry(utc(now), event=event, source=record.state, target=record.state, reason=record.reason,
                   request_id=request_id, detail=detail, image=image)
    noted = replace(record, history=record.history + (entry,), **changes)
    _replace_in_tx(conn, record, noted)
    return noted


def _reply(status: str, record: AuthoredRecord, **extra: Any) -> dict:
    return {"status": status, "code": None, "detail": None, "retryable": False, "record": record.to_json(),
            **extra}


class _DigestLocks:
    """One lock per digest while someone holds or waits for it; the entry is dropped after the last one."""

    def __init__(self):
        self._guard = threading.Lock()
        self._held: dict[str, tuple[threading.Lock, int]] = {}

    @contextlib.contextmanager
    def hold(self, digest: str) -> Iterator[None]:
        with self._guard:
            lock, users = self._held.get(digest, (threading.Lock(), 0))
            self._held[digest] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._guard:
                lock, users = self._held[digest]
                if users == 1:
                    del self._held[digest]
                else:
                    self._held[digest] = (lock, users - 1)


class AuthoredStrategyStore:
    """The trader's authority over authored strategies (SP3 spec 7.3). One instance per trader."""

    def __init__(self, db: Any, *, config: AuthoredStrategiesConfig, files: AuthoredFileStore,
                 now: Callable[[], dt.datetime], proofs: Any = None, calendar: Any = None,
                 shadow_sessions: int = 20, lines: Optional[Callable[[], Any]] = None,
                 withdraw: Optional[Callable[[str], bool]] = None):
        """``proofs`` (IsolationProofs) gates registration; ``calendar``, ``shadow_sessions`` and ``lines``
        (a callable returning DeploymentLines) drive the lazy expiry of ruling 11; ``withdraw`` (the SP2c
        version withdrawal, Task 6) ends a retired record's deployment line (ruling 18, decision D4)."""
        self._db = db
        self._config = config
        self._files = files
        self._now = now
        self._proofs = proofs
        self._calendar = calendar
        self._shadow_sessions = shadow_sessions
        self._lines = lines
        self._withdraw = withdraw
        self._submit_locks = _DigestLocks()

    @property
    def config(self) -> AuthoredStrategiesConfig:
        return self._config

    def get(self, digest: str) -> Optional[AuthoredRecord]:
        return self._db.transaction(lambda conn: record_in_tx(conn, digest))

    def submit(self, request: SubmitAuthoredStrategyRequest) -> dict:
        """Two submits of one digest run one after the other, so the second finds the first's record and
        never writes its own ``.json`` beside it. Different digests still race for the cap, which the
        insert transaction counts."""
        source = request.source.encode("utf-8")
        digest = source_digest(source)
        with self._submit_locks.hold(digest):
            return self._submit_held(request, source, digest)

    def _submit_held(self, request: SubmitAuthoredStrategyRequest, source: bytes, digest: str) -> dict:
        existing = self.get(digest)
        if existing is not None:                  # ruling 6: before the switch, the cap and the checks
            return _reply("EXISTING", existing)
        try:
            self._require_enabled()
            self._require_room()
            checked = check_submission(request, self._config)
            record = new_record(digest=digest, class_name=checked.class_name, bar_size=checked.bar_size,
                                tunables=checked.tunables, note=request.note, author_model=request.author_model,
                                now=self._now())
            self._files.write(digest, source, record.file_metadata())      # the file before the record
            status, saved = self._db.transaction(lambda conn: self._insert_in_tx(conn, record))
        except AuthoredRefused as refused:
            return refused.reply()
        if status == "EXISTING":                  # another store instance committed the digest first
            self._files.write(digest, source, saved.file_metadata())
        return _reply(status, saved)

    def overview(self) -> dict:
        """``get_authored_strategies`` and ``list_authored_strategies``: every record, the cap and the switch."""
        records, judged = self._db.transaction(lambda conn: (records_in_tx(conn), judged_count_in_tx(conn)))
        counted = sum(1 for record in records if record.state in COUNTED_STATES)
        return {"enabled": self._config.enabled, "max_active": self._config.max_active, "counted": counted,
                "room": max(0, self._config.max_active - counted), "judged_count": judged,
                "bar_sizes": list(self._config.bar_sizes), "records": [record.to_json() for record in records]}

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise AuthoredRefused(AUTHORED_STRATEGIES_DISABLED, "ai_paper.authored_strategies.enabled is false")

    def _require_room(self) -> None:
        """A cheap check before the static check and the file write; the insert counts again."""
        counted = self._db.transaction(counted_in_tx)
        if counted >= self._config.max_active:
            raise AuthoredRefused(AUTHORED_CAP_FULL, self._cap_detail(counted))

    def _insert_in_tx(self, conn: Any, record: AuthoredRecord) -> tuple[str, AuthoredRecord]:
        raced = record_in_tx(conn, record.digest)
        if raced is not None:                     # the same file, submitted twice at once
            return "EXISTING", raced
        # Count and insert are safe only because every write to this journal path holds the single
        # DuckDBConnection instance's lock; DuckDB itself would not serialize two such transactions.
        counted = counted_in_tx(conn)
        if counted >= self._config.max_active:
            raise AuthoredRefused(AUTHORED_CAP_FULL, self._cap_detail(counted))
        insert_in_tx(conn, record)
        return "STORED", record

    def _cap_detail(self, counted: int) -> str:
        return f"{counted} of {self._config.max_active} authored strategies are STORED, EVALUATING, SHADOW or DEPLOYED"
