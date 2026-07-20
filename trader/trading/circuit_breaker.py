"""Deterministic durable automation circuit-breaker policy."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from trader.data.circuit_breaker_store import BreakerState, CircuitBreakerStore


@dataclass(frozen=True)
class BreakerSignal:
    kind: str
    occurred_at: dt.datetime
    detail: str = ""
    key: str = ""


class BreakerResetRefused(RuntimeError):
    pass


_IMMEDIATE = {
    "DAILY_LOSS_BREACH",
    "PROTECTIVE_ORDER_FAILURE",
    "RECONCILIATION_DIVERGENCE",
    "LIQUIDATION_FAILED",
    # P3 session-risk immediate pauses (foundation design §6.2 / §6.5)
    "ACCOUNT_MISMATCH",
    "INSTRUMENT_HALT",
    "DRAWDOWN_BREACH",
    # P3 Task 6 — missed session flat confirmation
    "MISSED_FLAT_DEADLINE",
}
_ROLLING = {
    "QUOTE_FAILURE": (3, dt.timedelta(minutes=5)),
    "AMBIGUOUS_OUTCOME": (3, dt.timedelta(minutes=5)),
    "RUNTIME_EXCEPTION": (5, dt.timedelta(minutes=10)),
}
_KNOWN = _IMMEDIATE | set(_ROLLING) | {"BROKER_DISCONNECTED", "BROKER_RECONNECTED"}


class CircuitBreaker:
    def __init__(
        self,
        store: CircuitBreakerStore,
        *,
        now: Callable[[], dt.datetime],
        reset_ready: Callable[[], bool],
        reconciliation_complete: Callable[[], bool],
        session_key: Callable[[dt.datetime], str],
        disconnect_grace: dt.timedelta = dt.timedelta(seconds=30),
    ):
        self.store = store
        self._now = now
        self._reset_ready = reset_ready
        self._reconciliation_complete = reconciliation_complete
        self._session_key = session_key
        self._disconnect_grace = disconnect_grace

    def record(self, signal: BreakerSignal) -> BreakerState:
        if signal.kind not in _KNOWN:
            raise ValueError(f"unknown breaker signal {signal.kind!r}")
        now = self._now()

        def trip_reason(conn):
            if signal.kind in _IMMEDIATE:
                return signal.kind, signal.detail or signal.kind.replace("_", " ").lower()
            rolling = _ROLLING.get(signal.kind)
            if rolling:
                threshold, window = rolling
                if self.store.count_since(conn, signal.kind, now - window) >= threshold:
                    return signal.kind, f"{threshold} {signal.kind.lower()} signals within {window}"
            if signal.kind == "BROKER_DISCONNECTED":
                started = self.store.disconnect_started(conn)
                if started is not None and now - started >= self._disconnect_grace:
                    return "BROKER_DISCONNECTED", "broker disconnect exceeded grace period"
            return None

        return self.store.apply_signal(
            kind=signal.kind,
            detail=signal.detail,
            occurred_at=signal.occurred_at,
            key=signal.key,
            recorded_at=now,
            trip_reason=trip_reason,
        )

    def reset(self, command_id: str, reason: str, operator_authority: str) -> BreakerState:
        if not command_id or not reason.strip() or not operator_authority.strip():
            raise BreakerResetRefused("command, reason, and operator authority are required")
        current = self.store.get()
        if current.state == "CLEAR":
            return current
        now = self._now()
        if (current.reason_code == "DAILY_LOSS_BREACH" and current.tripped_at is not None
                and self._session_key(current.tripped_at) == self._session_key(now)):
            raise BreakerResetRefused("daily-loss breaker may reset only in the next session")
        if not self._reset_ready():
            raise BreakerResetRefused("semantic readiness preconditions are not satisfied")
        if not self._reconciliation_complete():
            raise BreakerResetRefused("reconciliation is incomplete")
        return self.store.reset(command_id, reason.strip(), now)
