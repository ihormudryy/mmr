"""The one model call path: price -> slot -> reserve + journal begin -> adapter -> journal finish + settle.

Order matters. The price is looked up before anything is written, so a missing price leaves
no trace. Reservation and journal begin commit together, so there is no reservation without
an attempt row. Journal finish, settlement and the cost event commit together.
Callers (Plans 5 and 6) depend on the `ModelCaller` protocol, so replay can swap the gateway.
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol

from trader.ai.budget import Budget, BudgetExhausted, HourlyLimitReached, RELEASE_NOT_SENT, RELEASE_REJECTED
from trader.ai.clock import Clock
from trader.ai.config import ROLE_NAMES, AiConfig, AiConfigError, ModelPrice, RoleConfig, usd_to_micros_floor
from trader.ai.journal import (
    COST_CONFIRMED, COST_CORRECTION, COST_ESTIMATED_UNKNOWN, COST_NONE, AttemptJournal, AttemptRecord,
)
from trader.ai.model_client import (
    NOT_SENT, REJECTED, ModelCallError, ModelClient, ModelRequest, ModelResponse, NotSentError,
    OutcomeUnknownError, Usage, build_model_client, estimate_input_tokens,
)
from trader.ai.store import AiStore

TIMEOUT_GRACE_SECONDS = 0.25


class CallRefused(Exception):
    """Refused before anything was reserved or sent. Safe to try again later."""

    def __init__(self, code: str, detail: str = "", *, retry_at: Optional[datetime] = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.retry_at = code, detail, retry_at


class CallFailed(Exception):
    """The call was reserved and journaled, and did not produce a usable answer.
    `outcome` is NOT_SENT, REJECTED, UNKNOWN or COST_UNKNOWN. Nothing may be submitted
    to the trader on the strength of a failed call."""

    def __init__(self, code: str, *, outcome: str, attempt_key: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.outcome, self.attempt_key, self.detail = code, outcome, attempt_key, detail

    @property
    def outcome_unknown(self) -> bool:
        return self.outcome not in (NOT_SENT, REJECTED)


class DecisionDeadline:
    """One deadline for a whole decision. Pass the same object to the orchestrator and Jev calls."""

    def __init__(self, clock: Clock, seconds: float, label: str = ""):
        self._clock = clock
        self._ends_at = clock.monotonic() + seconds
        self.seconds = seconds
        self.label = label

    def remaining(self) -> float:
        return max(0.0, self._ends_at - self._clock.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0


@dataclass(frozen=True)
class GatewayResult:
    response: ModelResponse
    attempt_key: str
    cost_micros: int
    reserved_micros: int
    overrun: bool = False
    replayed: bool = False


class ModelCaller(Protocol):
    def new_deadline(self, label: str = "") -> DecisionDeadline: ...

    async def call(self, role: str, request: ModelRequest, deadline: DecisionDeadline) -> GatewayResult: ...


@dataclass(frozen=True)
class _Begun:
    reservation_id: str
    reserved_micros: int
    attempt: AttemptRecord


class ModelGateway:
    def __init__(self, *, config: AiConfig, store: AiStore, clock: Clock, clients: Mapping[str, ModelClient],
                 budget: Optional[Budget] = None, journal: Optional[AttemptJournal] = None):
        for name in ROLE_NAMES:
            role, client = config.role(name), clients.get(name)
            if client is None or client.backend != role.backend or client.model_id != role.model:
                raise AiConfigError("CLIENT_ROLE_MISMATCH", f"the client for role {name!r} does not match ai.yaml")
        self.config, self.store, self.clock = config, store, clock
        self._clients = dict(clients)
        self.journal = journal or AttemptJournal(store)
        self.budget = budget or Budget(store, clock, calls_per_hour=config.budget.calls_per_hour)
        self._slots = asyncio.Semaphore(config.budget.max_in_flight)

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Migrate, apply the configured cap, then turn leftovers of a dead process into UNKNOWN."""
        await asyncio.to_thread(self.store.migrate)
        await self.budget.set_cap(usd_to_micros_floor(self.config.budget.model_budget_usd_per_day))
        await self.recover_after_restart()

    async def recover_after_restart(self) -> None:
        now = self.clock.now()

        def work(conn: Any) -> None:
            for attempt in self.journal.mark_started_as_unknown_in_tx(conn, now):
                reserved = self.budget.get_in_tx(conn, attempt.reservation_id).reserved_micros
                self.journal.add_cost_event_in_tx(conn, attempt=attempt, kind=COST_ESTIMATED_UNKNOWN,
                                                  cost_micros=reserved, usage=None, now=now)
            self.budget.recover_open_in_tx(conn, now)

        await self.store.atransaction(work)

    def new_deadline(self, label: str = "") -> DecisionDeadline:
        return DecisionDeadline(self.clock, self.config.budget.decision_deadline_seconds, label)

    # --- the call --------------------------------------------------------

    async def call(self, role: str, request: ModelRequest, deadline: DecisionDeadline) -> GatewayResult:
        role_config, client, price = self._prepare(role, request)
        worst_case = price.cost_micros(role_config.max_input_tokens, role_config.max_output_tokens)
        begun = await self._acquire_slot_and_begin(role, role_config, request, worst_case, deadline)
        try:
            return await self._send_and_settle(client, role_config, price, request, begun, deadline)
        finally:
            self._slots.release()

    def _prepare(self, role: str, request: ModelRequest) -> tuple[RoleConfig, ModelClient, ModelPrice]:
        if role not in ROLE_NAMES:
            raise CallRefused("ROLE_UNKNOWN", role)
        role_config = self.config.role(role)
        if request.max_output_tokens > role_config.max_output_tokens:
            raise CallRefused("OUTPUT_LIMIT_ABOVE_ROLE", f"{request.max_output_tokens} > {role_config.max_output_tokens}")
        if estimate_input_tokens(request.messages) > role_config.max_input_tokens:
            raise CallRefused("INPUT_TOO_LARGE", f"limit {role_config.max_input_tokens} tokens")
        price = self.config.prices.price_for(role_config.backend, role_config.model)
        if price is None:
            raise CallRefused("PRICE_UNAVAILABLE", f"{role_config.backend}/{role_config.model} has no price")
        return role_config, self._clients[role], price

    def _require_time(self, deadline: DecisionDeadline) -> None:
        if deadline.expired:
            raise CallRefused("DEADLINE_EXPIRED", deadline.label)

    async def _acquire_slot_and_begin(self, role: str, role_config: RoleConfig, request: ModelRequest,
                                      worst_case: int, deadline: DecisionDeadline) -> _Begun:
        """Returns holding one in-flight slot. The caller releases it."""
        while True:
            self._require_time(deadline)
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=deadline.remaining())
            except TimeoutError:
                raise CallRefused("IN_FLIGHT_LIMIT_DEADLINE", f"at most {self.config.budget.max_in_flight} calls at once") from None
            try:
                return await self._reserve_and_begin(role, role_config, request, worst_case)
            except HourlyLimitReached as limit:
                self._slots.release()
                if limit.retry_after_seconds > deadline.remaining():
                    raise CallRefused("HOURLY_LIMIT_EXCEEDS_DEADLINE", retry_at=limit.retry_at) from None
                await self.clock.sleep(limit.retry_after_seconds + 0.001)
            except BudgetExhausted as exhausted:
                self._slots.release()
                raise CallRefused("BUDGET_EXHAUSTED", str(exhausted), retry_at=exhausted.retry_at) from None
            except BaseException:
                self._slots.release()
                raise

    async def _reserve_and_begin(self, role: str, role_config: RoleConfig, request: ModelRequest,
                                 worst_case: int) -> _Begun:
        now = self.clock.now()
        reservation_id = uuid.uuid4().hex

        def work(conn: Any) -> _Begun:
            reservation = self.budget.reserve_in_tx(
                conn, reservation_id=reservation_id, role=role, backend=role_config.backend,
                model=role_config.model, worst_case_micros=worst_case, now=now)
            attempt = self.journal.begin_in_tx(
                conn, request=request, role=role, backend=role_config.backend, model=role_config.model,
                reservation_id=reservation_id, now=now)
            return _Begun(reservation_id, reservation.reserved_micros, attempt)

        return await self.store.atransaction(work)

    async def _send_and_settle(self, client: ModelClient, role_config: RoleConfig, price: ModelPrice,
                               request: ModelRequest, begun: _Begun, deadline: DecisionDeadline) -> GatewayResult:
        timeout = min(role_config.call_timeout_seconds, deadline.remaining())
        if timeout <= 0:
            return await self._fail(begun, NotSentError("DEADLINE_EXPIRED", "no time left to send"))
        keyed = dataclasses.replace(request, attempt_key=begun.attempt.attempt_key)
        try:
            response = await asyncio.wait_for(client.complete(keyed, timeout_seconds=timeout),
                                              timeout + TIMEOUT_GRACE_SECONDS)
        except asyncio.CancelledError:
            await asyncio.shield(self._record_failure(begun, OutcomeUnknownError("CALLER_CANCELLED")))
            raise
        except TimeoutError:
            return await self._fail(begun, OutcomeUnknownError("CALL_TIMEOUT"))
        except ModelCallError as error:
            return await self._fail(begun, error)
        except Exception as error:
            return await self._fail(begun, OutcomeUnknownError("ADAPTER_FAILURE", type(error).__name__))
        return await self._record_success(price, begun, response)

    async def _record_success(self, price: ModelPrice, begun: _Begun, response: ModelResponse) -> GatewayResult:
        now = self.clock.now()
        cost = price.cost_micros(response.usage.input_tokens, response.usage.output_tokens)

        def work(conn: Any) -> None:
            self.journal.finish_success_in_tx(conn, begun.attempt.attempt_key, response, now)
            self.budget.settle_in_tx(conn, begun.reservation_id, actual_micros=cost,
                                     input_tokens=response.usage.input_tokens,
                                     output_tokens=response.usage.output_tokens, now=now)
            self.journal.add_cost_event_in_tx(conn, attempt=begun.attempt, kind=COST_CONFIRMED,
                                              cost_micros=cost, usage=response.usage, now=now)

        await self.store.atransaction(work)
        return GatewayResult(response, begun.attempt.attempt_key, cost, begun.reserved_micros,
                             overrun=cost > begun.reserved_micros)

    async def _record_failure(self, begun: _Begun, error: ModelCallError) -> None:
        now = self.clock.now()
        provably_none = error.outcome in (NOT_SENT, REJECTED)

        def work(conn: Any) -> None:
            self.journal.finish_failure_in_tx(conn, begun.attempt.attempt_key, outcome=error.outcome,
                                              error_code=error.code, error_detail=error.detail, now=now)
            if provably_none:
                self.budget.release_in_tx(conn, begun.reservation_id, reason=(
                    RELEASE_NOT_SENT if error.outcome == NOT_SENT else RELEASE_REJECTED), now=now)
                self.journal.add_cost_event_in_tx(conn, attempt=begun.attempt, kind=COST_NONE,
                                                  cost_micros=0, usage=None, now=now)
            else:
                self.budget.mark_unknown_in_tx(conn, begun.reservation_id, now)
                self.journal.add_cost_event_in_tx(conn, attempt=begun.attempt, kind=COST_ESTIMATED_UNKNOWN,
                                                  cost_micros=begun.reserved_micros, usage=None, now=now)

        await self.store.atransaction(work)

    async def _fail(self, begun: _Begun, error: ModelCallError) -> GatewayResult:
        await self._record_failure(begun, error)
        raise CallFailed(error.code, outcome=error.outcome, attempt_key=begun.attempt.attempt_key,
                         detail=error.detail)

    # --- later usage -----------------------------------------------------

    async def report_late_usage(self, attempt_key: str, usage: Usage) -> bool:
        """Usage for an UNKNOWN attempt arrived later. Settles its reservation once.
        Returns False for an exact repeat. Priced at the current ai.yaml price."""
        now = self.clock.now()

        def work(conn: Any) -> bool:
            attempt = self.journal.get_in_tx(conn, attempt_key)
            if attempt is None:
                raise CallRefused("ATTEMPT_UNKNOWN", attempt_key)
            price = self.config.prices.price_for(attempt.backend, attempt.model)
            if price is None:
                raise CallRefused("PRICE_UNAVAILABLE", f"{attempt.backend}/{attempt.model} has no price")
            if not self.journal.reconcile_late_usage_in_tx(conn, attempt_key, usage, now):
                return False
            cost = price.cost_micros(usage.input_tokens, usage.output_tokens)
            self.budget.settle_in_tx(conn, attempt.reservation_id, actual_micros=cost,
                                     input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                     now=now, late=True)
            self.journal.add_cost_event_in_tx(conn, attempt=attempt, kind=COST_CORRECTION,
                                              cost_micros=cost, usage=usage, now=now)
            return True

        return await self.store.atransaction(work)


def build_gateway(config: AiConfig, *, store: AiStore, clock: Clock, environ: Mapping[str, str],
                  clients: Optional[Mapping[str, ModelClient]] = None) -> ModelGateway:
    """Build real adapters from env unless the caller passes ready clients (tests do)."""
    built = {name: (clients or {}).get(name) or build_model_client(config.role(name), environ=environ)
             for name in ROLE_NAMES}
    return ModelGateway(config=config, store=store, clock=clock, clients=built)
