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
import logging
import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from trader.authored.keys import authored_key, source_digest
from trader.automation.ai_deployment_activity import EXPIRED as VERSION_EXPIRED
from trader.automation.ai_deployment_activity import (
    ACTIVE, JUDGMENT_ENDED, NOT_STARTED, OVER_CAP, SUPERSEDED, WITHDRAWN,
)
from trader.automation.ai_deployment_versions import WITHDRAWAL_ENTRY_IN_FLIGHT
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_refusals import (
    AUTHORED_CAP_FULL, AUTHORED_STRATEGIES_DISABLED, AUTHORED_STRATEGY_UNKNOWN, AuthoredRefused,
    AuthoredStateConflict,
)
from trader.automation.authored_submission import check_submission
from trader.automation.authored_wire import (
    ReportAuthoredEvaluationRequest, RetireAuthoredStrategyRequest, SubmitAuthoredStrategyRequest,
)
from trader.automation.backtest_judgments import JudgmentRefused, nth_session_after
from trader.automation.evaluation_claims import ny_day, utc
from trader.research.canonical import canonical_json_bytes

logger = logging.getLogger(__name__)

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


# Ruling 18 and decision D4: a retired record's version is withdrawn through the SP2c path, as the strategy.
AUTHORED_RETIRE_REASON = "AUTHORED_SANDBOX_ERROR"


def authored_retire_command_id(version_digest: str) -> str:
    """One withdrawal per version: a resend reuses the id, and SP2c answers an already withdrawn version False."""
    return f"authored-retire:{version_digest}"


def withdraw_with(versions: Any) -> Callable[[str], bool]:
    """The ``withdraw`` port of AuthoredStrategyStore over SP2c's AiDeploymentVersionStore."""
    def withdraw(version_digest: str) -> bool:
        return versions.withdraw(version_digest, reason=AUTHORED_RETIRE_REASON, principal="strategy",
                                 command_id=authored_retire_command_id(version_digest))
    return withdraw


VERSION_SUPERSEDED = "VERSION_SUPERSEDED"          # SP2c withdraw's refusal of a renewed version
# Version statuses of a line that has not ended: a retirement withdraws its last version.
_TRADING_STATUSES = frozenset({ACTIVE, NOT_STARTED, OVER_CAP})

# Ruling 11: where a DEPLOYED record goes when its version line ends.
_LINE_ENDINGS: Mapping[str, tuple[AuthoredState, str]] = MappingProxyType({
    WITHDRAWN: (RETIRED, OWNER_RETIRED),
    VERSION_EXPIRED: (EXPIRED, DEPLOY_EXPIRED),
    JUDGMENT_ENDED: (EXPIRED, RENEWAL_ENDED),
})


def _already_reported(record: AuthoredRecord, request: ReportAuthoredEvaluationRequest) -> bool:
    """Ruling 17: the same event for the same request (and image) was applied before."""
    return any(entry.get("event") == request.event and entry.get("request_id") == request.request_id
               and entry.get("image") == request.sandbox_image_digest for entry in record.history)


def _apply_report(conn: Any, record: AuthoredRecord, request: ReportAuthoredEvaluationRequest,
                  now: dt.datetime) -> AuthoredRecord:
    detail = request.detail or None
    if request.event == "STARTED":
        return note_in_tx(conn, record, event="STARTED", now=now, request_id=request.request_id, detail=detail,
                          image=request.sandbox_image_digest, sandbox_image_digest=request.sandbox_image_digest,
                          sandbox_image_seen_at=utc(now))
    if request.event == "SANDBOX_ERROR":
        return move_in_tx(conn, record, REJECTED, event="SANDBOX_ERROR", reason=SANDBOX_ERROR, now=now,
                          request_id=request.request_id, detail=detail, evaluation_request_id=None)
    attempts = record.attempts + 1
    target, reason = (FAILED, SANDBOX_UNAVAILABLE) if attempts >= UNAVAILABLE_ATTEMPTS else (STORED, None)
    return move_in_tx(conn, record, target, event="SANDBOX_UNAVAILABLE", reason=reason, now=now,
                      request_id=request.request_id, detail=detail, attempts=attempts, evaluation_request_id=None)


def _existing_in_tx(conn: Any, digest: str) -> AuthoredRecord:
    record = record_in_tx(conn, digest)
    if record is None:
        raise AuthoredRefused(AUTHORED_STRATEGY_UNKNOWN, f"no authored strategy has digest {digest}")
    return record


class DeploymentLines:
    """Where an authored deployment's version line ends: its last version after renewals (ruling 11)."""

    def __init__(self, *, activity: Any, versions: Any):
        self._activity = activity
        self._versions = versions

    def end_status(self, version_digest: str) -> Optional[str]:
        return self._last_version(version_digest)[1]

    def live_version(self, version_digest: str) -> Optional[str]:
        """The line's last version while it may still trade; None once the line has ended or is unknown."""
        last, status = self._last_version(version_digest)
        return last if status in _TRADING_STATUSES else None

    def _last_version(self, version_digest: str) -> tuple[str, Optional[str]]:
        statuses = self._activity.statuses()
        renewed_by = {sealed.version.prior_version: sealed.digest for sealed in self._versions.sealed()
                      if sealed.version.prior_version is not None}
        current, seen = version_digest, set()
        while statuses.get(current) == SUPERSEDED and current in renewed_by and current not in seen:
            seen.add(current)
            current = renewed_by[current]
        return current, statuses.get(current)


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
            self.refresh()
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
        self.refresh()
        records, judged = self._db.transaction(lambda conn: (records_in_tx(conn), judged_count_in_tx(conn)))
        counted = sum(1 for record in records if record.state in COUNTED_STATES)
        return {"enabled": self._config.enabled, "max_active": self._config.max_active, "counted": counted,
                "room": max(0, self._config.max_active - counted), "judged_count": judged,
                "bar_sizes": list(self._config.bar_sizes), "records": [record.to_json() for record in records]}

    def report(self, request: ReportAuthoredEvaluationRequest) -> dict:
        """Research's facts about one evaluation (spec 7.6, 8; ruling 17). The switch is not checked: an
        evaluation already claimed must finish (ruling 8)."""
        now = utc(self._now())

        def write(conn: Any) -> tuple[str, AuthoredRecord, int]:
            record = _existing_in_tx(conn, request.digest)
            if _already_reported(record, request):
                return "UNCHANGED", record, judged_count_in_tx(conn)
            if record.state != EVALUATING or record.evaluation_request_id != request.request_id:
                raise AuthoredStateConflict(f"the record is {record.state.value} on {record.evaluation_request_id}; "
                                            f"the report names {request.request_id}")
            return "UPDATED", _apply_report(conn, record, request, now), judged_count_in_tx(conn)
        try:
            status, record, judged = self._db.transaction(write)
        except AuthoredRefused as refused:
            return refused.reply(judged_count=None)
        return _reply(status, record, judged_count=judged)

    def retire(self, request: RetireAuthoredStrategyRequest) -> dict:
        """The strategy service retires a DEPLOYED record after a live fault (spec 7.6, 8; ruling 18).

        The record moves first, so entries are refused at once (ruling 16) and the reason is SANDBOX_ERROR even
        if the trader dies before the next step. Then the version is withdrawn through the SP2c path (D4), so
        the line ends WITHDRAWN and a restart never lists it. A withdrawal refused while an entry is being sent
        is a retryable refusal; the strategy service resends until UPDATED or UNCHANGED (Plan 4 ruling 18)."""
        now = utc(self._now())

        def write(conn: Any) -> tuple[str, AuthoredRecord]:
            record = _existing_in_tx(conn, request.digest)
            if record.state == RETIRED:
                return "UNCHANGED", record
            if record.state != DEPLOYED:
                raise AuthoredStateConflict(
                    f"only a DEPLOYED authored strategy retires; this one is {record.state.value}")
            return "UPDATED", move_in_tx(conn, record, RETIRED, event="RETIRED_BY_STRATEGY", reason=SANDBOX_ERROR,
                                         now=now, detail=f"{request.code}: {request.detail}")
        try:
            status, record = self._db.transaction(write)
        except AuthoredRefused as refused:
            return refused.reply()
        version = self._version_to_withdraw(record)
        if version is not None and self._withdraw is not None:
            try:
                self._withdraw(version)
            except DeploymentRefused as refused:
                logger.error("authored strategy %s is RETIRED; withdrawing %s refused: %s", record.digest,
                             version, refused)
                not_withdrawn = AuthoredRefused(
                    refused.code, f"the record is RETIRED; its version is not withdrawn yet: {refused.message}",
                    retryable=self._withdrawal_retryable(refused.code))
                return {**not_withdrawn.reply(), "record": record.to_json()}
        return _reply(status, record)

    def _withdrawal_retryable(self, code: str) -> bool:
        """A resend finds a newer last version only through ``lines``; without them it would repeat forever."""
        return code == WITHDRAWAL_ENTRY_IN_FLIGHT or (code == VERSION_SUPERSEDED and self._lines is not None)

    def _version_to_withdraw(self, record: AuthoredRecord) -> Optional[str]:
        """The line's current version, not the registered one: a renewal may have replaced it (the trader does
        not rely on Plan 4 never renewing an authored line). None when the line already ended. Without
        ``lines`` the registered version is the only one known."""
        if record.deployment_version is None or self._lines is None:
            return record.deployment_version
        return self._lines().live_version(record.deployment_version)

    def refresh(self) -> None:
        """Ruling 11: a SHADOW or DEPLOYED record leaves the count when its window ends. Other stores are read
        first, outside the transaction; each record moves only if it did not move meanwhile. A line the strategy
        retired is withdrawn again while it still has a live version (its first withdrawal may have been lost)."""
        now = utc(self._now())
        records = self._db.transaction(records_in_tx)
        for record in records:
            if record.state == RETIRED and record.reason == SANDBOX_ERROR:
                self._withdraw_again(record)
        endings = [(record, ending) for record in records if (ending := self._ending(record, now)) is not None]
        if not endings:
            return

        def write(conn: Any) -> None:
            for record, (target, reason) in endings:
                current = record_in_tx(conn, record.digest)
                if current is not None and current.state == record.state \
                        and current.state_changed_at == record.state_changed_at:
                    move_in_tx(conn, current, target, event="WINDOW_ENDED", reason=reason, now=now)
        self._db.transaction(write)

    def _withdraw_again(self, record: AuthoredRecord) -> None:
        """Best effort: the record stays RETIRED whatever happens, and a failure never breaks a read or a submit.
        Its entries are refused by the RETIRED state already; this frees the version's SP2c slot."""
        if self._lines is None or self._withdraw is None or record.deployment_version is None:
            return
        try:
            version = self._lines().live_version(record.deployment_version)
            if version is not None:
                self._withdraw(version)
        except DeploymentRefused as refused:
            if refused.code != VERSION_SUPERSEDED:   # superseded: the next refresh finds the newer version
                logger.error("authored strategy %s is RETIRED; withdrawing its line again refused: %s",
                             record.digest, refused)
        except Exception:
            logger.exception("authored strategy %s is RETIRED; withdrawing its line again failed", record.digest)

    def _ending(self, record: AuthoredRecord, now: dt.datetime) -> Optional[tuple[AuthoredState, str]]:
        if record.state == SHADOW:
            return self._shadow_ending(record, now)
        if record.state == DEPLOYED and record.deployment_version is not None and self._lines is not None:
            return _LINE_ENDINGS.get(self._lines().end_status(record.deployment_version))
        return None

    def _shadow_ending(self, record: AuthoredRecord, now: dt.datetime) -> Optional[tuple[AuthoredState, str]]:
        if self._calendar is None:
            return None
        try:
            last = nth_session_after(self._calendar, ny_day(record.state_changed_at), self._shadow_sessions)
        except JudgmentRefused as refused:          # the calendar cannot serve: stay counted, the safe side
            logger.error("authored strategy %s stays SHADOW: %s", record.digest, refused.detail)
            return None
        return (EXPIRED, SHADOW_EXPIRED) if ny_day(now) > last else None

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
