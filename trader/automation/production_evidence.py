"""Trader-owned, paper-only evidence for automated intents.

Authorities are injected at composition; no broker connection or data download is
opened here. Session evidence consumes the original approval, never a recapture.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, cast

import exchange_calendars as xcals
import pandas as pd

from trader.automation.calendar_policy import ET
from trader.automation.artifact_verifier import VerifiedArtifact
from trader.automation.liquidity_policy import LiquidityEvidence
from trader.automation.models import ExecutionIntent
from trader.automation.risk_limits import PAPER_LIMITS
from trader.automation.session_risk import AllocationCeiling, AutomationSessionState
from trader.data.store import DateRange
from trader.objects import BarSize, WhatToShow
from trader.promotion.canary_risk import CanaryRiskStore

from trader.trading.approval_context import (
    ApprovalContext, ApprovalContextError, BrokerRiskSnapshotAuthority,
    WhatIfMarginAuthority, capture_approval_context,
)
from trader.trading.dispatch_guard import MAX_QUOTE_AGE_SECONDS, MAX_SOURCE_CLOCK_SKEW_SECONDS
from trader.trading.proposal_command_service import ExecutableQuote, QuoteAuthority

if TYPE_CHECKING:
    from trader.data.data_access import TickStorage
    from trader.data.domain_journal import DomainJournal


def _number(value: Any, code: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ApprovalContextError(code, "required numeric evidence is invalid") from None
    if not math.isfinite(number) or (positive and number <= 0):
        raise ApprovalContextError(code, "required numeric evidence is invalid")
    return number


class ProductionAutomationEvidence:
    """US-equity evidence, with fail-closed capture and no autonomous sizing.

    ``broker`` must be the trader's fenced materialized-state authority; its
    readiness and generation checks remain authoritative. ``promoted_at`` is
    the enumeration barrier, not a quote-age clock: broker deltas after that
    barrier are included in its fenced read. ``history`` is local TickStorage.
    The verified artifact allowlist owns the initial US instrument scope.
    """

    def __init__(
        self, *, broker: BrokerRiskSnapshotAuthority, quotes: QuoteAuthority,
        margin: WhatIfMarginAuthority, history: TickStorage | None,
        journal: DomainJournal, account_id: str, account_mode: str,
        strategy_id: str | None, max_drift_bps: float,
        now: Callable[[], dt.datetime],
    ) -> None:
        self._broker = broker
        self._quotes = quotes
        self._margin = margin
        self._history = history
        self._journal = journal
        self._account_id = account_id
        self._account_mode = account_mode
        self._strategy_id = strategy_id
        self._max_drift_bps = max_drift_bps
        self._now = now

    def approval_factory(self, *, intent: ExecutionIntent, command: Any) -> ApprovalContext:
        self._check_binding(intent)
        if intent.requested_quantity is None:
            raise ApprovalContextError("QUANTITY_REQUIRED", "explicit quantity is required")
        quantity = _number(intent.requested_quantity, "QUANTITY_INVALID", positive=True)
        drift = _number(self._max_drift_bps, "DRIFT_INVALID", positive=True)
        try:
            approval = capture_approval_context(
                account_id=self._account_id, conid=intent.conid, side=intent.side,
                quantity=quantity, quotes=self._quotes,
                broker=self._broker, margin=self._margin, now=self._now(),
                max_drift_bps=drift,
            )
        except Exception:
            # Provider exception text is opaque and may contain credentials.
            raise ApprovalContextError("EVIDENCE_UNAVAILABLE", "approval capture failed") from None
        self._validate_approval(intent, approval, self._now())
        return replace(approval, risk_direction="REDUCING" if intent.side == "SELL" else "INCREASING")

    def _validate_approval(self, intent: ExecutionIntent, approval: ApprovalContext, now: dt.datetime) -> None:
        if (approval.conid != intent.conid or approval.side != intent.side
                or approval.quantity != _number(intent.requested_quantity, "QUANTITY_INVALID", positive=True)):
            raise ApprovalContextError("APPROVAL_MISMATCH", "approval does not match intent")
        broker = approval.broker
        if broker.account_id != self._account_id:
            raise ApprovalContextError("ACCOUNT_MISMATCH", "broker account does not match")
        if broker.account_mode != "paper":
            raise ApprovalContextError("PAPER_ONLY", "broker is not in paper mode")
        if (type(broker.generation_id) is not int or broker.generation_id <= 0
                or type(broker.source_cursor) is not int or broker.source_cursor < 0):
            raise ApprovalContextError("BROKER_FENCE_INVALID", "complete broker fence is required")
        _number(broker.net_liquidation, "BROKER_EVIDENCE_INVALID", positive=True)
        _number(broker.daily_pnl, "BROKER_EVIDENCE_INVALID")
        if intent.side == "SELL":
            held = _number(broker.reducible_quantity(intent.conid), "BROKER_EVIDENCE_INVALID")
            if held < approval.quantity:
                raise ApprovalContextError("LONG_ONLY", "sell exceeds the fenced long position")

        quote = approval.quote
        if quote.conid != intent.conid or quote.side != intent.side:
            raise ApprovalContextError("QUOTE_MISMATCH", "quote identity does not match intent")
        if (not isinstance(quote.market_timestamp, dt.datetime)
                or quote.market_timestamp.utcoffset() is None):
            raise ApprovalContextError("QUOTE_CLOCK_INVALID", "aware market timestamp is required")
        age = (now - quote.market_timestamp).total_seconds()
        if age > MAX_QUOTE_AGE_SECONDS:
            raise ApprovalContextError("QUOTE_STALE", "executable quote is stale")
        if age < -MAX_SOURCE_CLOCK_SKEW_SECONDS:
            raise ApprovalContextError("QUOTE_CLOCK_INVALID", "market clock is in the future")
        price = _number(quote.price, "QUOTE_INVALID", positive=True)
        bid = _number(quote.bid, "QUOTE_INVALID", positive=True)
        ask = _number(quote.ask, "QUOTE_INVALID", positive=True)
        if ask < bid or price != (ask if intent.side == "BUY" else bid):
            raise ApprovalContextError("QUOTE_INVALID", "crossable side of book is required")
        if intent.side == "BUY" and quote.feed_type != "live":
            raise ApprovalContextError("FEED_NOT_LIVE", "automated entry requires a live feed")
        if quote.session_state != "continuous":
            raise ApprovalContextError("QUOTE_SESSION_INVALID", "continuous trading evidence is required")

    def _check_binding(self, intent: ExecutionIntent) -> None:
        if (self._account_mode != "paper" or intent.account_mode != "paper"
                or not isinstance(self._account_id, str)
                or not self._account_id.startswith("DU")):
            raise ApprovalContextError("PAPER_ONLY", "automation requires a paper account")
        if not isinstance(self._strategy_id, str) or not self._strategy_id.strip():
            raise ApprovalContextError("STRATEGY_REQUIRED", "configured strategy identity is required")

    def session_state_factory(
        self, *, intent: ExecutionIntent, command: Any, approval: ApprovalContext,
    ) -> AutomationSessionState:
        self._check_binding(intent)
        now = self._now()
        self._validate_approval(intent, approval, now)
        store = CanaryRiskStore(self._journal, cast(str, self._strategy_id), self._account_id)
        try:
            high_water_mark = store.update_high_water_mark(approval.broker.net_liquidation, now)
        except Exception:
            raise ApprovalContextError("HWM_UNAVAILABLE", "durable high-water-mark update failed") from None
        liquidity = self._liquidity(intent, approval.quote, now) if intent.side == "BUY" else None
        # Persistence/history I/O can consume the quote's freshness window.
        self._validate_approval(intent, approval, self._now())
        return AutomationSessionState(
            high_water_mark=high_water_mark, expected_account_id=self._account_id,
            limits=PAPER_LIMITS, liquidity=liquidity,
        )

    def allocation_factory(
        self, *, intent: ExecutionIntent, artifact: VerifiedArtifact, command: Any,
    ) -> AllocationCeiling:
        self._check_binding(intent)
        if artifact.artifact_id != intent.artifact_id:
            raise ApprovalContextError("ARTIFACT_MISMATCH", "artifact does not match intent")
        ceiling = _number(artifact.max_gross_allocation, "ALLOCATION_INVALID", positive=True)
        if artifact.expires_at <= self._now():
            raise ApprovalContextError("ALLOCATION_INVALID", "artifact authority has expired")
        # Paper ceiling is trader-owned. No request field can raise it, and no
        # signed allocation authority is invented from eligibility evidence.
        return AllocationCeiling(max_gross_fraction=min(PAPER_LIMITS.gross_fraction, ceiling))

    def _liquidity(self, intent: ExecutionIntent, quote: ExecutableQuote, now: dt.datetime) -> LiquidityEvidence:
        depth = _number(quote.ask_size, "DEPTH_INVALID")
        if depth < 0:
            raise ApprovalContextError("DEPTH_INVALID", "observed side depth is invalid")
        # TickStorage.read combines every bar size. Select its daily library,
        # then require explicit daily rows (legacy NULL bar sizes are ambiguous).
        try:
            calendar = xcals.get_calendar("XNYS")
            today = now.astimezone(ET).date()
            sessions = calendar.sessions_in_range(today - dt.timedelta(days=90), today)
            closed = [day for day in sessions if calendar.session_close(day) < pd.Timestamp(now)][-20:]
            start = dt.datetime.combine(closed[0].date(), dt.time(), ET)
            frame = cast("TickStorage", self._history).get_tickdata(BarSize.Days1).read(
                contract=intent.conid, date_range=DateRange(start, now),
            )
        except Exception:
            raise ApprovalContextError("HISTORY_UNAVAILABLE", "daily history read failed") from None
        if (not isinstance(frame, pd.DataFrame) or frame.empty
                or not {"bar_size", "close", "volume", "what_to_show"}.issubset(frame.columns)
                or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None
                or frame.index.hasnans):
            raise ApprovalContextError("HISTORY_INVALID", "dated daily trade bars are required")
        frame = frame.loc[frame.bar_size == "1 day"].copy()
        frame.index = pd.Index(frame.index.tz_convert(ET).date)
        expected = [day.date() for day in closed]
        daily = frame.loc[frame.index.isin(expected)]
        if len(daily) != 20 or daily.index.has_duplicates or set(daily.index) != set(expected):
            raise ApprovalContextError("HISTORY_INVALID", "all twenty latest closed sessions are required")
        if not (daily.what_to_show == int(WhatToShow.TRADES)).all():
            raise ApprovalContextError("HISTORY_INVALID", "trade volume provenance is required")
        volumes = [_number(value, "HISTORY_INVALID", positive=True) for value in daily.volume]
        closes = [_number(value, "HISTORY_INVALID", positive=True) for value in daily.close]
        dollars = [_number(price * volume, "HISTORY_INVALID", positive=True)
                   for price, volume in zip(closes, volumes)]
        adv = _number(sum(volumes) / 20, "HISTORY_INVALID", positive=True)
        return LiquidityEvidence(
            price=quote.price,
            median_dollar_volume_20d=_number(statistics.median(dollars), "HISTORY_INVALID", positive=True),
            adv_shares_20d=adv,
            spread_bps=(cast(float, quote.ask) - cast(float, quote.bid)) / quote.price * 10_000.0,
            top_of_book_depth=depth,
            feed_type=quote.feed_type, session_state=quote.session_state,
        )
