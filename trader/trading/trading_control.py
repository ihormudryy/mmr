"""Durable per-account pause gate. [M1-F3] Task 4.

``TradingControlStore`` is the single source of truth for whether an
account is allowed to take on NEW exposure. It lives in the same dedicated
``journal_duckdb_path`` file as ``trade_proposals`` (Task 1) and
``command_ledger``/``command_audit`` (Task 3) so a pause mutation and its
``trading_control.updated`` journal event commit together.

Pause semantics (binding, spec §9.4)
--------------------------------------
A pause/resume is an ABSOLUTE set, never a toggle:

- ``paused=True`` (pause) passes ``expected_revision=None`` and always
  succeeds against whatever the current row is -- pausing is risk-REDUCING,
  so it must be immediate and idempotent even from a stale dashboard view
  (a caller that hasn't refreshed in a while can still always stop new
  exposure).
- ``paused=False`` (resume) is risk-INCREASING and requires the caller to
  present the EXACT current ``revision`` -- a stale resume is rejected with
  ``PauseRevisionConflict`` rather than silently unpausing on top of a state
  the caller never actually saw.
- Setting the SAME state twice (pause-while-already-paused,
  resume-while-already-unpaused) is an idempotent no-op: it returns the
  current row UNCHANGED and mints no new revision, and -- because the
  no-op is detected before any write is attempted -- journals no event
  either (mirrors ``ProposalCommandService.reject_proposal``'s "already in
  the target terminal state" idiom).
- A missing row is never auto-seeded here: ``require_unpaused``/``get`` fail
  closed with ``PauseStateUnavailable`` (spec's "fail loudly, not
  silently" -- an ungoverned account must block exposure-increasing action,
  not silently allow it). Seeding is ``seed_in_tx``'s job alone, run once by
  ``trader_service`` before it reports ready.

Connection / transaction discipline
--------------------------------------
``seed_in_tx``, ``set_pause_in_tx``, and ``require_unpaused_in_tx`` all take
an EXISTING ``conn`` and run entirely inside the CALLER's transaction --
they never issue their own BEGIN/COMMIT. This is what lets Task 5's
approval saga call ``require_unpaused_in_tx`` inside the SAME transaction
that claims a proposal's ``SUBMITTING`` state: a pause that commits first is
guaranteed to be observed by that claim, serializing pause against final
order dispatch (spec I1).

The non-suffixed wrappers (``get``, ``require_unpaused``, ``set``) own the
transaction boundary themselves. ``set`` cannot go through
``DomainJournal.mutate`` for this: ``mutate`` unconditionally journals
exactly one event per call, but a same-state pause/resume must journal
NONE (the no-op contract above). So ``set``/``seed_in_tx`` manage a plain
``BEGIN``/``COMMIT``/``ROLLBACK`` directly (mirroring
``CommandLedger.purge_expired``'s identical idiom) and the actual event
write, when one is needed, goes through ``_journal_in_tx`` -- a thin
manual insert built on ``DomainJournal``'s own internal
revision-bookkeeping helpers (``_read_current_revision``,
``_upsert_materialized``, ``_insert_journal_row``). Reaching into those
(underscore-prefixed but same-package) helpers is deliberate: they are the
only way to co-locate "maybe write a row, maybe not" with "exactly one
event when a row really changed" inside a transaction ``mutate()`` doesn't
own.

Transport authority is intentionally asymmetric: production exposes
``pause_trading(command_id, reason)`` without preflight and
``resume_trading(command_id, expected_control_revision, reason,
preflight_nonce)``. Account and live/paper mode are never caller-supplied.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation

# [M1-F3] owns trader-DB (journal file) migration versions 20-29; Task 1
# used 20, Task 3 used 21. This task owns 22.
TRADING_CONTROL_MIGRATION_VERSION = 22
TRADING_CONTROL_MIGRATION_NAME = "m1f3_trading_control_state"

_TRADING_CONTROL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS trading_control_state (
        account_id VARCHAR PRIMARY KEY,
        new_exposure_paused BOOLEAN NOT NULL,
        revision BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        updated_by_command_id VARCHAR NOT NULL,
        updated_reason VARCHAR NOT NULL
    )
    """,
)

_COLUMNS = (
    "account_id", "new_exposure_paused", "revision", "updated_at",
    "updated_by_command_id", "updated_reason",
)

_ENTITY_TYPE = "trading_control"


def apply_trading_control_migration(journal_migrator: SchemaMigrator) -> None:
    """Create ``trading_control_state`` in the journal DB (idempotent)."""
    journal_migrator.apply(
        version=TRADING_CONTROL_MIGRATION_VERSION,
        name=TRADING_CONTROL_MIGRATION_NAME,
        statements=list(_TRADING_CONTROL_STATEMENTS),
    )


class PauseStateUnavailable(Exception):
    """No ``trading_control_state`` row exists for this account.

    Fail closed: an ungoverned account must never be treated as "unpaused
    by default" -- every exposure-increasing caller (``require_unpaused``)
    and every reader (``get``) raises this rather than assuming a safe
    default.
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        super().__init__(
            f"no trading_control_state row for account {account_id!r} -- "
            f"fail closed, this account is treated as ungoverned/blocked"
        )


class PauseRevisionConflict(Exception):
    """A resume (or a losing concurrent writer) didn't match the current row."""

    def __init__(self, account_id: str, current_revision: int):
        self.account_id = account_id
        self.current_revision = current_revision
        super().__init__(
            f"revision conflict for account {account_id!r}: "
            f"current revision is {current_revision}"
        )


class TradingPausedError(Exception):
    """Raised by ``require_unpaused*`` when the account is paused."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        super().__init__(f"account {account_id!r} is paused for new exposure")


@dataclass(frozen=True)
class TradingControlState:
    """One durable ``trading_control_state`` row."""

    account_id: str
    new_exposure_paused: bool
    revision: int
    updated_at: dt.datetime
    updated_by_command_id: str
    updated_reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "new_exposure_paused": self.new_exposure_paused,
            "revision": self.revision,
            "updated_at": self.updated_at.isoformat(),
            "updated_by_command_id": self.updated_by_command_id,
            "updated_reason": self.updated_reason,
        }


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("trading_control timestamps must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _row_to_state(row: tuple) -> TradingControlState:
    data = dict(zip(_COLUMNS, row))
    return TradingControlState(
        account_id=data["account_id"],
        new_exposure_paused=bool(data["new_exposure_paused"]),
        revision=data["revision"],
        updated_at=_as_utc(data["updated_at"]),
        updated_by_command_id=data["updated_by_command_id"],
        updated_reason=data["updated_reason"],
    )


class TradingControlStore:
    """Persistence adapter for ``trading_control_state`` in the journal file."""

    _SELECT = f"SELECT {', '.join(_COLUMNS)} FROM trading_control_state WHERE account_id = ?"

    def __init__(self, journal: DomainJournal):
        self._journal = journal

    # -- reads --------------------------------------------------------------

    def _get_in_tx(self, conn: Any, account_id: str) -> Optional[TradingControlState]:
        row = conn.execute(self._SELECT, [account_id]).fetchone()
        return _row_to_state(row) if row is not None else None

    def get(self, account_id: str) -> TradingControlState:
        state = self._get_in_tx(self._journal.connect(), account_id)
        if state is None:
            raise PauseStateUnavailable(account_id)
        return state

    def require_unpaused_in_tx(self, conn: Any, account_id: str) -> None:
        """Re-read the row on ``conn`` -- the CALLER's transaction.

        Task 5's approval saga calls this inside the same transaction that
        claims a proposal's ``SUBMITTING`` state, so a pause that has
        already committed on another connection is guaranteed to be seen
        here (spec I1: pause serializes against final order dispatch).
        """
        state = self._get_in_tx(conn, account_id)
        if state is None:
            raise PauseStateUnavailable(account_id)
        if state.new_exposure_paused:
            raise TradingPausedError(account_id)

    def require_unpaused(self, account_id: str) -> None:
        self.require_unpaused_in_tx(self._journal.connect(), account_id)

    # -- bootstrap ------------------------------------------------------------

    def seed_in_tx(
        self, conn: Any, accounts: Sequence[tuple[str, str]], now: dt.datetime,
    ) -> list[TradingControlState]:
        """Insert only accounts that don't already have a row.

        ``accounts`` is ``[(account_id, mode)]`` where ``mode`` is
        ``"live"`` or ``"paper"``: live accounts start PAUSED (safe
        default -- a live account must be explicitly resumed before it can
        trade), paper accounts start UNPAUSED (paper trading is
        consequence-free by construction). Idempotent: a re-run with the
        same accounts inserts nothing new and returns only what THIS call
        inserted (an empty list on a fully-seeded re-run). Runs entirely on
        the caller-supplied ``conn`` -- the caller (``trader_service``, or
        a test's ``db.transaction(...)``) owns the transaction boundary.
        """
        now = _as_utc(now)
        seeded: list[TradingControlState] = []
        for account_id, mode in accounts:
            if self._get_in_tx(conn, account_id) is not None:
                continue
            paused = mode == "live"
            row = conn.execute(
                """
                INSERT INTO trading_control_state (
                    account_id, new_exposure_paused, revision, updated_at,
                    updated_by_command_id, updated_reason
                ) VALUES (?, ?, 1, ?, ?, ?)
                RETURNING """ + ", ".join(_COLUMNS),
                [account_id, paused, now, "system:bootstrap", "account initialization"],
            ).fetchone()
            state = _row_to_state(row)
            self._journal_in_tx(
                conn, state, correlation_id=f"system:bootstrap:{account_id}",
                event_id=f"trading_control:{account_id}:{state.revision}",
            )
            seeded.append(state)
        return seeded

    # -- pause / resume -------------------------------------------------------

    def set_pause_in_tx(
        self,
        conn: Any,
        account_id: str,
        paused: bool,
        expected_revision: Optional[int],
        command_id: str,
        reason: str,
        now: dt.datetime,
    ) -> TradingControlState:
        now = _as_utc(now)
        current = self._get_in_tx(conn, account_id)
        if current is None:
            raise PauseStateUnavailable(account_id)          # fail closed, never auto-seed here
        if current.new_exposure_paused == paused:
            return current                                   # absolute set: idempotent no-op
        if not paused:                                       # resume is risk-increasing
            if expected_revision != current.revision:
                raise PauseRevisionConflict(account_id, current.revision)
        rows = conn.execute(
            """
            UPDATE trading_control_state
               SET new_exposure_paused = ?, revision = revision + 1,
                   updated_at = ?, updated_by_command_id = ?, updated_reason = ?
             WHERE account_id = ? AND revision = ?
         RETURNING """ + ", ".join(_COLUMNS),
            [paused, now, command_id, reason, account_id, current.revision],
        ).fetchall()
        if not rows:
            raise PauseRevisionConflict(account_id, current.revision)   # concurrent writer won
        state = _row_to_state(rows[0])
        self._journal_in_tx(
            conn, state, correlation_id=command_id,
            event_id=f"trading_control:{account_id}:{state.revision}",
        )
        return state

    def set(
        self,
        account_id: str,
        paused: bool,
        expected_revision: Optional[int],
        command_id: str,
        reason: str,
        now: dt.datetime,
    ) -> TradingControlState:
        """Own the transaction boundary around ``set_pause_in_tx``.

        Used by the split ``pause_trading`` / ``resume_trading`` actions and
        directly by tests exercising the "public" (non-``_in_tx``) surface.
        Mirrors ``CommandLedger.purge_expired``'s manual
        BEGIN/COMMIT/ROLLBACK idiom rather than ``DomainJournal.mutate`` --
        see the module docstring for why.
        """
        conn = self._journal.connect()
        conn.execute("BEGIN TRANSACTION")
        try:
            state = self.set_pause_in_tx(
                conn, account_id, paused, expected_revision, command_id, reason, now,
            )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except BaseException:
                pass
            raise
        return state

    # -- journaling (manual -- see module docstring) --------------------------

    def _journal_in_tx(
        self, conn: Any, state: TradingControlState, *, correlation_id: str, event_id: str,
    ) -> None:
        """Insert exactly one ``trading_control.updated`` event on ``conn``.

        Cannot use ``DomainJournal.mutate`` (it self-issues BEGIN/COMMIT --
        nested BEGIN crashes DuckDB when called from inside a transaction
        ``seed_in_tx``/``set_pause_in_tx`` don't own). Instead this
        replicates the same "bump entity_revision, upsert the materialized
        ledger, insert the journal row" sequence ``mutate()`` performs,
        directly against ``self._journal``'s own bookkeeping so this event
        is fully visible to ``read_after``/the long-poll feed like every
        other domain event.
        """
        mutation = DomainMutation(
            event_type="trading_control.updated",
            entity_type=_ENTITY_TYPE,
            entity_id=state.account_id,
            operation="upsert",
            account_id=state.account_id,
            source="trader_service",
            source_timestamp=state.updated_at,
            correlation_id=correlation_id,
            payload={
                "account_id": state.account_id,
                "new_exposure_paused": state.new_exposure_paused,
                "revision": state.revision,
                "updated_by_command_id": state.updated_by_command_id,
                "updated_reason": state.updated_reason,
            },
        )
        next_revision = self._journal._read_current_revision(conn, mutation.entity_type, mutation.entity_id) + 1
        self._journal._upsert_materialized(conn, mutation, next_revision, state.updated_at)
        self._journal._insert_journal_row(conn, event_id, mutation, next_revision, state.updated_at)
