"""Broker-verified liquidation and safe close state machine.

Order acknowledgements are *evidence of uncertainty*, never evidence that a
position is gone. Every child order is journaled before the broker call
(write-ahead) and fenced on the newest broker generation right after it. A
child becomes terminal only from its own broker row, or absent only when a
complete broker enumeration opened after that fence does not show it. A
further reduce is sized only from a broker generation newer than the last
fill this root observed.

Scopes: ``account`` (flatten everything; session flatten, /flatten,
protective failure, kill) and ``conid`` (close one position, fully or in
part, with protection hand-over and a linked re-protect for a partial).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import numbers
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, ContextManager, Optional, Protocol

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.trading.exit_owner import (
    CLAIMED, JOINED_FLATTEN, STATE_ACTIVE, STATE_FAILED_SAFE, STATE_RELEASED, UPGRADED, ExitOwnerRegistry,
    apply_exit_owner_migration,
)
from trader.trading.order_correlation import (
    legacy_reduce_prefix, liquidation_child_id, liquidation_child_kind, reprotect_oca_group,
)

log = logging.getLogger(__name__)

LIQUIDATION_MIGRATION_VERSION = 25
LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION = 36

RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "REDUCE_FAILED", "SUPERSEDED", "FAILED_SAFE"})
SUCCESS_STATES = frozenset({"FLAT", "CLOSED", "DONE"})
# Proven end states: the scope is closed or protected again, so the owner is released.
OWNER_RELEASED_STATES = SUCCESS_STATES | {"REDUCE_FAILED"}
_SUCCESS_FOR_GOAL = {
    "account": frozenset({"FLAT"}),
    "zero": frozenset({"CLOSED", "FLAT"}),
    "partial": frozenset({"DONE", "CLOSED", "FLAT"}),
}
_FAILURE_CODES = {"FAILED_SAFE": "CLOSE_FAILED_SAFE", "REDUCE_FAILED": "REDUCE_FAILED"}

CHILD_STATES = frozenset({
    "PLANNED", "UNKNOWN", "WORKING", "PENDING_CANCEL", "FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT",
})
CHILD_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"})
# Live at the broker: blocks every new reduce. Only WORKING is healthy protection (ruling 47).
CHILD_LIVE = ("WORKING", "PENDING_CANCEL")
CHILD_OPEN = ("UNKNOWN",) + CHILD_LIVE
# Terminal children whose broker row may still report a later fill (ruling 50).
_FILL_MAY_GROW = ("FILLED", "CANCELLED", "REJECTED")
_BROKER_ACCEPTED = frozenset({"PreSubmitted", "Submitted", "PendingCancel"})
_BROKER_HEALTHY = frozenset({"PreSubmitted", "Submitted"})
_BROKER_TERMINAL = {
    "Filled": "FILLED", "Cancelled": "CANCELLED", "ApiCancelled": "CANCELLED",
    "Inactive": "REJECTED", "Rejected": "REJECTED",
}
_LEG_KINDS = ("reprotect-stop", "reprotect-target")
_OPEN_BEFORE_SP1 = "state NOT IN ('FLAT', 'FAILED_SAFE')"


def apply_liquidation_migration(migrator: SchemaMigrator) -> None:
    """Journal migrations 25 (runs) and 36 (scope, goal, children, joins).

    Migration 36 also adopts runs that were open before the upgrade (D8): each
    run gets a join row; the oldest open run of an account becomes its ACTIVE
    account owner, in phase ``legacy`` (it acts only on a broker generation
    opened after the upgrade); other open runs of that account are SUPERSEDED
    by it. Every old run, FLAT included, is marked ``pre_sp1_open``: it may
    have sent a reduce the journal does not know (N2). An old FLAT was decided
    from an empty snapshot, which does not prove that an earlier reduce will
    not arrive late (ruling 49). Such a run is
    tracked by one wildcard child (``ref_prefix`` set, ``conid`` NULL), because
    old runs record no conids (ruling 42). Exit owners (migration 35) are
    applied first.
    """
    migrator.apply(LIQUIDATION_MIGRATION_VERSION, "p1_liquidation_runs", (
        """CREATE TABLE IF NOT EXISTS liquidation_runs (
            cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL, deadline TIMESTAMPTZ NOT NULL,
            generation_id BIGINT, detail VARCHAR NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        )""",
    ))
    apply_exit_owner_migration(migrator)
    migrator.apply(LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION, "sp1_liquidation_safe_close", (
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS scope VARCHAR DEFAULT 'account'",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS conid INTEGER",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS goal VARCHAR DEFAULT 'zero'",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS goal_quantity DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS phase VARCHAR DEFAULT ''",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS opened_generation BIGINT",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS stop_price DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS target_price DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS remaining_quantity DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS escalated BOOLEAN DEFAULT FALSE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS superseded_by VARCHAR",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS cleanup_pending BOOLEAN DEFAULT FALSE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS pre_sp1_open BOOLEAN DEFAULT FALSE",
        "UPDATE liquidation_runs SET pre_sp1_open = TRUE",
        """CREATE TABLE IF NOT EXISTS liquidation_children (
            child_id VARCHAR PRIMARY KEY,
            root_id VARCHAR NOT NULL,
            owner_root_id VARCHAR NOT NULL,
            account_id VARCHAR NOT NULL,
            conid INTEGER,
            kind VARCHAR NOT NULL,
            attempt INTEGER NOT NULL,
            state VARCHAR NOT NULL,
            fence_generation BIGINT NOT NULL,
            side VARCHAR,
            quantity DOUBLE,
            price DOUBLE,
            oca_group VARCHAR,
            target_order_entity_id VARCHAR,
            filled_at_send DOUBLE NOT NULL DEFAULT 0,
            filled_quantity DOUBLE NOT NULL DEFAULT 0,
            outstanding_quantity DOUBLE,
            observed_generation BIGINT,
            sent_generation BIGINT,
            order_entity_id VARCHAR,
            ref_prefix VARCHAR,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_liquidation_children_owner ON liquidation_children(owner_root_id)",
        """CREATE TABLE IF NOT EXISTS liquidation_joins (
            command_id VARCHAR PRIMARY KEY,
            root_id VARCHAR NOT NULL,
            account_id VARCHAR NOT NULL,
            conid INTEGER,
            outcome VARCHAR NOT NULL,
            requested_goal VARCHAR NOT NULL,
            requested_quantity DOUBLE,
            recorded_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_liquidation_joins_root ON liquidation_joins(root_id)",
        """INSERT INTO liquidation_joins
           SELECT cause_command_id, cause_command_id, account_id, NULL, 'CLAIMED', 'account', NULL, updated_at
           FROM liquidation_runs""",
        f"""INSERT INTO exit_owners
           SELECT cause_command_id, account_id, NULL, 'account_flatten', 'zero', NULL,
                  CASE WHEN row_number() OVER (PARTITION BY account_id ORDER BY updated_at, cause_command_id) = 1
                       THEN 'ACTIVE' ELSE 'SUPERSEDED' END,
                  updated_at
           FROM liquidation_runs WHERE {_OPEN_BEFORE_SP1}""",
        """UPDATE liquidation_runs SET state = 'SUPERSEDED', detail = 'taken over at the SP1 upgrade',
               superseded_by = (SELECT o.root_id FROM exit_owners o
                                WHERE o.account_id = liquidation_runs.account_id
                                  AND o.kind = 'account_flatten' AND o.state = 'ACTIVE')
           WHERE cause_command_id IN (SELECT root_id FROM exit_owners WHERE state = 'SUPERSEDED')""",
        f"UPDATE liquidation_runs SET phase = 'legacy' WHERE {_OPEN_BEFORE_SP1} AND state <> 'SUPERSEDED'",
    ))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LiquidationBusy(RuntimeError):
    """Another caller held the liquidation lock past the timeout; retry later."""


class DispatchRefused(RuntimeError):
    """A proven refusal *before* the broker boundary. The child becomes NOT_SENT."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class LiquidationRefused(ValueError):
    """A request the close refuses up front (no claim, no run, no order)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class RunStateError(RuntimeError):
    """A forbidden run change: a terminal or SUPERSEDED run, or a goal moving back to partial."""


class BrokerChangesBusy(RuntimeError):
    """Broker writes could not be held (lock busy, or a generation is staging); decide again later."""


class _StaleDispatch(RuntimeError):
    """R7: the journal says this root may no longer dispatch."""


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

class BrokerSnapshotPort(Protocol):
    def capture(self, account_id: str) -> Any: ...


class LiquidationDispatchPort(Protocol):
    """Reduce-only order boundary plus read-only broker evidence.

    Order methods raise ``DispatchRefused`` only for a proven refusal before
    the broker boundary. Any other exception means the order may exist.
    """
    def cancel(self, order: Any, child_id: str) -> None: ...
    def reduce(self, position: Any, side: str, quantity: float, child_id: str) -> None: ...
    def reduce_partial(self, position: Any, side: str, quantity: float, child_id: str) -> None: ...
    def place_exit_leg(self, position: Any, *, leg: str, quantity: float, price: float,
                       oca_group: str, child_id: str) -> None: ...
    def find_orders(self, account_id: str, child_id: str) -> list: ...
    def find_orders_with_prefix(self, account_id: str, prefix: str) -> list: ...
    def get_order(self, order_entity_id: str) -> Optional[Any]: ...
    def enumeration_complete(self) -> bool: ...
    def newest_generation(self) -> int: ...
    def hold_broker_changes(self) -> ContextManager[None]:
        """No broker row or position changes while held; raises ``BrokerChangesBusy`` (ruling 48)."""


class LiquidationBreakerPort(Protocol):
    def trip_liquidation(self, cause_command_id: str, detail: str) -> None: ...


class GenerationRefreshPort(Protocol):
    def request_refresh(self, account_id: str) -> None: ...


@dataclass(frozen=True)
class CancelTarget:
    order_entity_id: str
    order_group_id: Optional[str]


@dataclass(frozen=True)
class HandoverInfo:
    stop_price: Optional[float]
    target_price: Optional[float]


class ProtectionOwnershipPort(Protocol):
    """Implemented by ``ProtectiveOrderSaga``. Every method is idempotent."""
    def handover(self, *, account_id: str, conid: int, close_root_id: str,
                 cancels: tuple[CancelTarget, ...], generation: int, now: dt.datetime) -> HandoverInfo: ...
    def handover_account(self, *, account_id: str, close_root_id: str,
                         cancels: tuple[CancelTarget, ...], generation: int, now: dt.datetime) -> None: ...
    def expect_reprotect(self, *, close_root_id: str, groups: tuple[str, ...], now: dt.datetime) -> None: ...
    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float,
                              stop_group: str, stop_status: str, target_group: Optional[str],
                              target_status: Optional[str], now: dt.datetime) -> None: ...
    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None: ...


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChildRef:
    child_id: str                 # {root}-{kind}-{conid}-{attempt}; also the decoded order ref
    root_id: str                  # root that wrote it
    owner_root_id: str            # root that must reconcile it now (R9 inheritance)
    account_id: str
    conid: Optional[int]          # None only for a pre-SP1 wildcard child
    kind: str                     # cancel | reduce | reprotect-stop | reprotect-target
    attempt: int
    state: str                    # see CHILD_STATES
    fence_generation: int         # broker generation of the snapshot it was planned on
    side: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    oca_group: Optional[str] = None
    target_order_entity_id: Optional[str] = None
    filled_at_send: float = 0.0
    filled_quantity: float = 0.0
    outstanding_quantity: Optional[float] = None
    observed_generation: Optional[int] = None   # newest generation (staging included) when last observed
    sent_generation: Optional[int] = None       # newest generation (staging included) right after the send
    order_entity_id: Optional[str] = None       # the broker row of this child's own order
    ref_prefix: Optional[str] = None            # pre-SP1 wildcard: every ref {prefix}{conid} (ruling 42)

    @property
    def fill_bearing(self) -> bool:
        """True when this child may have changed the position since it was sent.

        An ABSENT child has an unknown fill, so it counts as fill-bearing.
        """
        return self.state == "ABSENT" or self.filled_quantity > self.filled_at_send


@dataclass(frozen=True)
class LiquidationReceipt:
    account_id: str
    cause_command_id: str
    state: str
    deadline: dt.datetime
    generation_id: Optional[int] = None
    detail: str = ""
    scope: str = "account"
    conid: Optional[int] = None
    goal: str = "zero"
    goal_quantity: Optional[float] = None
    phase: str = ""
    opened_generation: Optional[int] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    remaining_quantity: Optional[float] = None
    escalated: bool = False
    superseded_by: Optional[str] = None
    cleanup_pending: bool = False
    children: tuple[ChildRef, ...] = ()


@dataclass(frozen=True)
class JoinRow:
    command_id: str
    root_id: str
    account_id: str
    conid: Optional[int]          # None for an account request
    outcome: str
    requested_goal: str           # account | zero | partial (the request as it came in)
    requested_quantity: Optional[float]   # the raw requested quantity, before admission


@dataclass(frozen=True)
class CloseResolution:
    command_id: str
    root_id: str                  # the root that decided (after following SUPERSEDED)
    state: str
    success: bool
    outcome: dict
    error_code: Optional[str] = None      # set when success is False


@dataclass(frozen=True)
class _ChildOrder:
    """A working child order found by its own broker row, not in the captured snapshot."""
    order_entity_id: str
    conid: int
    order_group_id: str
    filled_quantity: float


_RUN_COLUMNS = (
    "cause_command_id", "account_id", "state", "deadline", "generation_id", "detail", "scope",
    "conid", "goal", "goal_quantity", "phase", "opened_generation", "stop_price", "target_price",
    "remaining_quantity", "escalated", "superseded_by", "cleanup_pending",
)
_CHILD_COLUMNS = (
    "child_id", "root_id", "owner_root_id", "account_id", "conid", "kind", "attempt", "state",
    "fence_generation", "side", "quantity", "price", "oca_group", "target_order_entity_id",
    "filled_at_send", "filled_quantity", "outstanding_quantity", "observed_generation",
    "sent_generation", "order_entity_id", "ref_prefix",
)
_JOIN_COLUMNS = ("command_id", "root_id", "account_id", "conid", "outcome", "requested_goal",
                 "requested_quantity")


def _run_from_row(row) -> LiquidationReceipt:
    values = dict(zip(_RUN_COLUMNS, row))
    return LiquidationReceipt(
        account_id=values["account_id"], cause_command_id=values["cause_command_id"],
        state=values["state"], deadline=values["deadline"], generation_id=values["generation_id"],
        detail=values["detail"], scope=values["scope"] or "account",
        conid=None if values["conid"] is None else int(values["conid"]),
        goal=values["goal"] or "zero", goal_quantity=values["goal_quantity"],
        phase=values["phase"] or "", opened_generation=values["opened_generation"],
        stop_price=values["stop_price"], target_price=values["target_price"],
        remaining_quantity=values["remaining_quantity"], escalated=bool(values["escalated"]),
        superseded_by=values["superseded_by"], cleanup_pending=bool(values["cleanup_pending"]),
    )


def _child_from_row(row) -> ChildRef:
    values = dict(zip(_CHILD_COLUMNS, row))
    values["conid"] = None if values["conid"] is None else int(values["conid"])
    values["attempt"] = int(values["attempt"])
    values["fence_generation"] = int(values["fence_generation"])
    return ChildRef(**values)


class LiquidationRunStore:
    """Journal rows of runs, children and joins. The broker stays the authority."""

    def __init__(self, db):
        self._db = db

    def transaction(self, fn):
        return self._db.transaction(fn)

    # -- runs ------------------------------------------------------------------

    def get_run_in_tx(self, conn, root_id: str) -> Optional[LiquidationReceipt]:
        row = conn.execute(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM liquidation_runs WHERE cause_command_id = ?",
            [root_id]).fetchone()
        return None if row is None else _run_from_row(row)

    def insert_run_in_tx(self, conn, receipt: LiquidationReceipt, now: dt.datetime) -> None:
        values = [getattr(receipt, c) for c in _RUN_COLUMNS]
        conn.execute(
            f"INSERT INTO liquidation_runs ({', '.join(_RUN_COLUMNS)}, updated_at) "
            f"VALUES ({', '.join('?' for _ in _RUN_COLUMNS)}, ?)", values + [now])

    def update_run_in_tx(self, conn, receipt: LiquidationReceipt, now: dt.datetime) -> None:
        current = self.get_run_in_tx(conn, receipt.cause_command_id)
        if current is None:
            raise RunStateError(f"unknown liquidation root {receipt.cause_command_id!r}")
        if current.state in RESCAN_TERMINAL and receipt.state != current.state:
            raise RunStateError(
                f"root {receipt.cause_command_id} is {current.state}; it cannot become {receipt.state}")
        if current.goal == "zero" and receipt.goal != "zero":
            raise RunStateError(f"root {receipt.cause_command_id} goal is zero; it never goes back")
        assignments = ", ".join(f"{c} = ?" for c in _RUN_COLUMNS[1:])
        conn.execute(
            f"UPDATE liquidation_runs SET {assignments}, updated_at = ? WHERE cause_command_id = ?",
            [getattr(receipt, c) for c in _RUN_COLUMNS[1:]] + [now, receipt.cause_command_id])

    def pre_sp1_roots_in_tx(self, conn, account_id: str) -> list[str]:
        """Runs from before the upgrade whose old reduces are not settled yet (N2, ruling 42)."""
        return [r[0] for r in conn.execute(
            "SELECT cause_command_id FROM liquidation_runs WHERE account_id = ? AND pre_sp1_open "
            "ORDER BY updated_at, cause_command_id", [account_id]).fetchall()]

    def clear_pre_sp1_mark_in_tx(self, conn, run_id: str) -> None:
        """Only in the transaction that settles the run's wildcard child (ruling 42)."""
        conn.execute("UPDATE liquidation_runs SET pre_sp1_open = FALSE WHERE cause_command_id = ?", [run_id])

    def fill_watermark_in_tx(self, conn, account_id: str, conid: Optional[int]) -> Optional[int]:
        """Newest generation at which any child of the scope was seen fill-bearing (ruling 43).

        Any root, any state. A conid scope also counts wildcard children
        (conid NULL), which may hold a fill of any conid.
        """
        conid_filter = "" if conid is None else " AND (conid = ? OR conid IS NULL)"
        params: list = [account_id] + ([] if conid is None else [int(conid)])
        row = conn.execute(
            "SELECT MAX(observed_generation) FROM liquidation_children WHERE account_id = ? "
            "AND (state = 'ABSENT' OR filled_quantity > filled_at_send)" + conid_filter, params).fetchone()
        return None if row[0] is None else int(row[0])

    def settled_children_in_tx(self, conn, account_id: str, conid: Optional[int]) -> tuple[ChildRef, ...]:
        """Terminal children of the scope, any root, whose row may still report a later fill (ruling 50).

        A conid scope also reads wildcard children (conid NULL), like the fill watermark.
        """
        conid_filter = "" if conid is None else " AND (conid = ? OR conid IS NULL)"
        params: list = [account_id, *_FILL_MAY_GROW] + ([] if conid is None else [int(conid)])
        rows = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children WHERE account_id = ? "
            f"AND state IN ({', '.join('?' for _ in _FILL_MAY_GROW)})" + conid_filter + " ORDER BY child_id",
            params).fetchall()
        return tuple(_child_from_row(r) for r in rows)

    def legacy_reduces_in_tx(self, conn, account_id: str) -> tuple[ChildRef, ...]:
        """Every wildcard child of the account, whoever owns it (ruling 42)."""
        rows = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children "
            "WHERE account_id = ? AND ref_prefix IS NOT NULL ORDER BY child_id", [account_id]).fetchall()
        return tuple(_child_from_row(r) for r in rows)

    def roots_to_advance_in_tx(self, conn) -> list[str]:
        """Roots with unfinished cleanup first, then every non-terminal root."""
        markers = ", ".join("?" for _ in RESCAN_TERMINAL)
        cleanup = [r[0] for r in conn.execute(
            "SELECT cause_command_id FROM liquidation_runs WHERE cleanup_pending ORDER BY updated_at").fetchall()]
        open_roots = [r[0] for r in conn.execute(
            f"SELECT cause_command_id FROM liquidation_runs WHERE state NOT IN ({markers}) "
            "AND NOT cleanup_pending ORDER BY updated_at", list(RESCAN_TERMINAL)).fetchall()]
        return cleanup + open_roots

    # -- children ----------------------------------------------------------------

    def children_in_tx(self, conn, owner_root_id: str) -> tuple[ChildRef, ...]:
        rows = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children WHERE owner_root_id = ? "
            "ORDER BY fence_generation, child_id", [owner_root_id]).fetchall()
        return tuple(_child_from_row(r) for r in rows)

    def child_in_tx(self, conn, child_id: str) -> Optional[ChildRef]:
        row = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children WHERE child_id = ?",
            [child_id]).fetchone()
        return None if row is None else _child_from_row(row)

    def next_attempt_in_tx(self, conn, root_id: str, kind: str, conid: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) FROM liquidation_children WHERE root_id = ? AND kind = ? AND conid = ?",
            [root_id, kind, int(conid)]).fetchone()
        return int(row[0]) + 1

    def insert_child_in_tx(self, conn, child: ChildRef, now: dt.datetime) -> None:
        if child.state not in CHILD_STATES:
            raise ValueError(f"unknown child state {child.state!r}")
        conn.execute(
            f"INSERT INTO liquidation_children ({', '.join(_CHILD_COLUMNS)}, updated_at) "
            f"VALUES ({', '.join('?' for _ in _CHILD_COLUMNS)}, ?)",
            [getattr(child, c) for c in _CHILD_COLUMNS] + [now])

    def update_child_in_tx(self, conn, child: ChildRef, now: dt.datetime) -> None:
        if child.state not in CHILD_STATES:
            raise ValueError(f"unknown child state {child.state!r}")
        assignments = ", ".join(f"{c} = ?" for c in _CHILD_COLUMNS[1:])
        conn.execute(
            f"UPDATE liquidation_children SET {assignments}, updated_at = ? WHERE child_id = ?",
            [getattr(child, c) for c in _CHILD_COLUMNS[1:]] + [now, child.child_id])

    def drop_planned_in_tx(self, conn, owner_root_id: str, now: dt.datetime) -> None:
        """A PLANNED child was never sent; a root that stops needs it no more."""
        conn.execute(
            "UPDATE liquidation_children SET state = 'NOT_SENT', updated_at = ? "
            "WHERE owner_root_id = ? AND state = 'PLANNED'", [now, owner_root_id])

    def inherit_children_in_tx(self, conn, *, account_id: str, conid: Optional[int],
                               to_root_id: str, now: dt.datetime) -> int:
        """R9: the new owner takes every open child of a SUPERSEDED or FAILED_SAFE root on its scope."""
        conid_filter = "" if conid is None else " AND conid = ?"
        params: list = [to_root_id, now, account_id, to_root_id]
        if conid is not None:
            params.append(int(conid))
        rows = conn.execute(
            "UPDATE liquidation_children SET owner_root_id = ?, updated_at = ? "
            "WHERE account_id = ? AND state IN ('UNKNOWN', 'WORKING', 'PENDING_CANCEL') AND owner_root_id <> ?"
            f"{conid_filter} AND owner_root_id IN ("
            "SELECT cause_command_id FROM liquidation_runs WHERE state IN ('SUPERSEDED', 'FAILED_SAFE')) "
            "RETURNING child_id", params).fetchall()
        return len(rows)

    # -- joins ---------------------------------------------------------------------

    def record_join_in_tx(self, conn, join: JoinRow, now: dt.datetime) -> None:
        conn.execute(
            f"INSERT INTO liquidation_joins ({', '.join(_JOIN_COLUMNS)}, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [getattr(join, c) for c in _JOIN_COLUMNS] + [now])

    def join_for_in_tx(self, conn, command_id: str) -> Optional[JoinRow]:
        row = conn.execute(
            f"SELECT {', '.join(_JOIN_COLUMNS)} FROM liquidation_joins WHERE command_id = ?",
            [command_id]).fetchone()
        return None if row is None else JoinRow(*row)

    def joins_resolving_to_in_tx(self, conn, root_id: str) -> list[JoinRow]:
        rows = conn.execute(
            f"SELECT {', '.join(_JOIN_COLUMNS)} FROM liquidation_joins WHERE root_id = ? OR root_id IN ("
            "SELECT cause_command_id FROM liquidation_runs WHERE superseded_by = ?) ORDER BY command_id",
            [root_id, root_id]).fetchall()
        return [JoinRow(*r) for r in rows]

    # -- reads in their own transaction ---------------------------------------------

    def receipt(self, root_id: str) -> Optional[LiquidationReceipt]:
        def read(conn):
            run = self.get_run_in_tx(conn, root_id)
            return None if run is None else replace(run, children=self.children_in_tx(conn, root_id))
        return self._db.transaction(read)

    def root_for(self, command_id: str) -> Optional[str]:
        join = self._db.transaction(lambda conn: self.join_for_in_tx(conn, command_id))
        return None if join is None else join.root_id

    def close_resolution(self, command_id: str) -> Optional[CloseResolution]:
        """Outcome of the exact root a command started or joined; None while undecided.

        SUPERSEDED is followed to the root that took over. A decided root that
        does not meet the request's goal (FAILED_SAFE, REDUCE_FAILED, or DONE
        for a full close) is a failure with an error code, never a success.
        """
        def read(conn):
            join = self.join_for_in_tx(conn, command_id)
            if join is None:
                return None
            run = self.get_run_in_tx(conn, join.root_id)
            seen: set[str] = set()
            while run is not None and run.state == "SUPERSEDED" and run.superseded_by \
                    and run.cause_command_id not in seen:
                seen.add(run.cause_command_id)
                run = self.get_run_in_tx(conn, run.superseded_by)
            if run is None or run.state not in RESCAN_TERMINAL or run.state == "SUPERSEDED" \
                    or run.cleanup_pending:
                return None
            success = run.state in _SUCCESS_FOR_GOAL[join.requested_goal]
            error_code = None if success else _FAILURE_CODES.get(run.state, "CLOSE_GOAL_NOT_MET")
            sold = conn.execute(
                "SELECT COALESCE(SUM(filled_quantity), 0) FROM liquidation_children "
                "WHERE root_id = ? AND kind = 'reduce'", [run.cause_command_id]).fetchone()[0]
            return CloseResolution(
                command_id=command_id, root_id=run.cause_command_id, state=run.state, success=success,
                error_code=error_code,
                outcome={"close_root_id": run.cause_command_id, "liquidation_state": run.state,
                         "generation_id": run.generation_id, "detail": run.detail,
                         "requested_goal": join.requested_goal, "requested_quantity": join.requested_quantity,
                         "root_goal": run.goal, "filled_quantity": float(sold),
                         "remaining_quantity": run.remaining_quantity},
            )
        return self._db.transaction(read)


def _exact_conid(conid) -> int:
    """#21, ruling 51: an exact positive integer conId, or the close is refused before any claim.

    ``1.5``, ``True`` and ``"265598"`` are never coerced: a coerced id can close another instrument.
    """
    if isinstance(conid, bool) or not isinstance(conid, numbers.Integral) or int(conid) <= 0:
        raise LiquidationRefused("CONID_INVALID", f"conid must be a positive integer, got {conid!r}")
    return int(conid)


def _requested_quantity(quantity) -> Optional[float]:
    """A partial quantity is a finite real number (not a bool, not a string); None is a full close."""
    if quantity is None:
        return None
    if isinstance(quantity, bool) or not isinstance(quantity, numbers.Real) or not math.isfinite(quantity):
        raise LiquidationRefused("PARTIAL_QUANTITY_INVALID", f"quantity must be a finite number, got {quantity!r}")
    return float(quantity)


def _reducing_side(quantity: float) -> str:
    return "SELL" if float(quantity) > 0 else "BUY"


def _targets(orders) -> tuple[CancelTarget, ...]:
    return tuple(CancelTarget(o.order_entity_id, getattr(o, "order_group_id", None)) for o in orders)


class LiquidationService:
    """One state machine for every exit. Call every entry point from one worker thread (R12).

    The worker is the serialization. The timed lock kept from master is a
    second guard for a caller that bypasses the worker (the drill, a test, a
    future producer): ``start`` and ``rescan`` hold it across the broker
    capture and the dispatch wait, and a caller that waits longer than the
    timeout gets :class:`LiquidationBusy`. ``start`` commits its claim before
    it waits, so a busy caller never loses the root: the next ``rescan``
    advances it. Lock order: ``LiquidationService._lock`` before
    ``BrokerIngest._apply_lock``.
    """

    def __init__(
        self,
        broker: BrokerSnapshotPort,
        dispatch: LiquidationDispatchPort,
        *,
        store: LiquidationRunStore,
        registry: ExitOwnerRegistry,
        now: Callable[[], dt.datetime],
        breaker: Optional[LiquidationBreakerPort] = None,
        journal=None,
        ledger=None,
        schedule_reconcile: Optional[Callable[[str], None]] = None,
        protection: Optional[ProtectionOwnershipPort] = None,
        refresh: Optional[GenerationRefreshPort] = None,
        deadline_seconds: float = 300.0,
        lock_timeout_seconds: float = 60.0,
    ):
        self._broker = broker
        self._dispatch = dispatch
        self._store = store
        self._registry = registry
        self._now = now
        self._breaker = breaker
        self._journal, self._ledger = journal, ledger
        self._schedule_reconcile = schedule_reconcile
        self._protection = protection
        self._refresh = refresh
        self._deadline_seconds = deadline_seconds
        # Default: twice the 30s order-dispatch timeout.
        self._lock = threading.Lock()
        self._lock_timeout_seconds = lock_timeout_seconds

    @contextmanager
    def _exclusive(self):
        if not self._lock.acquire(timeout=self._lock_timeout_seconds):
            raise LiquidationBusy(
                f"liquidation busy for more than {self._lock_timeout_seconds}s; retry later")
        try:
            yield
        finally:
            self._lock.release()

    def attach_protection(self, protection: ProtectionOwnershipPort) -> None:
        self._protection = protection

    # -- command entry -------------------------------------------------------------

    def liquidate(self, cmd) -> CommandReceipt:
        """Coordinator saga entry: acknowledgement is explicitly non-terminal.

        Only the reconciler resolves the command, from the exact root it
        started or joined (R17, D12).
        """
        if self._journal is None or self._ledger is None:
            raise RuntimeError("liquidation command authority is not configured")
        try:
            receipt = self.start(cmd.account_id, cmd.command_id,
                                 self._now() + dt.timedelta(seconds=self._deadline_seconds))
        except LiquidationBusy:
            # The claim committed before the wait; record the root as pending so the reconciler resolves it.
            self._record_pending(cmd, self._store.receipt(cmd.command_id))
            raise
        return self._record_pending(cmd, receipt)

    def _record_pending(self, cmd, receipt: LiquidationReceipt) -> CommandReceipt:
        outcome = {"liquidation_state": receipt.state, "detail": receipt.detail,
                   "generation_id": receipt.generation_id, "close_root_id": receipt.cause_command_id}
        now = self._now()

        def write(conn, _revision):
            self._ledger.transition_in_tx(conn, cmd.command_id, "RECEIVED", "OUTCOME_UNKNOWN",
                                          outcome=outcome, error_code="LIQUIDATION_PENDING", now=now)
        self._journal.mutate(self._journal.connect(), DomainMutation(
            event_type="command.updated", entity_type="command", entity_id=command_entity_id(cmd.command_id),
            operation="upsert", account_id=cmd.account_id, source="trader_service", source_timestamp=now,
            correlation_id=cmd.command_id, payload={"state": "OUTCOME_UNKNOWN", **outcome}),
            write, event_id=f"command:{cmd.command_id}:liquidation-pending")
        if self._schedule_reconcile is not None:
            self._schedule_reconcile(cmd.command_id)
        return CommandReceipt(cmd.command_id, cmd.command_id, "OUTCOME_UNKNOWN", outcome,
                              "LIQUIDATION_PENDING", False)

    # -- public API ----------------------------------------------------------------

    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, *,
              scope: str = "account", conid: Optional[int] = None, quantity: Optional[float] = None,
              stop_price: Optional[float] = None, target_price: Optional[float] = None) -> LiquidationReceipt:
        """Claim, then create or join a root. Returns the receipt of the root the caller must poll."""
        if not account_id or not cause_command_id:
            raise ValueError("account_id and cause_command_id are required")
        if ":" in cause_command_id:
            raise ValueError("cause command id may not contain ':'")
        if scope == "account":
            if conid is not None or quantity is not None:
                raise ValueError("account scope takes no conid or quantity")
            outcome, root = self._store.transaction(
                lambda conn: self._claim_account_in_tx(conn, account_id, cause_command_id, deadline))
        elif scope == "conid":
            outcome, root = self._claim_scoped(account_id, cause_command_id, _exact_conid(conid),
                                               _requested_quantity(quantity), deadline, stop_price, target_price)
        else:
            raise ValueError(f"unknown liquidation scope {scope!r}")
        if outcome in (CLAIMED, "EXISTING"):
            with self._exclusive():
                return self._tick(root)
        return self._store.receipt(root)

    def _claim_scoped(self, account_id, cause, conid, quantity, deadline, stop_price, target_price):
        """D15: a retry finds its root first; a partial request then learns ExitInProgress, and
        only a new partial request is admitted against a broker snapshot before it claims."""
        requested = quantity
        goal = "zero" if requested is None else "partial"
        existing = self._store.transaction(lambda conn: self._existing_in_tx(
            conn, account_id, cause, conid=conid, goal=goal, quantity=requested))
        if existing is not None:
            return existing
        admitted = None
        if requested is not None:
            self._store.transaction(
                lambda conn: self._registry.ensure_partial_allowed_in_tx(conn, account_id, conid))
            admitted = self._admit_partial(account_id, conid, requested)
        return self._store.transaction(lambda conn: self._claim_scoped_in_tx(
            conn, account_id, cause, conid, requested, admitted, deadline, stop_price, target_price))

    def rescan(self) -> Optional[LiquidationReceipt]:
        """Finish pending cleanups, then advance every root that is not terminal."""
        first: Optional[LiquidationReceipt] = None
        with self._exclusive():
            for root in self._store.transaction(self._store.roots_to_advance_in_tx):
                try:
                    advanced = self._tick(root)
                except Exception:  # one bad root must not stop the others
                    log.exception("liquidation root %s failed to advance", root)
                    continue
                if first is None:
                    first = advanced
        return first

    def receipt_for(self, root_id: str) -> Optional[LiquidationReceipt]:
        return self._store.receipt(root_id)

    def root_for(self, command_id: str) -> Optional[str]:
        return self._store.root_for(command_id)

    def close_resolution(self, command_id: str) -> Optional[CloseResolution]:
        return self._store.close_resolution(command_id)

    # -- claims (inside one transaction each, R6) ----------------------------------------

    def _existing_in_tx(self, conn, account_id, cause, *, conid, goal, quantity):
        join = self._store.join_for_in_tx(conn, cause)
        if join is None:
            return None
        if (join.account_id, join.conid, join.requested_goal, join.requested_quantity) != (
                account_id, conid, goal, quantity):
            raise ValueError("cause command id is already bound to another scope, conid or goal")
        if join.root_id != cause:
            return ("JOINED_BEFORE", join.root_id)
        return ("EXISTING", cause)

    def _claim_account_in_tx(self, conn, account_id, cause, deadline):
        existing = self._existing_in_tx(conn, account_id, cause, conid=None, goal="account", quantity=None)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_account_in_tx(conn, account_id=account_id, root_id=cause, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, None, claim.outcome,
                                                    "account", None), now)
        if claim.outcome == JOINED_FLATTEN:
            return (JOINED_FLATTEN, claim.root_id)
        self._store.insert_run_in_tx(conn, LiquidationReceipt(account_id, cause, "REQUESTED", deadline), now)
        for root in claim.superseded:
            self._supersede_run_in_tx(conn, root, by_root_id=cause)
        self._store.inherit_children_in_tx(conn, account_id=account_id, conid=None, to_root_id=cause, now=now)
        return (CLAIMED, cause)

    def _supersede_run_in_tx(self, conn, root_id: str, *, by_root_id: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL:
            return
        self._store.update_run_in_tx(conn, replace(
            run, state="SUPERSEDED", superseded_by=by_root_id,
            detail=f"taken over by account flatten {by_root_id}"), self._now())
        self._store.drop_planned_in_tx(conn, root_id, self._now())

    def _claim_scoped_in_tx(self, conn, account_id, cause, conid, requested, admitted, deadline,
                            stop_price, target_price):
        """The join row keeps the request as it came in; the owner and run get the admitted goal."""
        goal = "zero" if requested is None else "partial"
        existing = self._existing_in_tx(conn, account_id, cause, conid=conid, goal=goal, quantity=requested)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_scoped_in_tx(conn, account_id=account_id, conid=conid, root_id=cause,
                                                  goal_quantity=admitted, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, conid, claim.outcome,
                                                    goal, requested), now)
        if claim.outcome == CLAIMED:
            self._store.insert_run_in_tx(conn, LiquidationReceipt(
                account_id, cause, "REQUESTED", deadline, scope="conid", conid=conid,
                goal="zero" if admitted is None else "partial", goal_quantity=admitted,
                stop_price=stop_price, target_price=target_price), now)
            self._store.inherit_children_in_tx(conn, account_id=account_id, conid=conid, to_root_id=cause, now=now)
        elif claim.outcome == UPGRADED:
            self._upgrade_run_in_tx(conn, claim.root_id, f"goal upgraded to zero by {cause}")
        return (claim.outcome, claim.root_id)

    def upgrade_to_zero(self, root_id: str) -> LiquidationReceipt:
        """partial -> zero for an active scoped root, registry and run in one transaction."""
        def write(conn):
            self._registry.upgrade_goal_in_tx(conn, root_id, self._now())
            self._upgrade_run_in_tx(conn, root_id, "goal upgraded to zero exposure")
        self._store.transaction(write)
        return self._store.receipt(root_id)

    def _upgrade_run_in_tx(self, conn, root_id: str, detail: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL or run.goal == "zero":
            return
        phase = "cancel" if run.phase in ("reduce", "reprotect") else run.phase
        self._store.update_run_in_tx(conn, replace(run, goal="zero", goal_quantity=None, phase=phase,
                                                   detail=detail), self._now())
        self._store.drop_planned_in_tx(conn, root_id, self._now())

    def _admit_partial(self, account_id: str, conid: int, requested: float) -> Optional[float]:
        """R15 at start: whole shares, 0 < q < |position|; less than one share left = full close."""
        shares = math.floor(float(requested))
        if shares < 1:
            raise LiquidationRefused("PARTIAL_QUANTITY_INVALID", f"{requested!r} rounds to {shares}")
        snapshot = self._broker.capture(account_id)
        position = self._position_for(snapshot, conid)
        if position is None:
            raise LiquidationRefused("NO_POSITION", f"no position on conid {conid}")
        held = abs(float(position.quantity))
        if shares >= held:
            raise LiquidationRefused("QUANTITY_ABOVE_POSITION", f"{shares} >= {held}; send a full close")
        if held - shares < 1:
            return None
        return float(shares)

    # -- one step of one root --------------------------------------------------------

    def _tick(self, root_id: str) -> Optional[LiquidationReceipt]:
        receipt = self._store.receipt(root_id)
        if receipt is None:
            return None
        if receipt.cleanup_pending:
            return self._cleanup(receipt)
        if receipt.state in RESCAN_TERMINAL:
            return receipt
        try:
            snapshot = self._broker.capture(receipt.account_id)
            newest = int(self._dispatch.newest_generation())
            if getattr(snapshot, "account_id", None) != receipt.account_id:
                raise RuntimeError("broker snapshot account mismatch")
        except Exception as exc:
            if self._now() >= receipt.deadline:
                return self._on_deadline(receipt)
            return self._snapshot_unavailable(receipt, f"broker evidence unavailable: {exc}")
        receipt = self._fence_unsent(receipt, snapshot, newest)
        receipt = self._observe_children(receipt, snapshot, newest)
        receipt = self._observe_late_fills(receipt, newest)
        if self._now() >= receipt.deadline:
            # The deadline decides on this tick's evidence: an UNKNOWN child means FAILED_SAFE (R31).
            return self._on_deadline(receipt)
        if receipt.scope == "account":
            return self._advance_account(receipt, snapshot)
        return self._advance_conid(receipt, snapshot)

    def _snapshot_unavailable(self, receipt, detail):
        state = "OUTCOME_UNKNOWN" if receipt.scope == "account" else "VERIFYING"
        return self._set(receipt, state, detail=detail)

    def _on_deadline(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R31 / D10: a partial close whose protection is already cancelled escalates once to a
        full close of the live remainder, unless a child is UNKNOWN; everything else is FAILED_SAFE."""
        unknown = any(c.state == "UNKNOWN" for c in self._children_in_force(receipt))
        if (receipt.scope == "conid" and receipt.goal == "partial" and not receipt.escalated and not unknown
                and receipt.phase in ("cancel", "reduce", "reprotect")):
            label = "REPROTECT_DEADLINE" if receipt.phase == "reprotect" else "PARTIAL_DEADLINE"
            self._escalate(receipt, f"{label}: the partial close missed its deadline in phase {receipt.phase}")
            return self._store.receipt(receipt.cause_command_id)
        return self._finish(receipt, "FAILED_SAFE", detail="deadline elapsed without broker-confirmed result")

    # -- evidence (R4) ---------------------------------------------------------------

    def _fence_unsent(self, receipt, snapshot, newest: int) -> LiquidationReceipt:
        """Fence children whose send never recorded a fence, and adopt a pre-SP1 run.

        Every entry point runs on one worker, so no send is in flight when a
        tick starts: a child still without ``sent_generation`` was cut off by
        a crash, and every generation newer than ``newest`` opened after it. A
        cancel cut off that way becomes NOT_SENT (sending a cancel again is
        harmless). A run adopted at the upgrade is handled by ``_adopt_legacy``.
        """
        unfenced = [replace(c, sent_generation=newest, state="NOT_SENT" if c.kind == "cancel" else c.state)
                    for c in receipt.children if c.state == "UNKNOWN" and c.sent_generation is None]
        if unfenced:
            def write(conn):
                for child in unfenced:
                    self._store.update_child_in_tx(conn, child, self._now())
            self._store.transaction(write)
        tracked = self._track_pre_sp1_reduces(receipt, newest)
        if receipt.phase == "legacy":
            return self._adopt_legacy(receipt, newest)
        return self._store.receipt(receipt.cause_command_id) if unfenced or tracked else receipt

    def _track_pre_sp1_reduces(self, receipt, newest: int) -> bool:
        """R29 / N2, ruling 42: a run from before the upgrade may have sent reduces the journal does not know.

        Old runs record no conids, so each run of the account that is still
        marked ``pre_sp1_open`` becomes ONE wildcard ``UNKNOWN`` child of this
        root: it stands for every ref ``{run}-liquidation-reduce-{conid}``,
        fenced on ``newest``. The snapshot's positions are not used: a
        missing position is never evidence. The mark is cleared only when the
        child settles (``_observe_children``), never here.
        """
        def write(conn):
            added = False
            for old in self._store.pre_sp1_roots_in_tx(conn, receipt.account_id):
                prefix = legacy_reduce_prefix(old)
                child_id = f"{prefix}*"
                if self._store.child_in_tx(conn, child_id) is not None:
                    continue
                self._store.insert_child_in_tx(conn, ChildRef(
                    child_id=child_id, root_id=old, owner_root_id=receipt.cause_command_id,
                    account_id=receipt.account_id, conid=None, kind="reduce", attempt=0, state="UNKNOWN",
                    fence_generation=newest, sent_generation=newest, ref_prefix=prefix), self._now())
                added = True
            return added
        return self._store.transaction(write)

    def _adopt_legacy(self, receipt, newest: int) -> LiquidationReceipt:
        """R29: the adopted run waits for a broker generation opened after the upgrade."""
        return self._set(self._store.receipt(receipt.cause_command_id), receipt.state, phase="",
                         opened_generation=newest,
                         detail="adopted at the upgrade; old reduces are unknown until the broker settles them")

    def _observe_children(self, receipt, snapshot, newest: int) -> LiquidationReceipt:
        """Classify this root's open children and every open wildcard child of its account.

        A wildcard child that settles clears its run's ``pre_sp1_open`` mark
        in the same transaction (ruling 42).
        """
        generation = int(snapshot.generation_id)
        changed = [observed for child in self._children_in_force(receipt) if child.state in CHILD_OPEN
                   for observed in (self._evidence(child, generation, newest),) if observed != child]
        if not changed:
            return receipt

        def write(conn):
            for child in changed:
                self._store.update_child_in_tx(conn, child, self._now())
                if child.ref_prefix is not None and child.state in CHILD_TERMINAL:
                    self._store.clear_pre_sp1_mark_in_tx(conn, child.root_id)
        self._store.transaction(write)
        return self._store.receipt(receipt.cause_command_id)

    def _observe_late_fills(self, receipt, newest: int) -> LiquidationReceipt:
        """#20, ruling 50: a terminal child's row can report a fill later (``Cancelled`` with 0, then a fill).

        Every settled child of the scope, any root, is read again. Only its
        fill moves, only upwards, with ``observed_generation`` = ``newest``,
        so the fill watermark makes every root wait for a newer position
        generation. Its state stays terminal; an empty or ambiguous lookup
        changes nothing.
        """
        settled = self._store.transaction(
            lambda conn: self._store.settled_children_in_tx(conn, receipt.account_id, receipt.conid))
        grown = [replace(child, filled_quantity=filled, observed_generation=newest) for child in settled
                 for filled in (self._row_fill(child),) if filled is not None and filled > child.filled_quantity]
        if not grown:
            return receipt

        def write(conn):
            for child in grown:
                self._store.update_child_in_tx(conn, child, self._now())
        self._store.transaction(write)
        return self._store.receipt(receipt.cause_command_id)

    def _row_fill(self, child: ChildRef) -> Optional[float]:
        """The fill the broker reports now for a child's own order(s); None when no single row answers."""
        if child.ref_prefix is not None:
            rows = list(self._dispatch.find_orders_with_prefix(child.account_id, child.ref_prefix))
        elif child.kind == "cancel":
            row = self._dispatch.get_order(child.target_order_entity_id)
            rows = [] if row is None or getattr(row, "deleted", False) else [row]
        else:
            rows = list(self._dispatch.find_orders(child.account_id, child.child_id))
        if not rows or (child.ref_prefix is None and len(rows) > 1):
            return None
        return sum(float(getattr(r, "filled_quantity", 0.0) or 0.0) for r in rows)

    def _children_in_force(self, receipt) -> tuple[ChildRef, ...]:
        """The root's own children plus every wildcard child of its account, whoever owns it (ruling 42)."""
        own = {c.child_id for c in receipt.children}
        legacy = self._store.transaction(lambda conn: self._store.legacy_reduces_in_tx(conn, receipt.account_id))
        return receipt.children + tuple(c for c in legacy if c.child_id not in own)

    def _evidence(self, child: ChildRef, generation: int, newest: int) -> ChildRef:
        """Classify one child from its own broker row (R4, D1, D2).

        A terminal status counts at once, on any generation. An empty lookup
        is absence only when a complete enumeration opened after the child's
        send fence shows nothing; otherwise the child stays as it is.
        """
        if child.ref_prefix is not None:
            return self._legacy_evidence(child, generation, newest)
        if child.kind == "cancel":
            row = self._dispatch.get_order(child.target_order_entity_id)
            rows = [] if row is None or getattr(row, "deleted", False) else [row]
        else:
            rows = list(self._dispatch.find_orders(child.account_id, child.child_id))
        if not rows:
            proven = (child.sent_generation is not None and generation > child.sent_generation
                      and self._dispatch.enumeration_complete())
            return replace(child, state="ABSENT", observed_generation=newest) if proven else child
        if len(rows) > 1:
            return child  # an ambiguous correlation proves nothing
        row = rows[0]
        status = getattr(row, "status", None)
        filled = float(getattr(row, "filled_quantity", 0.0) or 0.0)
        total = float(getattr(row, "total_quantity", 0.0) or 0.0)
        if status in _BROKER_TERMINAL:
            state = _BROKER_TERMINAL[status]
        elif status == "PendingCancel":
            state = "PENDING_CANCEL"  # still live (it blocks), but never healthy protection (ruling 47)
        elif status in _BROKER_ACCEPTED:
            state = "WORKING"
        else:
            state = "UNKNOWN"  # PendingSubmit, ApiPending: a local echo is not acceptance
        outstanding = max(total - filled, 0.0)
        entity = child.order_entity_id if child.kind == "cancel" else getattr(row, "order_entity_id", None)
        if (state, filled, outstanding, entity) == (
                child.state, child.filled_quantity, child.outstanding_quantity, child.order_entity_id):
            return child
        return replace(child, state=state, filled_quantity=filled, outstanding_quantity=outstanding,
                       order_entity_id=entity, observed_generation=newest)

    def _legacy_evidence(self, child: ChildRef, generation: int, newest: int) -> ChildRef:
        """Ruling 42: a wildcard child settles only on positive evidence for every conid at once.

        Every broker row matching the prefix must be terminal AND a complete
        enumeration on a generation newer than the fence must hold, even when
        rows are terminal: another conid's old reduce may still be invisible.
        A visible working row keeps the child WORKING; it is waited on, never
        cancelled.
        """
        rows = list(self._dispatch.find_orders_with_prefix(child.account_id, child.ref_prefix))
        statuses = [getattr(r, "status", None) for r in rows]
        filled = sum(float(getattr(r, "filled_quantity", 0.0) or 0.0) for r in rows)
        if any(s not in _BROKER_TERMINAL for s in statuses):
            state = "WORKING" if any(s in _BROKER_ACCEPTED for s in statuses) else "UNKNOWN"
        elif generation > child.sent_generation and self._dispatch.enumeration_complete():
            state = "ABSENT" if not rows else ("FILLED" if filled > 0 else "CANCELLED")
        else:
            state = "UNKNOWN"
        if (state, filled) == (child.state, child.filled_quantity):
            return child
        return replace(child, state=state, filled_quantity=filled, observed_generation=newest)

    def _fresh(self, receipt, generation: int) -> bool:
        """R5, ruling 43: sizing needs a generation newer than every fill observed on the scope.

        The fence is any root's: a fill seen by a root that has since ended,
        been superseded or been restarted still binds the next root.
        """
        watermark = self._store.transaction(
            lambda conn: self._store.fill_watermark_in_tx(conn, receipt.account_id, receipt.conid))
        return watermark is None or generation > watermark

    def _blocking(self, receipt, generation: int) -> Optional[str]:
        """R5, D2: a child that is unknown or still working stops every new reduce.

        Ruling 42: so does every wildcard child of the account, whoever owns it.
        """
        receipt = replace(receipt, children=self._children_in_force(receipt))
        for child in receipt.children:
            if child.state == "UNKNOWN":
                return f"{child.child_id} outcome unknown"
            if child.state in CHILD_LIVE:
                return f"{child.child_id} still working ({child.state})"
        if not self._fresh(receipt, generation):
            return "awaiting a broker generation newer than the last observed fill"
        return None

    @staticmethod
    def _last_action_generation(receipt) -> int:
        fences = [c.sent_generation if c.sent_generation is not None else c.fence_generation
                  for c in receipt.children if c.state != "NOT_SENT"]
        return max(fences + [receipt.opened_generation or 0])

    # -- writes -------------------------------------------------------------------------

    def _trips_breaker(self, receipt, state: str) -> bool:
        if receipt.scope == "account":
            return state != "FLAT"
        return state == "FAILED_SAFE"

    def _set(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        """Change the named fields of the run as the journal has it now, never a stale copy."""
        root = receipt.cause_command_id

        def write(conn):
            current = self._store.get_run_in_tx(conn, root)
            updated = replace(current, state=state, detail=detail,
                              generation_id=current.generation_id if generation_id is None else generation_id,
                              **fields)
            self._store.update_run_in_tx(conn, updated, self._now())
        self._store.transaction(write)
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(root, detail or state)
        return self._store.receipt(root)

    def _wait(self, receipt, generation: int, detail: str) -> LiquidationReceipt:
        if self._refresh is not None:
            self._refresh.request_refresh(receipt.account_id)
        return self._set(receipt, "VERIFYING", generation_id=generation, detail=detail)

    def _finish(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        """Terminal state, owner release and cleanup_pending in one transaction (R6, R8, D3).

        The owner is released here, not in cleanup, so no claim can join a
        root that has already finished.
        """
        self._commit_terminal(receipt, state, generation_id=generation_id, detail=detail, **fields)
        return self._after_terminal(receipt, state, detail)

    def _commit_terminal(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> None:
        root = receipt.cause_command_id
        owner_state = STATE_RELEASED if state in OWNER_RELEASED_STATES else STATE_FAILED_SAFE

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            updated = replace(run, state=state, detail=detail, cleanup_pending=True,
                              generation_id=run.generation_id if generation_id is None else generation_id,
                              **fields)
            self._store.update_run_in_tx(conn, updated, self._now())
            self._store.drop_planned_in_tx(conn, root, self._now())
            self._registry.finish_in_tx(conn, root, owner_state, self._now())
        self._store.transaction(write)

    def _after_terminal(self, receipt, state: str, detail: str) -> LiquidationReceipt:
        root = receipt.cause_command_id
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(root, detail or state)
        return self._cleanup(self._store.receipt(root))

    def _cleanup(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R8: every saga step is idempotent; recovery re-runs it until the flag clears."""
        root = receipt.cause_command_id
        if self._protection is not None:
            if receipt.state in ("CLOSED", "FLAT"):
                self._protection.close_after_full(close_root_id=root, now=self._now())
            elif receipt.state in ("DONE", "REDUCE_FAILED"):
                stop, target = self._working_legs(receipt)
                self._protection.release_after_partial(
                    close_root_id=root, remaining_quantity=float(receipt.remaining_quantity),
                    stop_group=stop.child_id, stop_status=self._leg_status(stop),
                    target_group=None if target is None else target.child_id,
                    target_status=None if target is None else self._leg_status(target),
                    now=self._now())

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(run, cleanup_pending=False), self._now())
        self._store.transaction(write)
        self._schedule_commands(root)
        return self._store.receipt(root)

    def _schedule_commands(self, root: str) -> None:
        """Hand every command waiting on this root to the reconciler, its only resolver (D12).

        A command still in SUBMITTING is left alone: its producer moves it to
        OUTCOME_UNKNOWN and schedules it itself, so the two never race.
        """
        if self._schedule_reconcile is None or self._ledger is None:
            return
        joins = self._store.transaction(lambda conn: self._store.joins_resolving_to_in_tx(conn, root))
        for join in joins:
            row = self._ledger.get(join.command_id)
            if row is not None and row.state == "OUTCOME_UNKNOWN":
                self._schedule_reconcile(join.command_id)

    # -- dispatch (R1, R2, R3, R7) ------------------------------------------------------

    def _check_dispatchable_in_tx(self, conn, root_id: str, goal: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        owner = self._registry.get_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL or run.cleanup_pending:
            raise _StaleDispatch(f"root {root_id} is no longer open")
        if owner is None or owner.state != STATE_ACTIVE:
            raise _StaleDispatch(f"root {root_id} no longer owns its scope")
        if run.goal != goal or owner.goal != run.goal:
            raise _StaleDispatch(f"root {root_id} goal is {run.goal} (owner {owner.goal}), not {goal}")

    def _reserve(self, receipt, build) -> Optional[list]:
        """Journal children before any broker call; None when R7 says stop."""
        def write(conn):
            self._check_dispatchable_in_tx(conn, receipt.cause_command_id, receipt.goal)
            return build(conn)
        try:
            return self._store.transaction(write)
        except _StaleDispatch as ex:
            log.warning("liquidation %s reserves nothing: %s", receipt.cause_command_id, ex)
            return None

    def _still_dispatchable(self, root_id: str, goal: str) -> bool:
        try:
            self._store.transaction(lambda conn: self._check_dispatchable_in_tx(conn, root_id, goal))
        except _StaleDispatch as ex:
            log.warning("liquidation %s stops before the broker: %s", root_id, ex)
            return False
        return True

    def _send(self, receipt, child: ChildRef, call: Callable[[], Any]) -> None:
        """One broker call for one journaled child (R2, D13).

        ``DispatchRefused`` is a proven refusal before the order left: NOT_SENT.
        Any other exception may have crossed the boundary: the child stays
        UNKNOWN. Both are logged. A sent child is fenced on the newest broker
        generation right after the call (R22).
        """
        if not self._still_dispatchable(receipt.cause_command_id, receipt.goal):
            self._mark(child, "NOT_SENT")
            return
        try:
            call()
        except DispatchRefused as ex:
            log.warning("liquidation child %s refused before the broker: %s", child.child_id, ex)
            self._mark(child, "NOT_SENT")
            return
        except Exception:
            log.exception("liquidation child %s: outcome unknown after the broker call", child.child_id)
        try:
            fence = int(self._dispatch.newest_generation())
        except Exception:
            log.exception("liquidation child %s has no send fence; the next tick sets it", child.child_id)
            return

        def write(conn):
            current = self._store.child_in_tx(conn, child.child_id)
            self._store.update_child_in_tx(conn, replace(current, sent_generation=fence), self._now())
        self._store.transaction(write)

    def _mark(self, child: ChildRef, state: str) -> None:
        self._store.transaction(
            lambda conn: self._store.update_child_in_tx(conn, replace(child, state=state), self._now()))

    def _new_child(self, conn, receipt, *, kind: str, conid: int, generation: int, state: str = "UNKNOWN",
                   **fields) -> ChildRef:
        root = receipt.cause_command_id
        attempt = self._store.next_attempt_in_tx(conn, root, kind, conid)
        child = ChildRef(child_id=liquidation_child_id(root, kind, conid, attempt), root_id=root,
                         owner_root_id=root, account_id=receipt.account_id, conid=int(conid), kind=kind,
                         attempt=attempt, state=state, fence_generation=generation, **fields)
        self._store.insert_child_in_tx(conn, child, self._now())
        return child

    def _cancel_targets(self, receipt, orders, conid: Optional[int] = None) -> tuple:
        """Every identified working order of the scope this root has not cancelled yet (D2).

        That is the orders of the captured snapshot plus working re-protect
        legs whose own row shows them working (they may have appeared after
        the capture). Reduce orders are never cancelled: they only reduce, and
        while working they block the next reduce. A cancel this root sent
        covers its target; an inherited one does not (this root sends its own).
        """
        found = {o.order_entity_id: o for o in orders}
        for child in receipt.children:
            if child.kind in _LEG_KINDS and child.state == "WORKING" and child.order_entity_id \
                    and (conid is None or child.conid == conid):
                found.setdefault(child.order_entity_id, _ChildOrder(
                    child.order_entity_id, child.conid, child.child_id, child.filled_quantity))
        covered = {c.target_order_entity_id for c in receipt.children
                   if c.kind == "cancel" and c.root_id == receipt.cause_command_id
                   and c.state in CHILD_OPEN}
        return tuple(o for entity, o in found.items() if entity not in covered
                     and liquidation_child_kind(getattr(o, "order_group_id", None)) != "reduce")

    def _send_cancels(self, receipt, snapshot, targets) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        if not targets:
            return receipt
        children = self._reserve(receipt, lambda conn: [
            self._new_child(conn, receipt, kind="cancel", conid=int(o.conid), generation=generation,
                            target_order_entity_id=o.order_entity_id,
                            filled_at_send=float(getattr(o, "filled_quantity", 0.0) or 0.0))
            for o in targets])
        for child, order in zip(children or (), targets):
            self._send(receipt, child, lambda o=order, c=child: self._dispatch.cancel(o, c.child_id))
        return self._store.receipt(receipt.cause_command_id)

    # -- account scope ----------------------------------------------------------------

    def _advance_account(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        working = tuple(snapshot.working_orders)
        if receipt.phase == "" and receipt.opened_generation is not None \
                and generation <= receipt.opened_generation:
            return self._wait(receipt, generation, "adopted at the upgrade; awaiting a newer broker generation")
        targets = self._cancel_targets(receipt, working)
        if receipt.phase == "" or targets:
            # D6: the saga learns every order the close will cancel before the cancel is sent.
            if self._protection is not None:
                self._protection.handover_account(
                    account_id=receipt.account_id, close_root_id=receipt.cause_command_id,
                    cancels=_targets(targets), generation=generation, now=self._now())
        if receipt.phase == "":
            receipt = self._set(receipt, "CANCELLING_ENTRIES" if working else "VERIFYING",
                                generation_id=generation, phase="cancel", opened_generation=generation,
                                detail="account owner claimed; protection handed over")
        receipt = self._send_cancels(receipt, snapshot, targets)
        why = self._blocking(receipt, generation)
        if why is not None:
            return self._wait(receipt, generation, f"awaiting child confirmation: {why}")
        if working:
            return self._wait(receipt, generation, "awaiting broker confirmation that working orders are gone")
        positions = tuple(p for p in snapshot.positions if float(p.quantity) != 0.0)
        if not positions:
            if generation <= self._last_action_generation(receipt):
                return self._wait(receipt, generation, "awaiting fresh broker flat confirmation")
            return self._finish(receipt, "FLAT", generation_id=generation,
                                detail="fresh broker snapshot confirms no positions or working orders")
        return self._submit_reduces(receipt, snapshot, positions)

    def _submit_reduces(self, receipt, snapshot, positions, *, partial: Optional[float] = None) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        children = self._reserve(receipt, lambda conn: [
            self._new_child(conn, receipt, kind="reduce", conid=int(p.conid), generation=generation,
                            side=_reducing_side(p.quantity),
                            quantity=abs(float(p.quantity)) if partial is None else partial)
            for p in positions])
        if children is None:
            return self._store.receipt(receipt.cause_command_id)
        receipt = self._set(receipt, "REDUCING", generation_id=generation,
                            phase="reduce" if receipt.scope == "conid" else receipt.phase,
                            detail="submitting reduce-only orders")
        for child, position in zip(children, positions):
            send = self._dispatch.reduce if partial is None else self._dispatch.reduce_partial
            self._send(receipt, child, lambda p=position, c=child, s=send: s(p, c.side, c.quantity, c.child_id))
        return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                          "reduction submitted; awaiting broker evidence")

    # -- conid scope --------------------------------------------------------------------

    @staticmethod
    def _position_for(snapshot, conid: int):
        for position in snapshot.positions:
            if int(position.conid) == int(conid) and float(position.quantity) != 0.0:
                return position
        return None

    @staticmethod
    def _working_for(snapshot, conid: int) -> tuple:
        return tuple(o for o in snapshot.working_orders if int(o.conid) == int(conid))

    def _cancel_conid_orders(self, receipt, snapshot, working) -> LiquidationReceipt:
        """Hand over the orders about to be cancelled (D6), then cancel them."""
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        targets = self._cancel_targets(receipt, working, conid)
        if receipt.phase == "" or targets:
            info = HandoverInfo(None, None)
            if self._protection is not None:
                info = self._protection.handover(
                    account_id=receipt.account_id, conid=conid, close_root_id=receipt.cause_command_id,
                    cancels=_targets(targets), generation=generation, now=self._now())
            if receipt.phase == "":
                receipt = self._set(
                    receipt, "CANCELLING" if working else "VERIFYING", generation_id=generation, phase="cancel",
                    opened_generation=generation,
                    stop_price=receipt.stop_price if receipt.stop_price is not None else info.stop_price,
                    target_price=receipt.target_price if receipt.target_price is not None else info.target_price,
                    detail="protection handed over to the close")
        return self._send_cancels(receipt, snapshot, targets)

    def _advance_conid(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        working = self._working_for(snapshot, conid)
        position = self._position_for(snapshot, conid)
        if receipt.phase == "reprotect":
            return self._advance_reprotect(receipt, snapshot, working, position)
        receipt = self._cancel_conid_orders(receipt, snapshot, working)
        why = self._blocking(receipt, generation)
        if why is not None:
            return self._wait(receipt, generation, f"awaiting child confirmation: {why}")
        if working:
            return self._wait(receipt, generation, "awaiting broker confirmation that conid orders are gone")
        if position is None:
            if generation <= self._last_action_generation(receipt):
                return self._wait(receipt, generation, "awaiting a newer generation to prove the position is closed")
            return self._finish(receipt, "CLOSED", generation_id=generation,
                                detail="fresh broker generation shows no position and no working orders for conid")
        if receipt.goal == "partial":
            own = [c for c in receipt.children if c.kind == "reduce" and c.root_id == receipt.cause_command_id]
            if any(c.state in ("FILLED", "CANCELLED", "REJECTED", "ABSENT") for c in own):
                return self._start_reprotect(receipt, snapshot, position)
            return self._submit_partial(receipt, snapshot, position)
        return self._submit_reduces(receipt, snapshot, (position,))

    def _submit_partial(self, receipt, snapshot, position) -> LiquidationReceipt:
        held = abs(float(position.quantity))
        q = float(receipt.goal_quantity)
        if held <= q or held - q < 1:
            # R15 at dispatch: protection is already cancelled, so close the live remainder.
            self.upgrade_to_zero(receipt.cause_command_id)
            receipt = self._store.receipt(receipt.cause_command_id)
            return self._submit_reduces(receipt, snapshot, (position,))
        return self._submit_reduces(receipt, snapshot, (position,), partial=q)

    # -- re-protect (exit-only OCA, R13) ------------------------------------------------

    @staticmethod
    def _protective(position, stop_price: float, target_price: Optional[float]) -> Optional[str]:
        price = getattr(position, "market_price", None)
        if price is None or not math.isfinite(float(price)):
            return "MARKET_PRICE_MISSING: cannot check the stop side without a market price"
        long = float(position.quantity) > 0
        if (long and not stop_price < price) or (not long and not stop_price > price):
            return "STOP_NOT_PROTECTIVE: stop is not on the protective side of the market price"
        if target_price is not None and ((long and not target_price > price) or (not long and not target_price < price)):
            return "TARGET_NOT_VALID: target is not on the profit side of the market price"
        return None

    def _latest(self, receipt, kind: str) -> Optional[ChildRef]:
        legs = [c for c in receipt.children if c.kind == kind and c.root_id == receipt.cause_command_id]
        return max(legs, key=lambda c: c.attempt) if legs else None

    def _working_legs(self, receipt) -> tuple[ChildRef, Optional[ChildRef]]:
        return self._latest(receipt, "reprotect-stop"), self._latest(receipt, "reprotect-target")

    def _leg_row(self, leg: ChildRef):
        rows = list(self._dispatch.find_orders(leg.account_id, leg.child_id))
        return rows[0] if len(rows) == 1 else None

    def _leg_status(self, leg: ChildRef) -> str:
        row = self._leg_row(leg)
        return "Unknown" if row is None else str(getattr(row, "status", "Unknown"))

    def _start_reprotect(self, receipt, snapshot, position) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        if receipt.stop_price is None:
            return self._escalate_now(receipt, snapshot, "STOP_PRICE_MISSING: no stop price for re-protect")
        problem = self._protective(position, float(receipt.stop_price), receipt.target_price)
        if problem is not None:
            return self._escalate_now(receipt, snapshot, problem)
        root = receipt.cause_command_id
        remaining = abs(float(position.quantity))
        side = _reducing_side(position.quantity)
        conid = int(receipt.conid)
        attempt = self._store.transaction(
            lambda conn: self._store.next_attempt_in_tx(conn, root, "reprotect-stop", conid))
        groups = (liquidation_child_id(root, "reprotect-stop", conid, attempt),) + (
            (liquidation_child_id(root, "reprotect-target", conid, attempt),)
            if receipt.target_price is not None else ())
        if self._protection is not None:
            # R2-4: the saga binds the replacement legs before they exist, so their events are never lost.
            self._protection.expect_reprotect(close_root_id=root, groups=groups, now=self._now())

        def build(conn):
            group = reprotect_oca_group(root, conid, attempt)
            legs = [self._new_child(conn, receipt, kind="reprotect-stop", conid=conid, generation=generation,
                                    side=side, quantity=remaining, price=float(receipt.stop_price), oca_group=group)]
            if receipt.target_price is not None:
                legs.append(self._new_child(conn, receipt, kind="reprotect-target", conid=conid,
                                            generation=generation, state="PLANNED", side=side,
                                            quantity=remaining, price=float(receipt.target_price), oca_group=group))
            return legs
        legs = self._reserve(receipt, build)
        if legs is None:
            return self._store.receipt(root)
        receipt = self._set(receipt, "REPROTECTING", generation_id=generation, phase="reprotect",
                            detail="re-protect stop submitted; target waits for the stop")
        self._send_leg(receipt, legs[0], position)
        return self._wait(self._store.receipt(root), generation, "awaiting broker acceptance of the re-protect stop")

    def _send_leg(self, receipt, leg: ChildRef, position) -> None:
        self._send(receipt, leg, lambda: self._dispatch.place_exit_leg(
            position, leg="stop" if leg.kind == "reprotect-stop" else "target", quantity=leg.quantity,
            price=leg.price, oca_group=leg.oca_group, child_id=leg.child_id))

    def _advance_reprotect(self, receipt, snapshot, working, position) -> LiquidationReceipt:
        """R13, R26, R30: the legs' own rows decide; a normal exit ends CLOSED; a leg that
        was refused, rejected, cancelled or lost is a re-protect failure (never sent again).

        #22: DONE is decided from one read of each leg's row. A row that no
        longer matches the child is observed again instead of finishing. Only
        a ``WORKING`` leg on a ``Submitted``/``PreSubmitted`` row is healthy
        protection; ``PendingCancel`` is not (ruling 47). The terminal write
        commits while broker changes are held, after the rows, the position
        and the generation are read again (ruling 48).
        """
        generation = int(snapshot.generation_id)
        stop, target = self._working_legs(receipt)
        if any(c.state == "UNKNOWN" for c in receipt.children):
            return self._wait(receipt, generation, "awaiting broker evidence for a re-protect leg")
        if not self._fresh(receipt, generation):
            return self._wait(receipt, generation, "awaiting a broker generation newer than the last leg fill")
        if position is None:
            # R26: a target fill cancels its OCA stop (or the stop filled): the position was closed by an exit.
            return self._finish_reprotect_closed(receipt, snapshot, working)
        if stop.state in ("CANCELLED", "PENDING_CANCEL") and target is not None \
                and target.state in ("WORKING", "FILLED") and generation <= stop.observed_generation:
            # R26: a target fill cancels its OCA stop; judge the cancel together with the target
            # and the position on a newer generation, never on the callback that came first.
            return self._wait(receipt, generation, "reconciling an OCA stop cancel with its target")
        if stop.state != "WORKING":
            return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: stop leg {stop.state}")
        remaining = abs(float(position.quantity))
        if target is not None and target.state == "PLANNED":
            status = self._leg_status(stop)
            if status not in _BROKER_HEALTHY:
                # Ruling 47: the target is sent only next to a stop whose row is healthy right now.
                receipt = self._observe_children(receipt, snapshot, int(self._dispatch.newest_generation()))
                return self._wait(receipt, generation, f"stop leg row is {status}, not healthy protection")
            sized = replace(target, state="UNKNOWN", quantity=remaining, fence_generation=generation)

            def promote(conn):
                self._store.update_child_in_tx(conn, sized, self._now())
                return [sized]
            if self._reserve(receipt, promote):
                self._send_leg(receipt, sized, position)
            return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                              "re-protect target submitted for the live remaining position")
        if target is not None and target.state != "WORKING":
            return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: target leg {target.state}")
        legs = [stop] + ([target] if target is not None else [])
        if generation <= max(leg.sent_generation or leg.fence_generation for leg in legs):
            return self._wait(receipt, generation, "awaiting a generation newer than the re-protect legs")
        side = _reducing_side(position.quantity)
        rows = [self._leg_row(leg) for leg in legs]
        for leg, row in zip(legs, rows):
            linked = row is not None and getattr(row, "oca_group", None) == stop.oca_group \
                and getattr(row, "oca_type", None) == 2 and getattr(row, "action", None) == side
            if not linked:
                return self._escalate_now(
                    receipt, snapshot, f"REPROTECT_FAILED: {leg.child_id} is not a linked protective leg at the broker")
        for leg, row in zip(legs, rows):
            if self._leg_changed(leg, row):
                receipt = self._observe_children(receipt, snapshot, int(self._dispatch.newest_generation()))
                return self._wait(receipt, generation, f"{leg.child_id} changed since it was observed; observed again")
            if leg.outstanding_quantity != remaining:
                return self._wait(receipt, generation,
                                  f"{leg.child_id} outstanding {leg.outstanding_quantity} != position {remaining}")
        state = self._partial_outcome(receipt)
        detail = ("re-protect legs working in one OCA group for the remaining quantity" if state == "DONE" else
                  "the partial reduce sold nothing; the position is protected again, the close failed")
        return self._finish_held(receipt, state, generation=generation, legs=legs, rows=rows,
                                 remaining=remaining, detail=detail)

    def _finish_held(self, receipt, state: str, *, generation: int, legs, rows, remaining: float,
                     detail: str) -> LiquidationReceipt:
        """Ruling 48: DONE / REDUCE_FAILED and the owner release commit while broker changes are held.

        Under the hold no ingest batch or generation promote can write, so
        the rows, position and generation read again here are the ones the
        terminal transaction commits against. Any change since the decision,
        or a hold that cannot be taken, waits for the next tick.
        """
        try:
            with self._dispatch.hold_broker_changes():
                why = self._changed_since_decision(receipt, generation, legs, rows, remaining)
                if why is None:
                    self._commit_terminal(receipt, state, generation_id=generation,
                                          remaining_quantity=remaining, detail=detail)
        except BrokerChangesBusy as ex:
            why = f"broker changes could not be held: {ex}"
        if why is not None:
            return self._wait(receipt, generation, f"{state} not committed: {why}")
        return self._after_terminal(receipt, state, detail)

    def _changed_since_decision(self, receipt, generation: int, legs, rows, remaining: float) -> Optional[str]:
        try:
            if [self._leg_fingerprint(self._leg_row(leg)) for leg in legs] != [self._leg_fingerprint(r) for r in rows]:
                return "a re-protect leg row changed since the decision"
            snapshot = self._broker.capture(receipt.account_id)
        except Exception as ex:  # an unreadable broker is a reason to decide again, never to finish
            return f"broker evidence unreadable under the hold: {ex}"
        if int(snapshot.generation_id) != generation:
            return f"broker generation moved from {generation} to {snapshot.generation_id}"
        position = self._position_for(snapshot, receipt.conid)
        if position is None or abs(float(position.quantity)) != remaining:
            return "the position changed since the decision"
        return None

    @staticmethod
    def _leg_changed(leg: ChildRef, row) -> bool:
        """#22: True when the leg's row no longer says what the WORKING child recorded.

        Only ``Submitted`` / ``PreSubmitted`` count: a ``PendingCancel`` row is changed (ruling 47).
        """
        filled = float(getattr(row, "filled_quantity", 0.0) or 0.0)
        total = float(getattr(row, "total_quantity", 0.0) or 0.0)
        return (getattr(row, "status", None) not in _BROKER_HEALTHY or filled != leg.filled_quantity
                or max(total - filled, 0.0) != leg.outstanding_quantity)

    @staticmethod
    def _leg_fingerprint(row) -> Optional[tuple]:
        """Everything DONE reads from a leg's row; it must not change before the terminal write (#22, ruling 48)."""
        if row is None:
            return None
        return tuple(getattr(row, field, None) for field in
                     ("status", "filled_quantity", "total_quantity", "oca_group", "oca_type", "action", "revision"))

    def _partial_outcome(self, receipt) -> str:
        """R25 / D4: protection restored is not the requested reduction. Only a proven fill is DONE."""
        sold = sum(c.filled_quantity for c in receipt.children
                   if c.kind == "reduce" and c.root_id == receipt.cause_command_id and c.state != "ABSENT")
        return "DONE" if sold > 0 else "REDUCE_FAILED"

    def _finish_reprotect_closed(self, receipt, snapshot, working) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        receipt = self._cancel_conid_orders(receipt, snapshot, working)
        why = self._blocking(receipt, generation)
        if why is not None or working:
            return self._wait(receipt, generation, f"position closed by an exit; cancelling residual legs ({why})")
        if generation <= self._last_action_generation(receipt):
            return self._wait(receipt, generation, "awaiting a newer generation to prove no residual exits")
        return self._finish(receipt, "CLOSED", generation_id=generation,
                            detail="position closed by a re-protect exit; no residual exits")

    # -- escalation ----------------------------------------------------------------------

    def _escalate(self, receipt, reason: str) -> None:
        """Give up on the partial goal: trip the breaker and continue as a full close."""
        if self._breaker is not None:
            self._breaker.trip_liquidation(receipt.cause_command_id, reason)
        root = receipt.cause_command_id

        def write(conn):
            self._registry.upgrade_goal_in_tx(conn, root, self._now())
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(
                run, state="CANCELLING", goal="zero", goal_quantity=None, phase="cancel", escalated=True,
                deadline=self._now() + dt.timedelta(seconds=self._deadline_seconds),
                detail=f"escalated to full close: {reason}"), self._now())
            self._store.drop_planned_in_tx(conn, root, self._now())
        self._store.transaction(write)

    def _escalate_now(self, receipt, snapshot, reason: str) -> LiquidationReceipt:
        self._escalate(receipt, reason)
        return self._advance_conid(self._store.receipt(receipt.cause_command_id), snapshot)

