"""P3 Task 5 — broker-native protective entry saga.

Durable state machine for automated bracket/OCA entries. Broker events
(not submit acknowledgements) advance working/filled states. An entry fill
without confirmed working protection trips the P1 breaker and starts
verified liquidation.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Optional, Protocol

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.trading.circuit_breaker import BreakerSignal
from trader.trading.command_coordinator import BrokerRejectedError
from trader.trading.dispatch_guard import DispatchGuardError
from trader.trading.order_correlation import encode_order_ref

PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION = 30
PROTECTIVE_ORDER_SAGA_MIGRATION_NAME = "p3_automated_order_sagas"

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
})

_WORKING_STATUSES = frozenset({
    "PendingSubmit", "PreSubmitted", "Submitted", "ApiPending", "PendingCancel",
})
_FILLED_STATUSES = frozenset({"Filled"})
_CANCELLED_STATUSES = frozenset({"Cancelled", "ApiCancelled"})
_REJECTED_STATUSES = frozenset({"Inactive", "Rejected"})
_TERMINAL_SAGA = frozenset({"CLOSED", "SAFETY_FAILED"})

_PRICE_QUANT = Decimal("0.01")


def apply_protective_order_saga_migration(migrator: SchemaMigrator) -> bool:
    """Journal migration 30: durable automated order saga rows."""
    return migrator.apply(
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
        )


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

    def load_by_group(self, order_group_id: str) -> Optional[SagaState]:
        row = self._db.execute(
            "SELECT payload FROM automated_order_sagas WHERE order_group_id = ?",
            [order_group_id], fetch="one",
        )
        if row is None:
            return None
        return SagaState.from_payload(json.loads(row[0]))

    def save_in_tx(self, conn, state: SagaState, now: dt.datetime) -> None:
        payload = json.dumps(state.to_payload(), sort_keys=True, default=str)
        conn.execute("DELETE FROM automated_order_sagas WHERE command_id = ?", [state.command_id])
        conn.execute(
            "INSERT INTO automated_order_sagas "
            "(command_id, order_group_id, state, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
            [state.command_id, state.order_group_id, state.state, payload, now],
        )

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
        decision = self._session_risk.evaluate(
            intent, artifact, approval, session_state, allocation,
        )
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

        # 2) Re-run P1 DispatchGuard immediately before first IB side effect.
        try:
            self._dispatch_guard.revalidate(approval, request, now)
        except DispatchGuardError as ex:
            closed = replace(
                validated, state="CLOSED", error_code=ex.code, revision=validated.revision + 1,
            )
            self._persist(closed, now, from_state="VALIDATED")
            return closed

        submitting = replace(validated, state="SUBMITTING", revision=validated.revision + 1)
        self._persist(submitting, now, from_state="VALIDATED")

        # 3) Irreversible boundary — submit via existing bracket path.
        try:
            submitted = self._dispatch.submit_bracket(
                plan=plan, intent=intent, account_id=self._account_id,
            )
        except BrokerRejectedError:
            closed = replace(
                submitting, state="CLOSED", error_code="BROKER_REJECTED",
                revision=submitting.revision + 1,
            )
            self._persist(closed, now, from_state="SUBMITTING")
            return closed
        except Exception:
            unknown = replace(
                submitting, state="OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
                revision=submitting.revision + 1,
            )
            self._persist(unknown, now, from_state="SUBMITTING")
            return unknown

        order_ids = tuple(int(x) for x in (getattr(submitted, "order_ids", None) or ()))
        # Submit returns correlation ids only — broker events advance working.
        recorded = replace(
            submitting,
            submitted_order_ids=order_ids,
            revision=submitting.revision + 1,
        )
        self._persist(recorded, now, from_state="SUBMITTING")
        return recorded

    def resume(self, command_id: str) -> Optional[SagaState]:
        return self._store.load(command_id)

    def on_broker_event(self, event: BrokerOrderEvent) -> SagaState:
        state = self._store.load_by_group(event.order_group_id)
        if state is None:
            raise KeyError(f"no saga for order_group_id={event.order_group_id!r}")
        if event.event_id in state.seen_event_ids:
            return state
        if state.state in _TERMINAL_SAGA:
            return state

        now = self._now_utc()
        updated = self._apply_event(state, event)
        if updated is state:
            # Still record the event id for idempotency even if no transition.
            updated = replace(
                state,
                seen_event_ids=state.seen_event_ids + (event.event_id,),
                revision=state.revision + 1,
            )
            self._persist(updated, now, from_state=state.state, event_id=event.event_id)
            return updated

        updated = replace(
            updated,
            seen_event_ids=state.seen_event_ids + (event.event_id,),
            revision=state.revision + 1,
        )
        self._persist(updated, now, from_state=state.state, event_id=event.event_id)

        if updated.state == "SAFETY_FAILED" and state.state != "SAFETY_FAILED":
            self._trip_and_liquidate(updated, now)
        return updated

    # -- event application -------------------------------------------------

    def _apply_event(self, state: SagaState, event: BrokerOrderEvent) -> SagaState:
        status = event.status
        filled = _dec(event.filled_quantity)
        next_state = state

        if event.leg == "entry":
            if status in _WORKING_STATUSES:
                next_state = replace(next_state, entry_working=True)
            if status in _CANCELLED_STATUSES:
                next_state = replace(next_state, entry_cancelled=True, entry_working=False)
            if status in _REJECTED_STATUSES:
                return replace(
                    next_state, state="CLOSED", error_code="PARENT_REJECTED",
                    entry_working=False,
                )
            if filled > 0:
                next_state = replace(
                    next_state,
                    filled_quantity=filled,
                    protection_quantity=filled,
                    protection_adjusted=filled < next_state.requested_quantity,
                )
            if status in _FILLED_STATUSES:
                qty = max(filled, next_state.filled_quantity, next_state.requested_quantity)
                next_state = replace(
                    next_state,
                    filled_quantity=qty,
                    protection_quantity=qty,
                    entry_working=False,
                )

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
    ) -> None:
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
                "revision": state.revision,
            },
        )

        def write(conn, _revision: int) -> None:
            self._store.save_in_tx(conn, state, now)
            if event_id is not None:
                self._store.record_event_in_tx(conn, event_id, state.command_id, now)

        event_key = event_id or f"saga:{state.command_id}:{state.state}:{state.revision}"
        self._journal.mutate(
            self._journal.connect(),
            mutation,
            write,
            event_id=event_key,
        )

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
