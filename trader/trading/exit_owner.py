# trader/trading/exit_owner.py
"""Durable exit ownership: one close per position, one flatten per account.

Every exit producer (time exit, SELL intent, AI close, protective failure,
session flatten, /flatten, kill) claims here before it does anything. The
registry decides whether the caller starts a new root, joins an existing
root, upgrades a partial close to a full one, or is refused.

The ``*_in_tx`` methods take an open DuckDB connection. ``LiquidationService``
calls them inside one ``db.transaction`` together with its own run and child
rows, so a claim and the run it creates commit together or not at all.
Never call ``self._db`` from inside an ``*_in_tx`` method: the connection
lock is not reentrant.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator

EXIT_OWNER_MIGRATION_VERSION = 35

KIND_SCOPED = "scoped_close"
KIND_ACCOUNT = "account_flatten"
GOAL_ZERO = "zero"
GOAL_PARTIAL = "partial"
STATE_ACTIVE = "ACTIVE"
STATE_SUPERSEDED = "SUPERSEDED"
STATE_RELEASED = "RELEASED"
STATE_FAILED_SAFE = "FAILED_SAFE"

CLAIMED = "CLAIMED"
JOINED = "JOINED"
UPGRADED = "UPGRADED"
JOINED_FLATTEN = "JOINED_FLATTEN"


def apply_exit_owner_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(EXIT_OWNER_MIGRATION_VERSION, "sp1_exit_owners", (
        """CREATE TABLE IF NOT EXISTS exit_owners (
            root_id VARCHAR PRIMARY KEY,
            account_id VARCHAR NOT NULL,
            conid INTEGER,
            kind VARCHAR NOT NULL,
            goal VARCHAR NOT NULL,
            goal_quantity DOUBLE,
            state VARCHAR NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_exit_owners_account ON exit_owners(account_id, state)",
    ))


class ExitInProgress(RuntimeError):
    code = "EXIT_IN_PROGRESS"

    def __init__(self, root_id: str):
        self.root_id = root_id
        super().__init__(f"an exit already owns this position: {root_id}")


@dataclass(frozen=True)
class ExitOwnerRow:
    root_id: str
    account_id: str
    conid: Optional[int]
    kind: str
    goal: str
    goal_quantity: Optional[float]
    state: str


@dataclass(frozen=True)
class ExitClaim:
    root_id: str
    outcome: str  # CLAIMED | JOINED | UPGRADED | JOINED_FLATTEN
    superseded: tuple[str, ...] = ()


_COLUMNS = "root_id, account_id, conid, kind, goal, goal_quantity, state"


def _row(values: Optional[tuple]) -> Optional[ExitOwnerRow]:
    if values is None:
        return None
    root_id, account_id, conid, kind, goal, goal_quantity, state = values
    return ExitOwnerRow(root_id, account_id, None if conid is None else int(conid),
                        kind, goal, goal_quantity, state)


def _check_root_id(root_id: str) -> None:
    if not root_id or ":" in root_id:
        raise ValueError("root id must be non-empty and may not contain ':'")


class ExitOwnerRegistry:
    def __init__(self, db: Any):
        self._db = db

    # -- reads inside a caller's transaction --------------------------------

    def get_in_tx(self, conn, root_id: str) -> Optional[ExitOwnerRow]:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE root_id = ?", [root_id]).fetchone())

    def owner_for_in_tx(self, conn, account_id: str, conid: int) -> Optional[ExitOwnerRow]:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND conid = ? "
            "AND kind = ? AND state = ?",
            [account_id, int(conid), KIND_SCOPED, STATE_ACTIVE]).fetchone())

    def account_owner_in_tx(self, conn, account_id: str) -> Optional[ExitOwnerRow]:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
            [account_id, KIND_ACCOUNT, STATE_ACTIVE]).fetchone())

    def ensure_partial_allowed_in_tx(self, conn, account_id: str, conid: int) -> None:
        """A partial request against any active owner is refused, before anything is read or written."""
        owner = self.account_owner_in_tx(conn, account_id) or self.owner_for_in_tx(conn, account_id, conid)
        if owner is not None:
            raise ExitInProgress(owner.root_id)

    # -- claims inside a caller's transaction -------------------------------

    def claim_scoped_in_tx(self, conn, *, account_id: str, conid: int, root_id: str,
                           goal_quantity: Optional[float], now: dt.datetime) -> ExitClaim:
        _check_root_id(root_id)
        wants_partial = goal_quantity is not None
        flatten = self.account_owner_in_tx(conn, account_id)
        if flatten is not None:
            if wants_partial:
                raise ExitInProgress(flatten.root_id)
            return ExitClaim(flatten.root_id, JOINED_FLATTEN)
        owner = self.owner_for_in_tx(conn, account_id, conid)
        if owner is not None:
            if owner.root_id == root_id:
                return ExitClaim(owner.root_id, JOINED)
            if wants_partial:
                raise ExitInProgress(owner.root_id)
            if owner.goal == GOAL_PARTIAL:
                self.upgrade_goal_in_tx(conn, owner.root_id, now)
                return ExitClaim(owner.root_id, UPGRADED)
            return ExitClaim(owner.root_id, JOINED)
        self._insert_in_tx(conn, root_id, account_id, int(conid), KIND_SCOPED,
                           GOAL_PARTIAL if wants_partial else GOAL_ZERO, goal_quantity, now)
        return ExitClaim(root_id, CLAIMED)

    def claim_account_in_tx(self, conn, *, account_id: str, root_id: str,
                            now: dt.datetime) -> ExitClaim:
        _check_root_id(root_id)
        flatten = self.account_owner_in_tx(conn, account_id)
        if flatten is not None:
            outcome = CLAIMED if flatten.root_id == root_id else JOINED_FLATTEN
            return ExitClaim(flatten.root_id, outcome)
        scoped = tuple(sorted(r[0] for r in conn.execute(
            "SELECT root_id FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
            [account_id, KIND_SCOPED, STATE_ACTIVE]).fetchall()))
        conn.execute(
            "UPDATE exit_owners SET state = ?, updated_at = ? "
            "WHERE account_id = ? AND kind = ? AND state = ?",
            [STATE_SUPERSEDED, now, account_id, KIND_SCOPED, STATE_ACTIVE])
        self._insert_in_tx(conn, root_id, account_id, None, KIND_ACCOUNT, GOAL_ZERO, None, now)
        return ExitClaim(root_id, CLAIMED, scoped)

    def upgrade_goal_in_tx(self, conn, root_id: str, now: dt.datetime) -> None:
        """partial -> zero. Never the other way, never on a non-ACTIVE row."""
        conn.execute(
            "UPDATE exit_owners SET goal = ?, goal_quantity = NULL, updated_at = ? "
            "WHERE root_id = ? AND state = ?", [GOAL_ZERO, now, root_id, STATE_ACTIVE])

    def finish_in_tx(self, conn, root_id: str, state: str, now: dt.datetime) -> None:
        """ACTIVE -> RELEASED (proven done) or FAILED_SAFE (outcome not proven)."""
        if state not in (STATE_RELEASED, STATE_FAILED_SAFE):
            raise ValueError(f"an owner can only finish as RELEASED or FAILED_SAFE, not {state!r}")
        conn.execute("UPDATE exit_owners SET state = ?, updated_at = ? WHERE root_id = ? AND state = ?",
                     [state, now, root_id, STATE_ACTIVE])

    def _insert_in_tx(self, conn, root_id, account_id, conid, kind, goal, goal_quantity, now) -> None:
        if conn.execute("SELECT 1 FROM exit_owners WHERE root_id = ?", [root_id]).fetchone():
            raise ValueError(f"root id {root_id!r} was already used for an exit")
        conn.execute("INSERT INTO exit_owners VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     [root_id, account_id, conid, kind, goal, goal_quantity, STATE_ACTIVE, now])

    # -- one-transaction conveniences ---------------------------------------

    def get(self, root_id: str) -> Optional[ExitOwnerRow]:
        return self._db.transaction(lambda conn: self.get_in_tx(conn, root_id))

    def owner_for(self, account_id: str, conid: int) -> Optional[ExitOwnerRow]:
        return self._db.transaction(lambda conn: self.owner_for_in_tx(conn, account_id, conid))

    def account_owner(self, account_id: str) -> Optional[ExitOwnerRow]:
        return self._db.transaction(lambda conn: self.account_owner_in_tx(conn, account_id))

    def claim_scoped(self, **kwargs) -> ExitClaim:
        return self._db.transaction(lambda conn: self.claim_scoped_in_tx(conn, **kwargs))

    def claim_account(self, **kwargs) -> ExitClaim:
        return self._db.transaction(lambda conn: self.claim_account_in_tx(conn, **kwargs))

    def release(self, root_id: str, now: dt.datetime) -> None:
        self._db.transaction(lambda conn: self.finish_in_tx(conn, root_id, STATE_RELEASED, now))
