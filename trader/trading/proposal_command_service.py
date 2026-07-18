"""Trader-owned proposal creation, rejection, and expiry authority."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Protocol

from trader.data.domain_journal import DomainJournal
from trader.data.proposal_repository import ProposalDraft, ProposalRecord, ProposalRepository
from trader.trading.trading_control import PauseStateUnavailable, TradingPausedError


@dataclass(frozen=True)
class ExecutableQuote:
    conid: int
    side: str
    price: float
    market_timestamp: dt.datetime
    feed_type: str
    session_state: str
    bid: Optional[float] = None
    ask: Optional[float] = None


class QuoteAuthority(Protocol):
    def executable_quote(self, conid: int, *, side: str) -> Optional[ExecutableQuote]: ...


class UniverseAuthority(Protocol):
    def resolve_conid(self, conid: int) -> Any | None: ...


class PositionAuthority(Protocol):
    """Broker-verified reducible quantity for one (account, conid).

    Used ONLY to decide whether a SELL is a position-reducing close (exempt
    from the pause gate) or an exposure-increasing short (not exempt) --
    never caller-supplied. [M1-F3] Task 5's approval saga reuses the same
    protocol shape for its own risk-direction classification.
    """

    def reducible_quantity(self, account_id: str, conid: int) -> float: ...


@dataclass(frozen=True)
class ProposalCreateRequest:
    conid: int
    action: str
    quantity: Optional[float] = None
    amount: Optional[float] = None
    execution: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    confidence: float = 0.0
    thesis: str = ""
    group: str = ""
    max_price_drift_bps: Optional[float] = None


class ProposalCreationRefused(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _ConcurrentProposalChange(Exception):
    """A CAS lost to another writer; safe for the periodic sweep to skip."""


class ProposalCommandService:
    """The only Task-2 component allowed to mutate journal proposals.

    Every state transition is performed in the materialized-write callback
    supplied to ``DomainJournal.mutate``. This deliberately avoids wrapping
    ``mutate`` in ``DuckDBConnection.transaction``: ``mutate`` owns the only
    BEGIN/COMMIT pair and rolls both row and event back together on failure.
    """

    DEFAULT_DRIFT_BPS = 50.0

    def __init__(
        self,
        *,
        repository: ProposalRepository,
        journal: DomainJournal,
        risk_gate: Any,
        quotes: QuoteAuthority,
        universe: UniverseAuthority,
        account_id: str,
        account_mode: str,
        now: Callable[[], dt.datetime],
        ttl: dt.timedelta = dt.timedelta(minutes=5),
        controls: Any | None = None,
        sizer: Any | None = None,
        positions: Optional[PositionAuthority] = None,
    ):
        self._repository = repository
        self._journal = journal
        self._risk_gate = risk_gate
        self._quotes = quotes
        self._universe = universe
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._ttl = ttl
        self._controls = controls
        self._sizer = sizer
        self._positions = positions

    def create_proposal(
        self, request: ProposalCreateRequest, *, source: str, correlation_id: str
    ) -> ProposalRecord:
        if request.action not in {"BUY", "SELL"}:
            raise ProposalCreationRefused("INVALID_ACTION", "action must be BUY or SELL")
        if request.quantity is not None and request.amount is not None:
            raise ProposalCreationRefused("INVALID_SIZE", "quantity and amount are mutually exclusive")

        secdef = self._universe.resolve_conid(request.conid)
        if secdef is None:
            raise ProposalCreationRefused(
                "UNKNOWN_CONID", f"no exact local security definition for conId {request.conid}"
            )
        instrument_check = self._risk_gate.check_instrument(
            symbol=secdef.symbol,
            exchange=getattr(secdef, "primaryExchange", "") or "",
            sec_type=getattr(secdef, "secType", "") or "STK",
        )
        if not instrument_check.approved:
            raise ProposalCreationRefused("TRADING_FILTER_REJECTED", instrument_check.reason)

        if self._controls is not None and not self._is_reducing_close(request):
            try:
                self._controls.require_unpaused(self._account_id)
            except TradingPausedError as exc:
                raise ProposalCreationRefused("TRADING_PAUSED", str(exc)) from exc
            except PauseStateUnavailable as exc:
                raise ProposalCreationRefused("TRADING_PAUSED", str(exc)) from exc

        side = "ask" if request.action == "BUY" else "bid"
        quote = self._quotes.executable_quote(request.conid, side=side)
        if quote is None or quote.price <= 0:
            raise ProposalCreationRefused(
                "QUOTE_UNAVAILABLE",
                f"no executable {side} quote for conId {request.conid}",
            )
        if quote.conid != request.conid or quote.side != side:
            raise ProposalCreationRefused("QUOTE_UNAVAILABLE", "quote identity or side mismatch")
        if quote.market_timestamp.tzinfo is None:
            raise ProposalCreationRefused("QUOTE_UNAVAILABLE", "quote timestamp is not timezone-aware")

        quantity, amount = self._size(request, quote.price)
        now = self._as_utc(self._now())
        proposal_id = self._repository.reserve_id()
        metadata = {"group": request.group} if request.group else {}
        draft = ProposalDraft(
            id=proposal_id,
            symbol=secdef.symbol,
            action=request.action,
            quantity=quantity,
            amount=amount,
            execution=dict(request.execution),
            reasoning=request.reasoning,
            confidence=float(request.confidence),
            thesis=request.thesis,
            source=source,
            metadata=metadata,
            sec_type=getattr(secdef, "secType", "") or "STK",
            account_id=self._account_id,
            account_mode=self._account_mode,
            conid=request.conid,
            reference_price=float(quote.price),
            reference_timestamp=self._as_utc(quote.market_timestamp),
            reference_quote_side=quote.side,
            reference_feed_type=quote.feed_type,
            max_price_drift_bps=(
                float(request.max_price_drift_bps)
                if request.max_price_drift_bps is not None else self.DEFAULT_DRIFT_BPS
            ),
            expires_at=now + self._ttl,
            live_approval_eligible=True,
            created_at=now,
        )
        predicted = self._record_from_draft(draft, revision=1)
        written: list[ProposalRecord] = []

        def write_materialized(conn: Any, revision: int) -> None:
            if revision != 1:
                raise _ConcurrentProposalChange("new proposal unexpectedly has prior revision")
            if source.startswith("strategy:") and self._repository.pending_duplicate_in_tx(
                conn, source, request.conid, request.action
            ):
                raise ProposalCreationRefused(
                    "DUPLICATE_PENDING",
                    f"a pending {request.action} for conId {request.conid} already exists from {source}",
                )
            written.append(self._repository.insert_pending_in_tx(conn, draft, revision))

        self._journal.mutate(
            self._journal.connect(),
            self._repository.mutation_for(predicted, correlation_id),
            write_materialized,
            event_id=f"proposal:{proposal_id}:1",
        )
        return written[0]

    def reject_proposal(
        self, proposal_id: int, reason: str, correlation_id: str
    ) -> ProposalRecord:
        current = self._repository.get(proposal_id)
        if current is None:
            raise ProposalCreationRefused("NOT_FOUND", f"proposal {proposal_id} does not exist")
        if current.status == "REJECTED":
            return current
        if current.status != "PENDING":
            raise ProposalCreationRefused("NOT_PENDING", f"proposal {proposal_id} is {current.status}")

        now = self._as_utc(self._now())
        predicted = replace(
            current,
            status="REJECTED",
            rejection_reason=reason,
            updated_at=now,
            revision=current.revision + 1,
        )
        written: list[ProposalRecord] = []

        def write_materialized(conn: Any, revision: int) -> None:
            if revision != predicted.revision:
                raise _ConcurrentProposalChange("journal and proposal revision diverged")
            record = self._repository.reject_in_tx(
                conn, proposal_id, reason, now, current.revision
            )
            if record is None:
                raise _ConcurrentProposalChange("proposal changed before rejection")
            written.append(record)

        self._journal.mutate(
            self._journal.connect(),
            self._repository.mutation_for(predicted, correlation_id),
            write_materialized,
            event_id=f"proposal:{proposal_id}:{predicted.revision}",
        )
        return written[0]

    def expire_stale(self, now: dt.datetime) -> list[int]:
        now = self._as_utc(now)
        expired: list[int] = []
        for current in self._repository.stale_pending(now):
            predicted = replace(
                current,
                status="EXPIRED",
                updated_at=now,
                revision=current.revision + 1,
            )
            written: list[ProposalRecord] = []

            def write_materialized(conn: Any, revision: int) -> None:
                if revision != predicted.revision:
                    raise _ConcurrentProposalChange("journal and proposal revision diverged")
                record = self._repository.expire_in_tx(
                    conn, current.id, now, current.revision
                )
                if record is None:
                    raise _ConcurrentProposalChange("proposal changed before expiry")
                written.append(record)

            try:
                self._journal.mutate(
                    self._journal.connect(),
                    self._repository.mutation_for(predicted, correlation_id=None),
                    write_materialized,
                    event_id=f"proposal:{current.id}:{predicted.revision}",
                )
            except _ConcurrentProposalChange:
                continue
            expired.append(written[0].id)
        return expired

    async def run_expiry_loop(self, interval_seconds: float = 30.0) -> None:
        self.expire_stale(self._as_utc(self._now()))
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                self.expire_stale(self._as_utc(self._now()))
            except Exception:
                logging.exception("proposal expiry sweep failed; retrying on the next tick")

    def _is_reducing_close(self, request: ProposalCreateRequest) -> bool:
        """True only for a SELL whose quantity is VERIFIED (broker-reported
        held/reducible quantity, never caller-supplied) to be <= what's
        actually held. An amount-based or auto-sized SELL (quantity not
        known yet -- sizing happens after this check) or one with no wired
        ``PositionAuthority`` is NOT exempt: fail closed, treat as
        exposure-increasing."""
        if request.action != "SELL":
            return False
        if request.quantity is None or self._positions is None:
            return False
        held = self._positions.reducible_quantity(self._account_id, request.conid)
        return request.quantity <= held

    def _size(self, request: ProposalCreateRequest, price: float) -> tuple[Optional[float], float]:
        if request.quantity is not None:
            if request.quantity <= 0:
                raise ProposalCreationRefused("INVALID_SIZE", "quantity must be positive")
            return float(request.quantity), float(request.quantity) * price
        if request.amount is not None:
            if request.amount <= 0:
                raise ProposalCreationRefused("INVALID_SIZE", "amount must be positive")
            return float(request.amount) / price, float(request.amount)
        if self._sizer is None:
            raise ProposalCreationRefused("SIZING_BLOCKED", "no position sizer is configured")
        result = self._sizer.compute(confidence=float(request.confidence), price=price)
        if result.amount_usd <= 0:
            raise ProposalCreationRefused("SIZING_BLOCKED", "position sizing produced no exposure")
        quantity = float(result.quantity) if result.quantity else result.amount_usd / price
        return quantity, float(result.amount_usd)

    @staticmethod
    def _as_utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("proposal timestamps must be timezone-aware")
        return value.astimezone(dt.timezone.utc)

    @staticmethod
    def _record_from_draft(draft: ProposalDraft, revision: int) -> ProposalRecord:
        return ProposalRecord(
            id=draft.id,
            symbol=draft.symbol,
            action=draft.action,
            quantity=draft.quantity,
            amount=draft.amount,
            execution=draft.execution,
            reasoning=draft.reasoning,
            confidence=draft.confidence,
            thesis=draft.thesis,
            source=draft.source,
            metadata=draft.metadata,
            status="PENDING",
            created_at=draft.created_at,
            updated_at=draft.created_at,
            order_ids=[],
            rejection_reason="",
            sec_type=draft.sec_type,
            account_id=draft.account_id,
            account_mode=draft.account_mode,
            conid=draft.conid,
            reference_price=draft.reference_price,
            reference_timestamp=draft.reference_timestamp,
            reference_quote_side=draft.reference_quote_side,
            reference_feed_type=draft.reference_feed_type,
            max_price_drift_bps=draft.max_price_drift_bps,
            expires_at=draft.expires_at,
            live_approval_eligible=draft.live_approval_eligible,
            revision=revision,
            order_group_id=None,
        )
