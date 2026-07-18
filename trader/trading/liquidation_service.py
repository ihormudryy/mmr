"""Broker-verified emergency liquidation state machine.

This module deliberately regards order acknowledgements as *evidence of
uncertainty*, never as evidence that an account is flat.  It is intentionally
small and adapter-driven so the same state machine is usable by the session
controller and an authenticated emergency command without creating another
broker order path.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol


_NON_FLAT = {"REQUESTED", "CANCELLING_ENTRIES", "REDUCING", "VERIFYING", "OUTCOME_UNKNOWN", "FAILED_SAFE"}


class BrokerSnapshotPort(Protocol):
    def capture(self, account_id: str) -> Any: ...


class LiquidationDispatchPort(Protocol):
    """Narrow, reduce-only order boundary.

    Implementations must reject an order that can increase or flip the broker
    position.  ``command_id`` is a deterministic child id for audit/correlation.
    """
    def cancel(self, order: Any, command_id: str) -> None: ...
    def reduce(self, position: Any, side: str, quantity: float, command_id: str) -> None: ...


class LiquidationBreakerPort(Protocol):
    def trip_liquidation(self, cause_command_id: str, detail: str) -> None: ...


@dataclass(frozen=True)
class LiquidationReceipt:
    account_id: str
    cause_command_id: str
    state: str
    deadline: dt.datetime
    generation_id: Optional[int] = None
    detail: str = ""


class LiquidationService:
    """Conservative single-process saga; persistent orchestration is added by its owner.

    A root is idempotent while it waits for broker truth: it never emits another
    reduce order until a later broker generation is observed.  This eliminates
    the dangerous retry-on-timeout/partial-fill pattern.  A caller may invoke
    :meth:`rescan` whenever a promoted broker generation arrives.
    """

    def __init__(
        self,
        broker: BrokerSnapshotPort,
        dispatch: LiquidationDispatchPort,
        *,
        breaker: Optional[LiquidationBreakerPort] = None,
        now: Callable[[], dt.datetime],
    ):
        self._broker = broker
        self._dispatch = dispatch
        self._breaker = breaker
        self._now = now
        self._runs: dict[str, LiquidationReceipt] = {}

    @staticmethod
    def child_command_id(cause_command_id: str, phase: str, key: str) -> str:
        # command ids may not contain ':' because they become order references.
        return f"{cause_command_id}-liquidation-{phase}-{key}"

    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime) -> LiquidationReceipt:
        if not account_id or not cause_command_id:
            raise ValueError("account_id and cause_command_id are required")
        current = self._runs.get(cause_command_id)
        if current is not None:
            if current.account_id != account_id:
                raise ValueError("cause command id is already bound to another account")
            return self._advance(current)
        receipt = LiquidationReceipt(account_id, cause_command_id, "REQUESTED", deadline)
        self._runs[cause_command_id] = receipt
        return self._advance(receipt)

    def rescan(self) -> Optional[LiquidationReceipt]:
        """Advance one unresolved root from newly promoted broker evidence."""
        for receipt in tuple(self._runs.values()):
            if receipt.state in _NON_FLAT:
                return self._advance(receipt)
        return None

    def _set(self, receipt: LiquidationReceipt, state: str, *, generation_id=None, detail="") -> LiquidationReceipt:
        updated = LiquidationReceipt(
            receipt.account_id, receipt.cause_command_id, state, receipt.deadline,
            receipt.generation_id if generation_id is None else generation_id, detail,
        )
        self._runs[receipt.cause_command_id] = updated
        if state != "FLAT" and self._breaker is not None:
            self._breaker.trip_liquidation(receipt.cause_command_id, detail or state)
        return updated

    def _advance(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        if receipt.state in {"FLAT", "FAILED_SAFE"}:
            return receipt
        if self._now() >= receipt.deadline:
            return self._set(receipt, "FAILED_SAFE", detail="liquidation deadline elapsed without broker-confirmed flat state")
        try:
            snapshot = self._broker.capture(receipt.account_id)
        except Exception as exc:
            return self._set(receipt, "OUTCOME_UNKNOWN", detail=f"broker snapshot unavailable: {exc}")
        if getattr(snapshot, "account_id", None) != receipt.account_id:
            return self._set(receipt, "OUTCOME_UNKNOWN", detail="broker snapshot account mismatch")

        generation = int(snapshot.generation_id)
        # A zero snapshot is valid only after the generation that observed or
        # submitted the action.  This rejects stale cached 'flat' snapshots.
        if (not snapshot.positions and not snapshot.working_orders and receipt.generation_id is not None
                and generation > receipt.generation_id):
            return self._set(receipt, "FLAT", generation_id=generation, detail="fresh broker snapshot confirms no positions or working orders")

        # Never retry a submitted reduction absent new broker truth.  The
        # previous order may be live even if its acknowledgement was lost.
        if receipt.state in {"REDUCING", "VERIFYING", "OUTCOME_UNKNOWN"} and receipt.generation_id is not None and generation <= receipt.generation_id:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting newer broker generation")

        working = tuple(snapshot.working_orders)
        if working:
            current = self._set(receipt, "CANCELLING_ENTRIES", generation_id=generation, detail="cancelling broker-reported working orders")
            try:
                for order in working:
                    child = self.child_command_id(receipt.cause_command_id, "cancel", str(order.order_entity_id))
                    self._dispatch.cancel(order, child)
            except Exception as exc:
                return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"cancel outcome unknown: {exc}")
            # Do not reduce from the same snapshot: cancellation is only an
            # acknowledgement.  A subsequent promoted generation must prove
            # entries are absent before a reduction is sent.
            return self._set(current, "VERIFYING", generation_id=generation, detail="awaiting broker confirmation that working orders are gone")

        positions = tuple(position for position in snapshot.positions if float(position.quantity) != 0.0)
        if not positions:
            # Initial already-flat must still get a fresh snapshot after the
            # root is created; record this generation and wait one promotion.
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting fresh broker flat confirmation")
        current = self._set(receipt, "REDUCING", generation_id=generation, detail="submitting reduce-only liquidation orders")
        try:
            for position in positions:
                quantity = abs(float(position.quantity))
                side = "SELL" if position.quantity > 0 else "BUY"
                child = self.child_command_id(receipt.cause_command_id, "reduce", str(position.conid))
                self._dispatch.reduce(position, side, quantity, child)
        except Exception as exc:
            return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"reduction outcome unknown: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation, detail="reduction submitted; awaiting broker-confirmed flat state")
