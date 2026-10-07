"""Durable model budget (spec 5.4). Money is integer micro-USD.

Rules, all persisted in ai.duckdb:
- The daily window is the America/New_York calendar date. It is computed in Python from the
  injected clock and stored as text. Never read a date back from a DuckDB timestamp.
- A reservation belongs to the window it was made in, for its whole life. A call that crosses
  midnight settles in its own window. Nothing is reset twice or erased.
- Lowering the cap applies at once. Raising it applies at the first 00:00 New York after the
  request, and a restart never advances or delays that date.
- Worst-case reservation, the window total, the cap check and the hourly check run in one
  transaction, so concurrent roles cannot overspend.
- UNKNOWN keeps its reserved amount. A later usage report settles it once.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from trader.ai.clock import Clock
from trader.ai.store import AiStore, to_utc

NEW_YORK = ZoneInfo("America/New_York")

OPEN = "OPEN"
UNKNOWN_STATE = "UNKNOWN"
SETTLED = "SETTLED"
RELEASED = "RELEASED"

RELEASE_NOT_SENT = "NOT_SENT"
RELEASE_REJECTED = "REJECTED"


def window_date(instant: datetime) -> str:
    return instant.astimezone(NEW_YORK).date().isoformat()


def next_window_date(instant: datetime) -> str:
    return (instant.astimezone(NEW_YORK).date() + timedelta(days=1)).isoformat()


def next_window_start(instant: datetime) -> datetime:
    midnight = datetime.combine(instant.astimezone(NEW_YORK).date() + timedelta(days=1), time(0), tzinfo=NEW_YORK)
    return midnight.astimezone(timezone.utc)


class BudgetRefusal(Exception):
    code = ""


class BudgetNotInitialized(BudgetRefusal):
    code = "BUDGET_NOT_INITIALIZED"


class BudgetExhausted(BudgetRefusal):
    code = "BUDGET_EXHAUSTED"

    def __init__(self, *, window: str, cap_micros: int, committed_micros: int, needed_micros: int,
                 retry_at: datetime):
        super().__init__(f"window {window}: {committed_micros} + {needed_micros} > cap {cap_micros} micro-USD")
        self.window, self.cap_micros = window, cap_micros
        self.committed_micros, self.needed_micros, self.retry_at = committed_micros, needed_micros, retry_at


class HourlyLimitReached(BudgetRefusal):
    """A delay, not a block: the call may be retried at `retry_at`."""

    code = "HOURLY_LIMIT"

    def __init__(self, *, retry_at: datetime, retry_after_seconds: float):
        super().__init__(f"calls_per_hour reached; retry in {retry_after_seconds:.0f}s")
        self.retry_at, self.retry_after_seconds = retry_at, retry_after_seconds


class BudgetConflict(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    window_date: str
    reserved_micros: int
    state: str = OPEN
    actual_micros: Optional[int] = None


@dataclass(frozen=True)
class BudgetSnapshot:
    window_date: str
    effective_cap_micros: int
    pending_cap_micros: Optional[int]
    pending_window_date: Optional[str]
    committed_micros: int
    open_reservations: int
    unknown_reservations: int
    next_reset_at: datetime


class Budget:
    def __init__(self, store: AiStore, clock: Clock, *, calls_per_hour: int):
        if type(calls_per_hour) is not int or calls_per_hour < 1:
            raise ValueError("calls_per_hour must be a positive integer")
        self.store = store
        self.clock = clock
        self.calls_per_hour = calls_per_hour

    # --- cap -------------------------------------------------------------

    def _state(self, conn: Any) -> Optional[tuple]:
        return conn.execute("SELECT effective_cap_micros, pending_cap_micros, pending_window_date "
                            "FROM ai_budget_state WHERE id = 1").fetchone()

    def _event(self, conn: Any, kind: str, requested: int, now: datetime) -> None:
        effective, pending, pending_window = self._state(conn)
        conn.execute("INSERT INTO ai_budget_cap_events VALUES (?, ?, ?, ?, ?, ?)",
                     [now, kind, requested, effective, pending, pending_window])

    def _roll_in_tx(self, conn: Any, now: datetime) -> None:
        state = self._state(conn)
        if state is None or state[1] is None:
            return
        if window_date(now) >= state[2]:
            conn.execute("UPDATE ai_budget_state SET effective_cap_micros = ?, pending_cap_micros = NULL, "
                         "pending_window_date = NULL, updated_at = ? WHERE id = 1", [state[1], now])
            self._event(conn, "RAISE_APPLIED", state[1], now)

    def set_cap_in_tx(self, conn: Any, requested_micros: int, now: datetime) -> str:
        """Owner setting from config. Call at every start and whenever the owner changes it."""
        if type(requested_micros) is not int or requested_micros < 0:
            raise ValueError("the cap must be a non-negative integer of micro-USD")
        state = self._state(conn)
        if state is None:
            conn.execute("INSERT INTO ai_budget_state VALUES (1, ?, NULL, NULL, ?)", [requested_micros, now])
            self._event(conn, "INITIALIZED", requested_micros, now)
            return "INITIALIZED"
        self._roll_in_tx(conn, now)
        effective, pending, pending_window = self._state(conn)
        if requested_micros < effective:
            conn.execute("UPDATE ai_budget_state SET effective_cap_micros = ?, pending_cap_micros = NULL, "
                         "pending_window_date = NULL, updated_at = ? WHERE id = 1", [requested_micros, now])
            decision = "LOWERED"
        elif requested_micros == effective:
            conn.execute("UPDATE ai_budget_state SET pending_cap_micros = NULL, pending_window_date = NULL, "
                         "updated_at = ? WHERE id = 1", [now])
            decision = "UNCHANGED"
        elif pending == requested_micros:
            return "RAISE_KEPT"
        else:
            conn.execute("UPDATE ai_budget_state SET pending_cap_micros = ?, pending_window_date = ?, "
                         "updated_at = ? WHERE id = 1", [requested_micros, next_window_date(now), now])
            decision = "RAISE_SCHEDULED"
        self._event(conn, decision, requested_micros, now)
        return decision

    async def set_cap(self, requested_micros: int) -> str:
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.set_cap_in_tx(conn, requested_micros, now))

    # --- reservations ----------------------------------------------------

    def _committed(self, conn: Any, window: str) -> int:
        return conn.execute(
            "SELECT COALESCE(SUM(CASE state WHEN 'SETTLED' THEN actual_micros WHEN 'RELEASED' THEN 0 "
            "ELSE reserved_micros END), 0) FROM ai_budget_reservations WHERE window_date = ?", [window]).fetchone()[0]

    def _check_hourly(self, conn: Any, now: datetime) -> None:
        rows = conn.execute(
            "SELECT created_at FROM ai_budget_reservations WHERE created_at > ? "
            "AND NOT (state = 'RELEASED' AND release_reason = 'NOT_SENT') ORDER BY created_at",
            [now - timedelta(hours=1)]).fetchall()
        if len(rows) >= self.calls_per_hour:
            leaves = to_utc(rows[len(rows) - self.calls_per_hour][0]) + timedelta(hours=1)
            raise HourlyLimitReached(retry_at=leaves, retry_after_seconds=max(0.0, (leaves - now).total_seconds()))

    def reserve_in_tx(self, conn: Any, *, reservation_id: str, role: str, backend: str, model: str,
                      worst_case_micros: int, now: datetime) -> Reservation:
        if type(worst_case_micros) is not int or worst_case_micros < 0:
            raise ValueError("worst_case_micros must be a non-negative integer")
        if self._state(conn) is None:
            raise BudgetNotInitialized("call set_cap at startup")
        self._roll_in_tx(conn, now)
        window = window_date(now)
        cap = self._state(conn)[0]
        committed = self._committed(conn, window)
        if committed + worst_case_micros > cap:
            raise BudgetExhausted(window=window, cap_micros=cap, committed_micros=committed,
                                  needed_micros=worst_case_micros, retry_at=next_window_start(now))
        self._check_hourly(conn, now)
        conn.execute(
            "INSERT INTO ai_budget_reservations (reservation_id, window_date, role, backend, model, state, "
            "reserved_micros, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [reservation_id, window, role, backend, model, OPEN, worst_case_micros, now])
        return Reservation(reservation_id, window, worst_case_micros)

    def get_in_tx(self, conn: Any, reservation_id: str) -> Reservation:
        row = conn.execute("SELECT reservation_id, window_date, reserved_micros, state, actual_micros "
                           "FROM ai_budget_reservations WHERE reservation_id = ?", [reservation_id]).fetchone()
        if row is None:
            raise BudgetConflict("RESERVATION_UNKNOWN", reservation_id)
        return Reservation(*row)

    def release_in_tx(self, conn: Any, reservation_id: str, *, reason: str, now: datetime) -> None:
        """Only for a call that provably produced no tokens: not sent, or rejected by the provider."""
        row = self.get_in_tx(conn, reservation_id)
        if row.state != OPEN:
            raise BudgetConflict("RESERVATION_STATE", f"{reservation_id} is {row.state}")
        conn.execute("UPDATE ai_budget_reservations SET state = ?, release_reason = ?, closed_at = ? "
                     "WHERE reservation_id = ?", [RELEASED, reason, now, reservation_id])

    def mark_unknown_in_tx(self, conn: Any, reservation_id: str, now: datetime) -> None:
        row = self.get_in_tx(conn, reservation_id)
        if row.state != OPEN:
            raise BudgetConflict("RESERVATION_STATE", f"{reservation_id} is {row.state}")
        conn.execute("UPDATE ai_budget_reservations SET state = ?, closed_at = ? WHERE reservation_id = ?",
                     [UNKNOWN_STATE, now, reservation_id])

    def settle_in_tx(self, conn: Any, reservation_id: str, *, actual_micros: int, input_tokens: int,
                     output_tokens: int, now: datetime, late: bool = False) -> bool:
        """Settle once. `late=True` is a usage report for an UNKNOWN reservation.
        Returns False for an exact repeat; a different figure is a conflict."""
        row = self.get_in_tx(conn, reservation_id)
        if row.state == SETTLED:
            if row.actual_micros == actual_micros:
                return False
            raise BudgetConflict("SETTLEMENT_CONFLICT", reservation_id)
        if row.state != (UNKNOWN_STATE if late else OPEN):
            raise BudgetConflict("RESERVATION_STATE", f"{reservation_id} is {row.state}")
        conn.execute("UPDATE ai_budget_reservations SET state = ?, actual_micros = ?, input_tokens = ?, "
                     "output_tokens = ?, closed_at = ? WHERE reservation_id = ?",
                     [SETTLED, actual_micros, input_tokens, output_tokens, now, reservation_id])
        return True

    def recover_open_in_tx(self, conn: Any, now: datetime) -> list[str]:
        """Process start only: nothing is in flight, so every OPEN reservation is UNKNOWN."""
        ids = [r[0] for r in conn.execute("SELECT reservation_id FROM ai_budget_reservations WHERE state = ?",
                                          [OPEN]).fetchall()]
        conn.execute("UPDATE ai_budget_reservations SET state = ?, closed_at = ? WHERE state = ?",
                     [UNKNOWN_STATE, now, OPEN])
        return ids

    def snapshot_in_tx(self, conn: Any, now: datetime) -> BudgetSnapshot:
        if self._state(conn) is None:
            raise BudgetNotInitialized("call set_cap at startup")
        self._roll_in_tx(conn, now)
        effective, pending, pending_window = self._state(conn)
        window = window_date(now)
        counts = dict(conn.execute("SELECT state, count(*) FROM ai_budget_reservations WHERE window_date = ? "
                                   "GROUP BY state", [window]).fetchall())
        return BudgetSnapshot(window, effective, pending, pending_window, self._committed(conn, window),
                              counts.get(OPEN, 0), counts.get(UNKNOWN_STATE, 0), next_window_start(now))

    async def snapshot(self) -> BudgetSnapshot:
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.snapshot_in_tx(conn, now))
