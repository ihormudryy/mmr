"""P3 Task 5 — broker-native protective entry saga.

Durable state machine for automated bracket/OCA entries. Broker events
(not submit acknowledgements) advance working/filled states. An entry fill
without confirmed working protection trips the P1 breaker and starts
verified liquidation.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import threading
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Optional, Protocol

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.trading.approval_context import AllocationDispatchEvidence, InFlightEntry
from trader.trading.circuit_breaker import BreakerSignal
from trader.trading.command_coordinator import BrokerRejectedError
from trader.trading.dispatch_guard import DispatchGuardError
from trader.trading.order_correlation import encode_order_ref

PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION = 30
PROTECTIVE_ORDER_SAGA_MIGRATION_NAME = "p3_automated_order_sagas"
PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 37
_SAVE_ATTEMPTS = 5

SAGA_STATES = frozenset({
    "VALIDATED",
    "SUBMITTING",
    "ENTRY_WORKING",
    "PARTIALLY_FILLED",
    "PROTECTED",
    "EXITING",
    "CLOSED",
    "OUTCOME_UNKNOWN",
    "SAFETY_FAILED",
    "CLOSE_OWNED",
    "NOT_SENT",
})

# A stop that protects a released remainder: accepted and working, or already filled as the exit.
_RELEASE_STOP_STATUSES = frozenset({"PreSubmitted", "Submitted", "Filled"})
_WORKING_STATUSES = frozenset({
    "PendingSubmit", "PreSubmitted", "Submitted", "ApiPending", "PendingCancel",
})
_FILLED_STATUSES = frozenset({"Filled"})
_CANCELLED_STATUSES = frozenset({"Cancelled", "ApiCancelled"})
_REJECTED_STATUSES = frozenset({"Inactive", "Rejected"})
_TERMINAL_SAGA = frozenset({"CLOSED", "SAFETY_FAILED", "NOT_SENT"})
# A send that may not have reached the broker. Nothing else retires these rows.
_UNCONFIRMED_SEND_SAGA = ("SUBMITTING", "OUTCOME_UNKNOWN")
ORPHAN_NOT_SENT = "ORPHAN_NOT_SENT"
DEFAULT_ORPHAN_SETTLE_SECONDS = 60.0
# The entry may be at the broker with quantity still unfilled. VALIDATED is
# not here: it is written before the dispatch guard, so nothing was sent yet.
_IN_FLIGHT_SAGA = ("SUBMITTING", "OUTCOME_UNKNOWN", "ENTRY_WORKING", "PARTIALLY_FILLED")
# Sagas whose entry order may still be working. SAFETY_FAILED keeps recording
# entry events (see _record_entry_after_safety_failure); CLOSE_OWNED records
# them too until the close's expected cancel of the entry arrives.
_MAY_WORK_ENTRY_SAGA = _IN_FLIGHT_SAGA + ("SAFETY_FAILED", "CLOSE_OWNED")

_account_entry_locks: dict[str, threading.Lock] = {}
_account_entry_locks_guard = threading.Lock()


def _account_entry_lock(account_id: str) -> threading.Lock:
    """One lock per account: the final gross check and the send run under it."""
    with _account_entry_locks_guard:
        return _account_entry_locks.setdefault(account_id, threading.Lock())

_PRICE_QUANT = Decimal("0.01")


def apply_protective_order_saga_migration(migrator: SchemaMigrator) -> bool:
    """Journal migrations 30 (saga rows) and 37 (close ownership, protection groups).

    Migration 37 backfills ``account_id`` / ``conid`` from the payload. Sagas
    that were already SAFETY_FAILED before the upgrade get
    ``flatten_requested = FALSE``: the worker starts a flatten only for a
    failure seen by this version (R29). Returns whether migration 30 was
    newly applied, as before.
    """
    applied = migrator.apply(
        PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION,
        PROTECTIVE_ORDER_SAGA_MIGRATION_NAME,
        (
            """CREATE TABLE IF NOT EXISTS automated_order_sagas (
                command_id VARCHAR PRIMARY KEY,
                order_group_id VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                payload VARCHAR NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_automated_order_sagas_group
                ON automated_order_sagas(order_group_id)""",
            """CREATE TABLE IF NOT EXISTS automated_order_saga_events (
                event_id VARCHAR PRIMARY KEY,
                command_id VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
        ),
    )
    migrator.apply(PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION, "sp1_saga_close_ownership", (
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS account_id VARCHAR",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS conid INTEGER",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS close_root_id VARCHAR",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS flatten_requested BOOLEAN DEFAULT FALSE",
        """UPDATE automated_order_sagas
           SET account_id = json_extract_string(payload, '$.account_id'),
               conid = CAST(json_extract(payload, '$.conid') AS INTEGER),
               flatten_requested = FALSE
           WHERE account_id IS NULL""",
        """CREATE TABLE IF NOT EXISTS automated_order_saga_groups (
            order_group_id VARCHAR PRIMARY KEY,
            command_id VARCHAR NOT NULL,
            protection_generation INTEGER NOT NULL
        )""",
    ))
    return applied


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantize_price(value: Decimal) -> Decimal:
    return value.quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Pure bracket construction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BracketLegPlan:
    role: str  # entry | stop | take_profit
    action: str
    order_type: str
    quantity: Decimal
    limit_price: Optional[Decimal]
    stop_price: Optional[Decimal]
    parent_role: Optional[str]
    transmit: bool
    oca_group: Optional[str]
    order_ref: str


@dataclass(frozen=True)
class BracketPlan:
    order_group_id: str
    order_ref: str
    legs: tuple[BracketLegPlan, ...]
    exit_type: str
    oca_group: str


def compute_entry_limit(
    intent,
    *,
    ask: Decimal,
    bid: Decimal,
) -> Decimal:
    """LIMIT / MARKETABLE_LIMIT → concrete limit price. Never MARKET."""
    offset = _dec(intent.entry_policy.limit_offset_bps) / Decimal("10000")
    if intent.side == "BUY":
        base = ask
        price = base * (Decimal("1") + offset)
    else:
        base = bid
        price = base * (Decimal("1") - offset)
    return _quantize_price(price)


def build_bracket_plan(
    intent,
    *,
    quantity: Decimal,
    limit_price: Optional[Decimal],
    order_group_id: str,
    force_market_entry: bool = False,
) -> BracketPlan:
    """Build a broker-native bracket/OCA plan from an approved intent.

    Transmit ordering mirrors IB bracket semantics: entry and non-final
    children are ``transmit=False``; the final protective child transmits
    the whole group. Children that form a stop+target pair share an OCA
    group. Unrestricted MARKET entry is refused.
    """
    if force_market_entry or limit_price is None:
        raise ValueError(
            "automated entry refuses unrestricted MARKET; "
            "LIMIT / MARKETABLE_LIMIT with a concrete limit_price is required"
        )
    entry_type = intent.entry_policy.order_type
    if entry_type not in ("LIMIT", "MARKETABLE_LIMIT"):
        raise ValueError(f"unsupported entry order_type {entry_type!r}")

    qty = _dec(quantity)
    if qty <= 0:
        raise ValueError("quantity must be strictly positive")

    order_ref = encode_order_ref(order_group_id)
    oca_group = f"oca-{order_group_id}"
    reverse = "SELL" if intent.side == "BUY" else "BUY"
    has_target = intent.target_policy is not None
    exit_type = "BRACKET" if has_target else "STOP_LOSS"

    entry = BracketLegPlan(
        role="entry",
        action=intent.side,
        order_type="LMT",
        quantity=qty,
        limit_price=_dec(limit_price),
        stop_price=None,
        parent_role=None,
        transmit=False,
        oca_group=None,
        order_ref=order_ref,
    )

    legs: list[BracketLegPlan] = [entry]
    if has_target:
        legs.append(BracketLegPlan(
            role="take_profit",
            action=reverse,
            order_type="LMT",
            quantity=qty,
            limit_price=_dec(intent.target_policy.target_price),
            stop_price=None,
            parent_role="entry",
            transmit=False,
            oca_group=oca_group,
            order_ref=order_ref,
        ))

    stop_type = "STP" if intent.stop_policy.order_type == "STP" else "STP LMT"
    legs.append(BracketLegPlan(
        role="stop",
        action=reverse,
        order_type=stop_type,
        quantity=qty,
        limit_price=None,
        stop_price=_dec(intent.stop_policy.stop_price),
        parent_role="entry",
        transmit=True,
        oca_group=oca_group if has_target else None,
        order_ref=order_ref,
    ))

    return BracketPlan(
        order_group_id=order_group_id,
        order_ref=order_ref,
        legs=tuple(legs),
        exit_type=exit_type,
        oca_group=oca_group,
    )


def execution_spec_from_plan(plan: BracketPlan) -> dict[str, Any]:
    """Map a BracketPlan onto the existing place_expressive_order ExecutionSpec."""
    entry = plan.legs[0]
    stop = next(leg for leg in plan.legs if leg.role == "stop")
    tp = next((leg for leg in plan.legs if leg.role == "take_profit"), None)
    spec: dict[str, Any] = {
        "order_type": "LIMIT",
        "limit_price": float(entry.limit_price) if entry.limit_price is not None else None,
        "exit_type": plan.exit_type,
        "stop_loss_price": float(stop.stop_price) if stop.stop_price is not None else None,
        "tif": "DAY",
        "outside_rth": False,
        "oca_group": plan.oca_group if tp is not None else None,
    }
    if tp is not None:
        spec["take_profit_price"] = float(tp.limit_price) if tp.limit_price is not None else None
    return {k: v for k, v in spec.items() if v is not None}


# ---------------------------------------------------------------------------
# Durable saga state + broker events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrokerOrderEvent:
    order_group_id: str
    leg: str
    status: str
    filled_quantity: float
    total_quantity: float
    order_id: int
    event_id: str
    source_timestamp: dt.datetime
    order_entity_id: Optional[str] = None


class SagaRevisionConflict(RuntimeError):
    """Another writer saved this saga since it was read (R28). Re-read and apply again."""


@dataclass(frozen=True)
class SagaState:
    command_id: str
    order_group_id: str
    order_ref: str
    state: str
    account_id: str
    conid: int
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    protection_quantity: Decimal = Decimal("0")
    protection_working: bool = False
    protection_adjusted: bool = False
    entry_working: bool = False
    stop_working: bool = False
    target_working: bool = False
    stop_filled: bool = False
    target_filled: bool = False
    entry_cancelled: bool = False
    stop_rejected: bool = False
    target_rejected: bool = False
    submitted_order_ids: tuple[int, ...] = ()
    seen_event_ids: tuple[str, ...] = ()
    revision: int = 0
    error_code: Optional[str] = None
    plan_json: Optional[dict[str, Any]] = None
    close_root_id: Optional[str] = None
    expected_cancel_ids: tuple[str, ...] = ()
    handover_generation: Optional[int] = None
    protection_generation: int = 0
    active_groups: tuple[str, ...] = ()
    pending_groups: tuple[str, ...] = ()      # re-protect legs of the owning close, not yet released
    pending_protection_lost: bool = False      # a pending leg was rejected or cancelled unasked
    flatten_requested: bool = False            # SAFETY_FAILED seen by this version: the worker flattens
    # When the saga last saw the entry fill grow (broker ingest clock).
    filled_at: Optional[dt.datetime] = None
    # Broker position quantity of the conid when the entry was sent.
    baseline_position: Optional[Decimal] = None
    # Broker enumeration generation and time at the send attempt, and the
    # time submit_bracket returned or raised (see retire_orphan_reservations).
    send_generation_id: Optional[int] = None
    send_attempted_at: Optional[str] = None
    send_returned_at: Optional[str] = None

    @property
    def current_groups(self) -> tuple[str, ...]:
        """Order groups of the protection that is live now (older groups are retired)."""
        return self.active_groups or (self.order_group_id,)

    def to_payload(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "order_group_id": self.order_group_id,
            "order_ref": self.order_ref,
            "state": self.state,
            "account_id": self.account_id,
            "conid": self.conid,
            "side": self.side,
            "requested_quantity": str(self.requested_quantity),
            "filled_quantity": str(self.filled_quantity),
            "protection_quantity": str(self.protection_quantity),
            "protection_working": self.protection_working,
            "protection_adjusted": self.protection_adjusted,
            "entry_working": self.entry_working,
            "stop_working": self.stop_working,
            "target_working": self.target_working,
            "stop_filled": self.stop_filled,
            "target_filled": self.target_filled,
            "entry_cancelled": self.entry_cancelled,
            "stop_rejected": self.stop_rejected,
            "target_rejected": self.target_rejected,
            "submitted_order_ids": list(self.submitted_order_ids),
            "seen_event_ids": list(self.seen_event_ids),
            "revision": self.revision,
            "error_code": self.error_code,
            "plan_json": self.plan_json,
            "close_root_id": self.close_root_id,
            "expected_cancel_ids": list(self.expected_cancel_ids),
            "handover_generation": self.handover_generation,
            "protection_generation": self.protection_generation,
            "active_groups": list(self.active_groups),
            "pending_groups": list(self.pending_groups),
            "pending_protection_lost": self.pending_protection_lost,
            "flatten_requested": self.flatten_requested,
            "filled_at": None if self.filled_at is None else self.filled_at.isoformat(),
            "baseline_position": (
                None if self.baseline_position is None else str(self.baseline_position)
            ),
            "send_generation_id": self.send_generation_id,
            "send_attempted_at": self.send_attempted_at,
            "send_returned_at": self.send_returned_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SagaState":
        return cls(
            command_id=payload["command_id"],
            order_group_id=payload["order_group_id"],
            order_ref=payload["order_ref"],
            state=payload["state"],
            account_id=payload["account_id"],
            conid=int(payload["conid"]),
            side=payload["side"],
            requested_quantity=_dec(payload["requested_quantity"]),
            filled_quantity=_dec(payload.get("filled_quantity", "0")),
            protection_quantity=_dec(payload.get("protection_quantity", "0")),
            protection_working=bool(payload.get("protection_working", False)),
            protection_adjusted=bool(payload.get("protection_adjusted", False)),
            entry_working=bool(payload.get("entry_working", False)),
            stop_working=bool(payload.get("stop_working", False)),
            target_working=bool(payload.get("target_working", False)),
            stop_filled=bool(payload.get("stop_filled", False)),
            target_filled=bool(payload.get("target_filled", False)),
            entry_cancelled=bool(payload.get("entry_cancelled", False)),
            stop_rejected=bool(payload.get("stop_rejected", False)),
            target_rejected=bool(payload.get("target_rejected", False)),
            submitted_order_ids=tuple(payload.get("submitted_order_ids") or ()),
            seen_event_ids=tuple(payload.get("seen_event_ids") or ()),
            revision=int(payload.get("revision", 0)),
            error_code=payload.get("error_code"),
            plan_json=payload.get("plan_json"),
            close_root_id=payload.get("close_root_id"),
            expected_cancel_ids=tuple(payload.get("expected_cancel_ids") or ()),
            handover_generation=payload.get("handover_generation"),
            protection_generation=int(payload.get("protection_generation", 0)),
            active_groups=tuple(payload.get("active_groups") or ()),
            pending_groups=tuple(payload.get("pending_groups") or ()),
            pending_protection_lost=bool(payload.get("pending_protection_lost", False)),
            flatten_requested=bool(payload.get("flatten_requested", False)),
            filled_at=(
                dt.datetime.fromisoformat(payload["filled_at"])
                if payload.get("filled_at") else None
            ),
            baseline_position=(
                _dec(payload["baseline_position"])
                if payload.get("baseline_position") is not None else None
            ),
            send_generation_id=(
                int(payload["send_generation_id"])
                if payload.get("send_generation_id") is not None else None
            ),
            send_attempted_at=payload.get("send_attempted_at"),
            send_returned_at=payload.get("send_returned_at"),
        )


class _BrokerTraceFound(Exception):
    """The broker may hold the order group: the retirement must not commit."""


@dataclass(frozen=True)
class BrokerEnumeration:
    """A complete, promoted broker enumeration: its id and when it began."""

    generation_id: int
    started_at: dt.datetime


class OrphanEvidencePort(Protocol):
    def latest_complete_enumeration(self, account_id: str) -> BrokerEnumeration:
        """Newest complete enumeration; raises when none can be read."""
        ...

    def entry_orders(self, account_id: str, order_group_id: str) -> list[Any]:
        """Broker entry order rows of the group (deleted and terminal included)."""
        ...

    def has_trace_in_tx(
        self, conn, account_id: str, order_group_id: str, conid: int, since: dt.datetime,
    ) -> bool:
        """True if the broker may hold an order or execution of the group.

        Runs inside the caller's journal transaction, so broker writes cannot
        land between this read and the caller's write. Must answer True when
        it cannot rule one out; raises when unreadable.
        """
        ...


class BracketDispatchPort(Protocol):
    def submit_bracket(self, *, plan: BracketPlan, intent: Any, account_id: str) -> Any: ...


class ProtectiveOrderSagaStore:
    def __init__(self, db):
        self._db = db

    def load(self, command_id: str) -> Optional[SagaState]:
        row = self._db.execute(
            "SELECT payload FROM automated_order_sagas WHERE command_id = ?",
            [command_id], fetch="one",
        )
        if row is None:
            return None
        return SagaState.from_payload(json.loads(row[0]))

    def load_by_group(self, order_group_id: str) -> Optional[tuple[SagaState, int]]:
        """The saga that owns ``order_group_id`` and the protection generation of that group.

        Re-protect groups are in ``automated_order_saga_groups`` (a pending
        group has the generation after the current one); the entry bracket
        group is the saga row itself (generation 0).
        """
        row = self._db.execute(
            "SELECT s.payload, g.protection_generation FROM automated_order_saga_groups g "
            "JOIN automated_order_sagas s ON s.command_id = g.command_id WHERE g.order_group_id = ?",
            [order_group_id], fetch="one",
        )
        if row is None:
            row = self._db.execute(
                "SELECT payload, 0 FROM automated_order_sagas WHERE order_group_id = ?",
                [order_group_id], fetch="one",
            )
        if row is None:
            return None
        return SagaState.from_payload(json.loads(row[0])), int(row[1])

    def _load_many(self, where: str, params: list) -> list[SagaState]:
        rows = self._db.execute(
            f"SELECT payload FROM automated_order_sagas WHERE {where} ORDER BY command_id",
            params, fetch="all",
        )
        return [SagaState.from_payload(json.loads(r[0])) for r in rows]

    def load_live(self, account_id: str, conid: Optional[int] = None) -> list[SagaState]:
        """Every saga that is not terminal: open ones and ones a close owns."""
        where = "account_id = ? AND state NOT IN ('CLOSED', 'SAFETY_FAILED')"
        params: list = [account_id]
        if conid is not None:
            where += " AND conid = ?"
            params.append(int(conid))
        return self._load_many(where, params)

    def load_by_close_root(self, close_root_id: str) -> list[SagaState]:
        return self._load_many("state = 'CLOSE_OWNED' AND close_root_id = ?", [close_root_id])

    def load_flatten_requested(self, account_id: str) -> list[SagaState]:
        return self._load_many("account_id = ? AND state = 'SAFETY_FAILED' AND flatten_requested", [account_id])

    def save_in_tx(self, conn, state: SagaState, now: dt.datetime) -> None:
        """Insert a new saga, or update one with a revision check (R28).

        An update must be built from the stored revision (``state.revision``
        is that plus one); otherwise another writer won and this one raises
        ``SagaRevisionConflict``. ``order_group_id`` never changes, so the
        update never touches the indexed column.
        """
        payload = json.dumps(state.to_payload(), sort_keys=True, default=str)
        stored = conn.execute(
            "SELECT CAST(json_extract(payload, '$.revision') AS INTEGER) FROM automated_order_sagas "
            "WHERE command_id = ?", [state.command_id]).fetchone()
        if stored is None:
            conn.execute(
                "INSERT INTO automated_order_sagas (command_id, order_group_id, state, payload, updated_at, "
                "account_id, conid, close_root_id, flatten_requested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [state.command_id, state.order_group_id, state.state, payload, now,
                 state.account_id, int(state.conid), state.close_root_id, state.flatten_requested],
            )
        elif int(stored[0]) != state.revision - 1:
            raise SagaRevisionConflict(
                f"saga {state.command_id} is at revision {stored[0]}; this write was built on {state.revision - 1}")
        else:
            conn.execute(
                "UPDATE automated_order_sagas SET state = ?, payload = ?, updated_at = ?, account_id = ?, "
                "conid = ?, close_root_id = ?, flatten_requested = ? WHERE command_id = ?",
                [state.state, payload, now, state.account_id, int(state.conid), state.close_root_id,
                 state.flatten_requested, state.command_id],
            )
        groups = [(g, state.protection_generation) for g in state.active_groups] + \
                 [(g, state.protection_generation + 1) for g in state.pending_groups]
        for group, generation in groups:
            conn.execute(
                "INSERT INTO automated_order_saga_groups VALUES (?, ?, ?) ON CONFLICT (order_group_id) "
                "DO UPDATE SET command_id = excluded.command_id, protection_generation = excluded.protection_generation",
                [group, state.command_id, generation],
            )

    def in_flight_entries(
        self, account_id: str, exclude_command_id: str,
    ) -> tuple[InFlightEntry, ...]:
        """BUY entries of this account whose exposure the broker may not show yet.

        The unfilled part counts until a broker cancel or reject of the entry
        is recorded. The filled part counts in every state until an exit leg
        fills; the dispatch guard drops it once the broker snapshot proves it.
        """
        rows = self._db.execute(
            "SELECT payload, updated_at FROM automated_order_sagas WHERE state <> 'VALIDATED'",
            fetch="all",
        )
        entries = []
        for payload, updated_at in rows or ():
            state = SagaState.from_payload(json.loads(payload))
            if (
                state.account_id != account_id
                or state.command_id == exclude_command_id
                or state.side != "BUY"
            ):
                continue
            unfilled = Decimal("0")
            if state.state in _MAY_WORK_ENTRY_SAGA and not state.entry_cancelled:
                unfilled = max(Decimal("0"), state.requested_quantity - state.filled_quantity)
            filled = Decimal("0")
            if not (state.stop_filled or state.target_filled):
                filled = state.filled_quantity
            if unfilled <= 0 and filled <= 0:
                continue
            entries.append(InFlightEntry(
                order_group_id=state.order_group_id,
                conid=state.conid,
                unfilled_quantity=float(unfilled),
                filled_quantity=float(filled),
                limit_price=_entry_limit_price(state),
                # Rows written before filled_at existed: the last update is
                # no earlier than the fill.
                filled_at=state.filled_at or (_as_utc(updated_at) if filled > 0 else None),
                baseline_position=(
                    None if state.baseline_position is None else float(state.baseline_position)
                ),
            ))
        return tuple(entries)

    def require_revision_in_tx(self, conn, command_id: str, revision: int) -> None:
        row = conn.execute(
            "SELECT payload FROM automated_order_sagas WHERE command_id = ?", [command_id],
        ).fetchone()
        current = None if row is None else SagaState.from_payload(json.loads(row[0])).revision
        if current != revision:
            raise ValueError(f"saga {command_id} changed: revision {current}, expected {revision}")

    def terminal_entries(self, account_id: str) -> tuple[SagaState, ...]:
        """NOT_SENT and CLOSED rows whose exit never filled: the saga thinks
        they hold at most what they recorded."""
        rows = self._db.execute(
            "SELECT payload FROM automated_order_sagas WHERE state IN ('NOT_SENT', 'CLOSED')",
            fetch="all",
        )
        states = (SagaState.from_payload(json.loads(payload)) for (payload,) in rows or ())
        return tuple(
            state for state in states
            if state.account_id == account_id and not (state.stop_filled or state.target_filled)
        )

    def unconfirmed_sends(self, account_id: str) -> tuple[SagaState, ...]:
        placeholders = ", ".join("?" for _ in _UNCONFIRMED_SEND_SAGA)
        rows = self._db.execute(
            f"SELECT payload FROM automated_order_sagas WHERE state IN ({placeholders})",
            list(_UNCONFIRMED_SEND_SAGA), fetch="all",
        )
        states = (SagaState.from_payload(json.loads(payload)) for (payload,) in rows or ())
        return tuple(state for state in states if state.account_id == account_id)

    def seen_event_in_tx(self, conn, event_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM automated_order_saga_events WHERE event_id = ?", [event_id],
        ).fetchone()
        return row is not None

    def record_event_in_tx(self, conn, event_id: str, command_id: str, now: dt.datetime) -> None:
        conn.execute(
            "INSERT INTO automated_order_saga_events (event_id, command_id, recorded_at) "
            "VALUES (?, ?, ?)",
            [event_id, command_id, now],
        )


def _leg_plan_to_json(leg: BracketLegPlan) -> dict[str, Any]:
    return {
        "role": leg.role,
        "action": leg.action,
        "order_type": leg.order_type,
        "quantity": str(leg.quantity),
        "limit_price": None if leg.limit_price is None else str(leg.limit_price),
        "stop_price": None if leg.stop_price is None else str(leg.stop_price),
        "parent_role": leg.parent_role,
        "transmit": leg.transmit,
        "oca_group": leg.oca_group,
        "order_ref": leg.order_ref,
    }


def _plan_to_json(plan: BracketPlan) -> dict[str, Any]:
    return {
        "order_group_id": plan.order_group_id,
        "order_ref": plan.order_ref,
        "exit_type": plan.exit_type,
        "oca_group": plan.oca_group,
        "legs": [_leg_plan_to_json(leg) for leg in plan.legs],
    }


def _apply_entry_leg(state: SagaState, event: BrokerOrderEvent) -> SagaState:
    """Record an entry order event on the saga fields; never changes ``state.state``.

    A cancel or reject ends the unfilled part (``entry_cancelled``).
    """
    status = event.status
    filled = _dec(event.filled_quantity)
    next_state = state
    if status in _WORKING_STATUSES:
        next_state = replace(next_state, entry_working=True)
    if status in _CANCELLED_STATUSES or status in _REJECTED_STATUSES:
        next_state = replace(next_state, entry_cancelled=True, entry_working=False)
    if filled > next_state.filled_quantity:
        next_state = replace(
            next_state,
            filled_quantity=filled,
            protection_quantity=filled,
            protection_adjusted=filled < next_state.requested_quantity,
        )
    if status in _FILLED_STATUSES:
        qty = max(filled, next_state.filled_quantity, next_state.requested_quantity)
        next_state = replace(
            next_state, filled_quantity=qty, protection_quantity=qty, entry_working=False,
        )
    if next_state.filled_quantity > state.filled_quantity:
        next_state = replace(next_state, filled_at=_as_utc(event.source_timestamp))
    return next_state


def _entry_limit_price(state: SagaState) -> float:
    try:
        price = float(state.plan_json["legs"][0]["limit_price"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"saga {state.command_id} has no entry limit price") from exc
    if not price > 0:
        raise ValueError(f"saga {state.command_id} has an invalid entry limit price")
    return price


def _is_reduction(approval) -> bool:
    direction = getattr(approval, "risk_direction", None)
    return str(getattr(direction, "value", direction)) == "REDUCING"


def _with_allocation_evidence(
    approval, artifact, decision, entry_limit_price, in_flight_entries=(),
):
    """Freeze the ceiling the approval was granted under so dispatch can re-check it."""
    ceiling = getattr(decision, "effective_gross_ceiling", None)
    if ceiling is None:
        return approval  # DispatchGuard refuses an entry that has no evidence
    return replace(approval, allocation=AllocationDispatchEvidence(
        artifact_digest=artifact.artifact_id,
        artifact_max_gross=float(artifact.max_gross_allocation),
        authority_digest=getattr(decision, "authority_digest", None),
        effective_gross_ceiling=float(ceiling),
        entry_limit_price=float(entry_limit_price),
        in_flight_entries=tuple(in_flight_entries),
    ))


class ProtectiveOrderSaga:
    """Durable protective-entry saga driven by broker events."""

    def __init__(
        self,
        *,
        journal: Any,
        ledger: Any,
        dispatch: BracketDispatchPort,
        dispatch_guard: Any,
        session_risk: Any,
        breaker: Any,
        liquidation: Any,
        account_id: str,
        account_mode: str,
        now: Callable[[], dt.datetime],
        db: Any,
        liquidation_deadline_seconds: float = 300.0,
        orphan_evidence: Optional[OrphanEvidencePort] = None,
        orphan_settle_seconds: float = DEFAULT_ORPHAN_SETTLE_SECONDS,
    ):
        self._journal = journal
        self._ledger = ledger
        self._dispatch = dispatch
        self._dispatch_guard = dispatch_guard
        self._session_risk = session_risk
        self._breaker = breaker
        self._liquidation = liquidation
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._store = ProtectiveOrderSagaStore(db)
        self._liquidation_deadline_seconds = liquidation_deadline_seconds
        self._orphan_evidence = orphan_evidence
        self._orphan_settle = dt.timedelta(seconds=orphan_settle_seconds)

    # -- public API --------------------------------------------------------

    def start(
        self,
        *,
        intent,
        approval,
        request,
        artifact,
        session_state,
        allocation,
    ) -> SagaState:
        existing = self._store.load(intent.command_id)
        if existing is not None:
            return existing

        order_group_id = f"og-{intent.command_id}"
        order_ref = encode_order_ref(order_group_id)
        now = self._now_utc()

        # 1) Session / liquidity risk (Task 4) — before any IB side effect.
        try:
            decision = self._session_risk.evaluate(
                intent, artifact, approval, session_state, allocation,
            )
        except Exception as ex:
            # A failed risk read/evaluation is a known no-submit outcome, not
            # an ambiguous broker acknowledgement. Persist the refusal.
            state = SagaState(
                command_id=intent.command_id,
                order_group_id=order_group_id,
                order_ref=order_ref,
                state="CLOSED",
                account_id=self._account_id,
                conid=intent.conid,
                side=intent.side,
                requested_quantity=_dec(intent.requested_quantity or 0),
                error_code=getattr(ex, "code", None) or "AUTOMATION_RISK_UNAVAILABLE",
            )
            self._persist(state, now, from_state=None)
            return state
        if not decision.approved:
            code = decision.reason_codes[0] if decision.reason_codes else "RISK_REJECTED"
            state = SagaState(
                command_id=intent.command_id,
                order_group_id=order_group_id,
                order_ref=order_ref,
                state="CLOSED",
                account_id=self._account_id,
                conid=intent.conid,
                side=intent.side,
                requested_quantity=_dec(intent.requested_quantity or 0),
                error_code=str(code),
            )
            self._persist(state, now, from_state=None)
            return state

        quantity = _dec(decision.approved_quantity or intent.requested_quantity or 0)
        if quantity <= 0:
            state = SagaState(
                command_id=intent.command_id,
                order_group_id=order_group_id,
                order_ref=order_ref,
                state="CLOSED",
                account_id=self._account_id,
                conid=intent.conid,
                side=intent.side,
                requested_quantity=Decimal("0"),
                error_code="QUANTITY_INVALID",
            )
            self._persist(state, now, from_state=None)
            return state

        # Quote → limit (MARKETABLE_LIMIT offset or plain LIMIT offset).
        quote = approval.market.quote if approval.market is not None else None
        if quote is None:
            state = SagaState(
                command_id=intent.command_id,
                order_group_id=order_group_id,
                order_ref=order_ref,
                state="CLOSED",
                account_id=self._account_id,
                conid=intent.conid,
                side=intent.side,
                requested_quantity=quantity,
                error_code="EXECUTABLE_QUOTE_MISSING",
            )
            self._persist(state, now, from_state=None)
            return state

        ask = _dec(quote.ask if quote.ask is not None else quote.price)
        bid = _dec(quote.bid if quote.bid is not None else quote.price)
        limit_price = compute_entry_limit(intent, ask=ask, bid=bid)
        plan = build_bracket_plan(
            intent, quantity=quantity, limit_price=limit_price,
            order_group_id=order_group_id,
        )

        validated = SagaState(
            command_id=intent.command_id,
            order_group_id=order_group_id,
            order_ref=order_ref,
            state="VALIDATED",
            account_id=self._account_id,
            conid=intent.conid,
            side=intent.side,
            requested_quantity=quantity,
            plan_json=_plan_to_json(plan),
            revision=1,
        )
        self._persist(validated, now, from_state=None)

        # A reduction never waits for, or depends on, in-flight entries.
        lock = (
            contextlib.nullcontext() if _is_reduction(approval)
            else _account_entry_lock(self._account_id)
        )
        with lock:
            return self._guard_and_submit(
                validated, plan, intent, approval, request, artifact, decision,
                limit_price, now,
            )

    def _guard_and_submit(
        self, validated, plan, intent, approval, request, artifact, decision,
        limit_price, now,
    ) -> SagaState:
        """Final gross check and send. Callers hold the account entry lock for entries.

        The SUBMITTING row written here is the durable reservation: later
        checks count it until the broker snapshot proves its exposure.
        """
        in_flight: tuple[InFlightEntry, ...] = ()
        if not _is_reduction(approval):
            try:
                in_flight = self._store.in_flight_entries(
                    self._account_id, exclude_command_id=validated.command_id,
                )
            except Exception:
                closed = replace(
                    validated, state="CLOSED", error_code="IN_FLIGHT_STATE_UNAVAILABLE",
                    revision=validated.revision + 1,
                )
                self._persist(closed, now, from_state="VALIDATED")
                return closed

        # 2) Re-run P1 DispatchGuard immediately before first IB side effect.
        try:
            self._dispatch_guard.revalidate(
                _with_allocation_evidence(approval, artifact, decision, limit_price, in_flight),
                request, now,
            )
        except DispatchGuardError as ex:
            closed = replace(
                validated, state="CLOSED", error_code=ex.code, revision=validated.revision + 1,
            )
            self._persist(closed, now, from_state="VALIDATED")
            return closed

        send_generation_id = None
        if self._orphan_evidence is not None:
            try:
                send_generation_id = self._orphan_evidence.latest_complete_enumeration(
                    self._account_id,
                ).generation_id
            except Exception:
                closed = replace(
                    validated, state="CLOSED", error_code="BROKER_GENERATION_UNAVAILABLE",
                    revision=validated.revision + 1,
                )
                self._persist(closed, now, from_state="VALIDATED")
                return closed

        submitting = replace(
            validated, state="SUBMITTING", revision=validated.revision + 1,
            send_generation_id=send_generation_id, send_attempted_at=now.isoformat(),
            baseline_position=_dec(approval.broker.reducible_quantity(validated.conid)),
        )
        self._persist(submitting, now, from_state="VALIDATED")

        # 3) Irreversible boundary — submit via existing bracket path.
        try:
            submitted = self._dispatch.submit_bracket(
                plan=plan, intent=intent, account_id=self._account_id,
            )
        except BrokerRejectedError:
            returned_at = self._now_utc().isoformat()
            return self._after_dispatch(intent.command_id, now, lambda s: replace(
                s, state="CLOSED", error_code="BROKER_REJECTED", send_returned_at=returned_at))
        except Exception:
            returned_at = self._now_utc().isoformat()
            return self._after_dispatch(intent.command_id, now, lambda s: replace(
                s, state="OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
                send_returned_at=returned_at))

        returned_at = self._now_utc().isoformat()
        order_ids = tuple(int(x) for x in (getattr(submitted, "order_ids", None) or ()))
        # Submit returns correlation ids only — broker events advance working.
        return self._after_dispatch(
            intent.command_id, now,
            lambda s: replace(s, submitted_order_ids=order_ids, send_returned_at=returned_at),
            always=True)

    def _after_dispatch(self, command_id: str, now: dt.datetime,
                        change: Callable[[SagaState], SagaState], *, always: bool = False) -> SagaState:
        """The write after ``submit_bracket``, from a fresh read under the revision check (N1).

        The ingest thread may have saved an event of this bracket while the
        call waited. A state change is made only while the saga is still
        SUBMITTING (an event already moved it on, so the broker has the
        order); ``always`` changes apply on top of whatever the ingest wrote.
        """
        def attempt() -> SagaState:
            current = self._store.load(command_id)
            if current.state != "SUBMITTING" and not always:
                return current
            updated = replace(change(current), revision=current.revision + 1)
            self._persist(updated, now, from_state=current.state)
            return updated
        return self._retrying(attempt)

    def resume(self, command_id: str) -> Optional[SagaState]:
        return self._store.load(command_id)

    def on_broker_event(self, event: BrokerOrderEvent) -> SagaState:
        """Apply one broker event. A lost race with another writer re-reads and applies again (R28)."""
        return self._retrying(lambda: self._apply_broker_event(event))

    def _retrying(self, attempt: Callable[[], Any]) -> Any:
        """Read, change and save under the revision check; on a conflict start from a fresh read."""
        for _ in range(_SAVE_ATTEMPTS - 1):
            try:
                return attempt()
            except SagaRevisionConflict:
                continue
        return attempt()

    def _apply_broker_event(self, event: BrokerOrderEvent) -> SagaState:
        found = self._store.load_by_group(event.order_group_id)
        if found is None:
            raise KeyError(f"no saga for order_group_id={event.order_group_id!r}")
        state, group_generation = found
        if event.event_id in state.seen_event_ids:
            return state
        if state.state == "SAFETY_FAILED" and event.leg == "entry":
            return self._record_entry_after_safety_failure(state, event)
        if state.state == "NOT_SENT":
            return self._reopen_terminal_row(state, event)
        if state.state in _TERMINAL_SAGA:
            return state

        now = self._now_utc()
        if group_generation > state.protection_generation:
            # A re-protect leg of the close that owns this saga, before its release.
            return self._on_pending_event(state, event, now)
        if group_generation < state.protection_generation:
            # A retired leg: record the event, never let it change today's protection.
            return self._record_only(state, event, now)
        if state.state == "CLOSE_OWNED":
            return self._on_owned_event(state, event, now)
        updated = self._apply_event(state, event)
        if updated.state == "SAFETY_FAILED" and state.state != "SAFETY_FAILED":
            updated = replace(updated, flatten_requested=True)
        updated = replace(
            updated,
            seen_event_ids=state.seen_event_ids + (event.event_id,),
            revision=state.revision + 1,
        )
        self._persist(updated, now, from_state=state.state, event_id=event.event_id)

        if updated.state == "SAFETY_FAILED" and state.state != "SAFETY_FAILED":
            self._trip_and_liquidate(updated, now)
        return updated

    def _record_only(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        recorded = replace(state, seen_event_ids=state.seen_event_ids + (event.event_id,),
                           revision=state.revision + 1)
        self._persist(recorded, now, from_state=state.state, event_id=event.event_id)
        return recorded

    def _lost(self, state: SagaState, event: BrokerOrderEvent) -> bool:
        """R14: only a cancel of a ref the close asked to cancel is expected."""
        expected = event.order_entity_id is not None and event.order_entity_id in state.expected_cancel_ids
        return event.status in _REJECTED_STATUSES or (event.status in _CANCELLED_STATUSES and not expected)

    def _on_owned_event(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        if self._lost(state, event):
            failed = replace(
                state, state="SAFETY_FAILED", error_code="PROTECTION_LOST_DURING_CLOSE", flatten_requested=True,
                seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1,
            )
            self._persist(failed, now, from_state=state.state, event_id=event.event_id)
            self._trip_and_liquidate(failed, now)
            return failed
        owned = replace(
            self._owned_bookkeeping(state, event),
            seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1,
        )
        self._persist(owned, now, from_state=state.state, event_id=event.event_id)
        return owned

    def _on_pending_event(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        """A replacement leg before release. The close judges its own legs (and escalates); the saga
        only remembers a loss, so the release cannot report protection that is gone (R2-4)."""
        lost = state.state == "CLOSE_OWNED" and self._lost(state, event)
        recorded = replace(state, pending_protection_lost=state.pending_protection_lost or lost,
                           seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1)
        self._persist(recorded, now, from_state=state.state, event_id=event.event_id)
        return recorded

    @staticmethod
    def _owned_bookkeeping(state: SagaState, event: BrokerOrderEvent) -> SagaState:
        working = event.status in _WORKING_STATUSES
        filled = event.status in _FILLED_STATUSES
        if event.leg == "entry":
            # Fills and the cancel of the entry keep the gross reservation true.
            return _apply_entry_leg(state, event)
        if event.leg == "stop":
            return replace(state, stop_working=working, stop_filled=state.stop_filled or filled)
        if event.leg == "take_profit":
            return replace(state, target_working=working, target_filled=state.target_filled or filled)
        return state

    # -- close ownership (ProtectionOwnershipPort) --------------------------

    @staticmethod
    def _prices_from_plan(state: SagaState):
        from trader.trading.liquidation_service import HandoverInfo
        legs = (state.plan_json or {}).get("legs", [])
        stop = next((leg for leg in legs if leg.get("role") == "stop"), None)
        target = next((leg for leg in legs if leg.get("role") == "take_profit"), None)
        return HandoverInfo(
            stop_price=None if stop is None or stop.get("stop_price") is None else float(stop["stop_price"]),
            target_price=None if target is None or target.get("limit_price") is None else float(target["limit_price"]),
        )

    @staticmethod
    def _own(states: list[SagaState], close_root_id: str, cancels, generation: int) -> list[SagaState]:
        """CLOSE_OWNED with the expected cancels merged in; a pending leg's cancel is expected too."""
        owned = []
        for state in states:
            groups = set(state.current_groups) | set(state.pending_groups)
            mine = {c.order_entity_id for c in cancels if c.order_group_id in groups}
            expected = tuple(sorted(set(state.expected_cancel_ids) | mine))
            if (state.state, state.close_root_id, state.expected_cancel_ids) == ("CLOSE_OWNED", close_root_id, expected):
                continue  # idempotent: already handed over with these refs
            owned.append(replace(state, state="CLOSE_OWNED", close_root_id=close_root_id,
                                 expected_cancel_ids=expected, handover_generation=generation,
                                 revision=state.revision + 1))
        return owned

    def handover(self, *, account_id: str, conid: int, close_root_id: str, cancels, generation: int,
                 now: dt.datetime):
        from trader.trading.liquidation_service import HandoverInfo

        def attempt():
            states = self._store.load_live(account_id, conid)
            self._persist_all([(s, None) for s in self._own(states, close_root_id, cancels, generation)],
                              _as_utc(now))
            return self._prices_from_plan(states[0]) if states else HandoverInfo(None, None)
        return self._retrying(attempt)

    def handover_account(self, *, account_id: str, close_root_id: str, cancels, generation: int,
                         now: dt.datetime) -> None:
        def attempt():
            states = self._store.load_live(account_id)
            self._persist_all([(s, None) for s in self._own(states, close_root_id, cancels, generation)],
                              _as_utc(now))
        self._retrying(attempt)

    def expect_reprotect(self, *, close_root_id: str, groups: tuple[str, ...], now: dt.datetime) -> None:
        """Bind the replacement legs to the saga before they are sent, as pending groups."""
        def attempt():
            changed = [replace(s, pending_groups=tuple(groups), revision=s.revision + 1)
                       for s in self._store.load_by_close_root(close_root_id)[:1]
                       if s.pending_groups != tuple(groups)]
            self._persist_all([(s, None) for s in changed], _as_utc(now))
        self._retrying(attempt)

    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float, stop_group: str,
                              stop_status: str, target_group: Optional[str], target_status: Optional[str],
                              now: dt.datetime, protection_problem: Optional[str] = None) -> None:
        """Back to protection of the remainder, judged from the legs' broker status at release.

        The new legs become the only live protection (a new protection
        generation). A leg that is not working, a pending leg lost while the
        close owned the saga, or a ``protection_problem`` the close found
        under its release hold (#22/#25 round 7) is today's incident path.
        """
        def attempt():
            states = self._store.load_by_close_root(close_root_id)
            if not states:
                return None
            keeper, merged = states[0], states[1:]
            remaining = _dec(remaining_quantity)
            groups = (stop_group,) + ((target_group,) if target_group else ())
            base = replace(
                keeper, state="PROTECTED", close_root_id=None, expected_cancel_ids=(), handover_generation=None,
                protection_generation=keeper.protection_generation + 1, active_groups=groups, pending_groups=(),
                pending_protection_lost=False, requested_quantity=remaining, filled_quantity=remaining,
                protection_quantity=remaining, protection_working=False, stop_working=False,
                target_working=False, stop_filled=False, target_filled=False, stop_rejected=False,
                target_rejected=False, entry_working=False, entry_cancelled=False, error_code=None,
                revision=keeper.revision + 1,
            )
            released = self._apply_event(base, BrokerOrderEvent(
                stop_group, "stop", stop_status, 0.0, float(remaining), 0, f"release:{close_root_id}:stop", now))
            if target_group:
                released = self._apply_event(released, BrokerOrderEvent(
                    target_group, "take_profit", target_status or "Unknown", 0.0, float(remaining), 0,
                    f"release:{close_root_id}:target", now))
            # #22 round 5: the stop row can change after DONE committed and before this read. Live
            # event handling counts PendingCancel (and not-yet-accepted statuses) as working; at
            # release they are not protection.
            stop_accepted = stop_status in _RELEASE_STOP_STATUSES
            if protection_problem or keeper.pending_protection_lost or not stop_accepted \
                    or released.state not in ("PROTECTED", "EXITING", "CLOSED"):
                released = replace(released, state="SAFETY_FAILED", flatten_requested=True,
                                   error_code=released.error_code or "PROTECTION_LOST_DURING_CLOSE")
            closed = [replace(s, state="CLOSED", close_root_id=None, error_code="PROTECTION_MERGED",
                              revision=s.revision + 1) for s in merged]
            self._persist_all([(released, "CLOSE_OWNED")] + [(s, "CLOSE_OWNED") for s in closed], _as_utc(now))
            return released
        released = self._retrying(attempt)
        if released is not None and released.state == "SAFETY_FAILED":
            self._trip_and_liquidate(released, _as_utc(now))

    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None:
        def attempt():
            closed = [replace(s, state="CLOSED", error_code=None, close_root_id=None, stop_working=False,
                              target_working=False, revision=s.revision + 1)
                      for s in self._store.load_by_close_root(close_root_id)]
            self._persist_all([(s, "CLOSE_OWNED") for s in closed], _as_utc(now))
        self._retrying(attempt)

    def unhandled_failures(self, account_id: str) -> list[str]:
        """SAFETY_FAILED sagas this version asked to flatten; the worker makes sure each one has a root.

        A saga that was already SAFETY_FAILED before the upgrade is not here (R29):
        no flatten starts on the first deploy without a fresh trigger.
        """
        return [s.command_id for s in self._store.load_flatten_requested(account_id)]

    def _record_entry_after_safety_failure(
        self, state: SagaState, event: BrokerOrderEvent,
    ) -> SagaState:
        """Keep the reservation true after SAFETY_FAILED: a later fill or cancel
        of the entry changes what the account may hold. Liquidation already runs."""
        updated = replace(
            _apply_entry_leg(state, event),
            seen_event_ids=state.seen_event_ids + (event.event_id,),
            revision=state.revision + 1,
        )
        self._persist(updated, self._now_utc(), from_state=state.state, event_id=event.event_id)
        return updated
    # -- orphan reservations -----------------------------------------------

    def retire_orphan_reservations(self) -> tuple[str, ...]:
        """Retire SUBMITTING / OUTCOME_UNKNOWN rows the broker proves were never sent.

        Such a row counts toward gross until a broker event moves it. After a
        crash before the send, or a send that never reached IB, no event comes.
        A row is retired only when a complete broker enumeration that began
        after the send attempt, and after the generation recorded at the send,
        shows no order group and no execution for it. Anything unreadable
        keeps the row: the reservation fails closed. Returns the retired
        command ids.
        """
        if self._orphan_evidence is None:
            return ()
        lock = _account_entry_lock(self._account_id)
        # Never wait: an entry send holds this lock for up to the dispatch timeout.
        if not lock.acquire(blocking=False):
            return ()
        try:
            return self._retire_orphans()
        finally:
            lock.release()

    def _retire_orphans(self) -> tuple[str, ...]:
        candidates = self._store.unconfirmed_sends(self._account_id)
        if not candidates:
            return ()
        now = self._now_utc()
        try:
            enumeration = self._orphan_evidence.latest_complete_enumeration(self._account_id)
        except Exception:
            logging.warning("orphan reservation check skipped: no complete broker enumeration")
            return ()
        retired = []
        for state in candidates:
            if self._retire_if_orphan(state, enumeration, now):
                retired.append(state.command_id)
        return tuple(retired)

    def _retire_if_orphan(
        self, state: SagaState, enumeration: BrokerEnumeration, now: dt.datetime,
    ) -> bool:
        if not self._enumeration_postdates_send(state, enumeration, now):
            return False
        since = _as_utc(dt.datetime.fromisoformat(state.send_attempted_at))

        def require_no_trace(conn) -> None:
            if self._orphan_evidence.has_trace_in_tx(
                conn, state.account_id, state.order_group_id, state.conid, since,
            ):
                raise _BrokerTraceFound(state.order_group_id)

        retired = replace(
            state, state="NOT_SENT", error_code=ORPHAN_NOT_SENT, revision=state.revision + 1,
        )
        try:
            self._persist(
                retired, now, from_state=state.state, expected_revision=state.revision,
                check_in_tx=require_no_trace,
            )
        except _BrokerTraceFound:
            return False
        except Exception:
            logging.exception("could not retire orphan reservation %s", state.command_id)
            return False
        logging.error(
            "INCIDENT %s: saga %s (order group %s) was %s with no broker order or "
            "execution in broker generation %s; reservation released",
            ORPHAN_NOT_SENT, state.command_id, state.order_group_id, state.state,
            enumeration.generation_id,
        )
        return True

    def _enumeration_postdates_send(
        self, state: SagaState, enumeration: BrokerEnumeration, now: dt.datetime,
    ) -> bool:
        # Without the returned marker the send may still be running (or the
        # process died inside it): nothing proves when it could reach IB.
        if (
            state.send_generation_id is None
            or state.send_attempted_at is None
            or state.send_returned_at is None
        ):
            return False
        returned_at = _as_utc(dt.datetime.fromisoformat(state.send_returned_at))
        return (
            enumeration.generation_id > state.send_generation_id
            and _as_utc(enumeration.started_at) > returned_at
            and now - returned_at >= self._orphan_settle
        )

    def reconcile_terminal_entries(self) -> tuple[str, ...]:
        """After a generation promotion: route fills the saga never saw.

        Promotion applies broker orders without saga events. A NOT_SENT or
        CLOSED row whose broker entry order shows more fill than the row
        recorded takes the same path as a late fill event. Returns the
        command ids it reopened.
        """
        if self._orphan_evidence is None:
            return ()
        reopened = []
        for state in self._store.terminal_entries(self._account_id):
            unseen = self._unseen_entry_fill_event(state)
            if unseen is not None and unseen.event_id not in state.seen_event_ids:
                self._reopen_terminal_row(state, unseen)
                reopened.append(state.command_id)
        return tuple(reopened)

    def _unseen_entry_fill_event(self, state: SagaState) -> Optional[BrokerOrderEvent]:
        orders = self._orphan_evidence.entry_orders(state.account_id, state.order_group_id)
        filled = [order for order in orders if _dec(order.filled_quantity) > state.filled_quantity]
        if not filled:
            return None
        order = max(filled, key=lambda row: row.filled_quantity)
        return BrokerOrderEvent(
            order_group_id=state.order_group_id, leg="entry", status=order.status,
            filled_quantity=float(order.filled_quantity),
            total_quantity=float(order.total_quantity), order_id=0,
            event_id=(
                f"promotion:{order.order_entity_id}:{order.status}:{order.filled_quantity}"
            ),
            source_timestamp=order.source_timestamp,
        )

    def _reopen_terminal_row(self, state: SagaState, event: BrokerOrderEvent) -> SagaState:
        """The broker shows an order of a NOT_SENT or CLOSED row: reserve it again.

        Any fill is unprotected as far as the saga knows, so it goes to
        SAFETY_FAILED, which trips the breaker and starts liquidation.
        """
        now = self._now_utc()
        logging.critical(
            "broker event %s for saga %s in %s: the order exists after all",
            event.event_id, state.command_id, state.state,
        )
        self._breaker.record(BreakerSignal(
            kind="RECONCILIATION_DIVERGENCE",
            occurred_at=now,
            detail=f"broker order seen for {state.state} saga {state.command_id}",
            key=state.command_id,
        ))
        reopened = self._apply_event(
            replace(state, state="OUTCOME_UNKNOWN", error_code="ORPHAN_ORDER_FOUND"), event,
        )
        if reopened.filled_quantity > 0:
            reopened = replace(reopened, state="SAFETY_FAILED", error_code="ORPHAN_ORDER_FILLED")
        reopened = replace(
            reopened,
            seen_event_ids=state.seen_event_ids + (event.event_id,),
            revision=state.revision + 1,
        )
        self._persist(reopened, now, from_state=state.state, event_id=event.event_id)
        if reopened.state == "SAFETY_FAILED":
            self._trip_and_liquidate(reopened, now)
        return reopened

    # -- event application -------------------------------------------------

    def _apply_event(self, state: SagaState, event: BrokerOrderEvent) -> SagaState:
        status = event.status
        filled = _dec(event.filled_quantity)
        next_state = state

        if event.leg == "entry":
            next_state = _apply_entry_leg(next_state, event)
            if status in _REJECTED_STATUSES:
                # The fill is recorded first: those shares stay reserved.
                return replace(next_state, state="CLOSED", error_code="PARENT_REJECTED")

        elif event.leg == "stop":
            if status in _WORKING_STATUSES:
                next_state = replace(next_state, stop_working=True, stop_rejected=False)
            if status in _REJECTED_STATUSES:
                next_state = replace(next_state, stop_working=False, stop_rejected=True)
            elif status in _CANCELLED_STATUSES:
                # OCA cancel after target fill is expected; bare cancel while
                # unprotected is a rejection of protection.
                if next_state.target_filled:
                    next_state = replace(next_state, stop_working=False)
                else:
                    next_state = replace(next_state, stop_working=False, stop_rejected=True)
            if status in _FILLED_STATUSES:
                next_state = replace(next_state, stop_filled=True, stop_working=False)

        elif event.leg == "take_profit":
            if status in _WORKING_STATUSES:
                next_state = replace(next_state, target_working=True, target_rejected=False)
            if status in _REJECTED_STATUSES:
                next_state = replace(next_state, target_working=False, target_rejected=True)
            elif status in _CANCELLED_STATUSES:
                # OCA cancel after stop fill is expected.
                next_state = replace(next_state, target_working=False)
            if status in _FILLED_STATUSES:
                next_state = replace(next_state, target_filled=True, target_working=False)

        protection_working = self._protection_confirmed(next_state)
        next_state = replace(next_state, protection_working=protection_working)

        if next_state.entry_cancelled and next_state.filled_quantity <= 0:
            return replace(next_state, state="CLOSED", error_code=None)

        fully_filled = next_state.filled_quantity >= next_state.requested_quantity > 0
        partially_filled = (
            next_state.filled_quantity > 0
            and next_state.filled_quantity < next_state.requested_quantity
        )

        # Exit fills take priority — OCA cancels must not look like missing protection.
        if next_state.stop_filled or next_state.target_filled:
            if self._flat(next_state):
                return replace(next_state, state="CLOSED")
            return replace(next_state, state="EXITING")

        # Filled entry without confirmed working protection is a capital-safety incident.
        if next_state.filled_quantity > 0 and not protection_working:
            if fully_filled or next_state.stop_rejected or (
                event.leg == "entry" and status in _FILLED_STATUSES
            ):
                return replace(
                    next_state, state="SAFETY_FAILED", error_code="MISSING_PROTECTION",
                )

        if fully_filled and protection_working:
            return replace(next_state, state="PROTECTED")

        if partially_filled:
            return replace(next_state, state="PARTIALLY_FILLED")

        if next_state.entry_working or next_state.stop_working or next_state.target_working:
            return replace(next_state, state="ENTRY_WORKING")

        return next_state

    @staticmethod
    def _protection_confirmed(state: SagaState) -> bool:
        """Stop must be working (or already filled as exit). Target optional."""
        if state.stop_working or state.stop_filled:
            return True
        return False

    @staticmethod
    def _flat(state: SagaState) -> bool:
        if state.stop_filled and (state.target_filled or not state.target_working):
            # Stop filled; TP cancelled by OCA or never present.
            return True
        if state.target_filled and (state.stop_filled or not state.stop_working):
            return True
        return False

    def _trip_and_liquidate(self, state: SagaState, now: dt.datetime) -> None:
        self._breaker.record(BreakerSignal(
            kind="PROTECTIVE_ORDER_FAILURE",
            occurred_at=now,
            detail=state.error_code or "missing protection after entry fill",
            key=state.command_id,
        ))
        deadline = now + dt.timedelta(seconds=self._liquidation_deadline_seconds)
        self._liquidation.start(state.account_id, state.command_id, deadline)

    # -- persistence -------------------------------------------------------

    def _persist(
        self,
        state: SagaState,
        now: dt.datetime,
        *,
        from_state: Optional[str],
        event_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        check_in_tx: Optional[Callable[[Any], None]] = None,
    ) -> None:
        mutation, write, event_key = self._mutation(
            state, now, from_state=from_state, event_id=event_id,
            expected_revision=expected_revision, check_in_tx=check_in_tx,
        )
        self._journal.mutate(self._journal.connect(), mutation, write, event_id=event_key)

    def _persist_all(self, items: list[tuple[SagaState, Optional[str]]], now: dt.datetime) -> None:
        """Several saga rows in one journal transaction (all or none, revision-checked)."""
        if not items:
            return
        prepared = [self._mutation(state, now, from_state=from_state, event_id=None) for state, from_state in items]
        self._journal.mutate_batch_work(
            self._journal.connect(),
            lambda _conn, append: [append(mutation, write, key) for mutation, write, key in prepared],
        )

    def _mutation(self, state: SagaState, now: dt.datetime, *, from_state: Optional[str],
                  event_id: Optional[str], expected_revision: Optional[int] = None,
                  check_in_tx: Optional[Callable[[Any], None]] = None):
        if state.state not in SAGA_STATES:
            raise ValueError(f"invalid saga state {state.state!r}")

        mutation = DomainMutation(
            event_type="automated_order_saga.updated",
            entity_type="automated_order_saga",
            entity_id=command_entity_id(state.command_id),
            operation="upsert",
            account_id=state.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=state.command_id,
            payload={
                "state": state.state,
                "from_state": from_state,
                "order_group_id": state.order_group_id,
                "order_ref": state.order_ref,
                "error_code": state.error_code,
                "filled_quantity": str(state.filled_quantity),
                "protection_working": state.protection_working,
                "close_root_id": state.close_root_id,
                "revision": state.revision,
            },
        )

        def write(conn, _revision: int) -> None:
            if expected_revision is not None:
                self._store.require_revision_in_tx(conn, state.command_id, expected_revision)
            if check_in_tx is not None:
                check_in_tx(conn)
            self._store.save_in_tx(conn, state, now)
            if event_id is not None:
                self._store.record_event_in_tx(conn, event_id, state.command_id, now)

        event_key = event_id or f"saga:{state.command_id}:{state.state}:{state.revision}"
        return mutation, write, event_key

    def _now_utc(self) -> dt.datetime:
        return _as_utc(self._now())


# ---------------------------------------------------------------------------
# Adapter: BracketPlan → existing TradingRuntimeOrderDispatch / expressive order
# ---------------------------------------------------------------------------

class ProtectiveBracketDispatch:
    """Bridges ProtectiveOrderSaga onto TradingRuntimeOrderDispatch.

    Builds a proposal-shaped object so the existing expressive-order path
    places the bracket — no second IB order path.
    """

    def __init__(self, orders: Any, *, symbol_resolver: Optional[Callable[[int], str]] = None):
        self._orders = orders
        self._symbol_resolver = symbol_resolver or (lambda conid: str(conid))

    def submit_bracket(self, *, plan: BracketPlan, intent: Any, account_id: str) -> Any:
        from types import SimpleNamespace

        spec = execution_spec_from_plan(plan)
        quantity = float(plan.legs[0].quantity)
        proposal = SimpleNamespace(
            conid=intent.conid,
            symbol=self._symbol_resolver(intent.conid),
            sec_type="STK",
            action=intent.side,
            quantity=quantity,
            account_id=account_id,
            account_mode=intent.account_mode,
            reference_price=float(plan.legs[0].limit_price or 0),
            execution=spec,
        )
        return self._orders.submit(
            proposal=proposal,
            order_ref=plan.order_ref,
            order_group_id=plan.order_group_id,
        )
