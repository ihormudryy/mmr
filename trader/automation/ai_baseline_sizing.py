"""Size a baseline exactly as a real ai_paper ENTER of the same deployment is sized now (SP2 Plan 2 Ruling 19).

A discretionary deployment is sized as a real discretionary ENTER: on the scope rule's liquidity cap and
20-session volume, and never for an instrument outside the rule (SP2 Plan 3 Ruling 19, issue #85).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.ai_paper_config import STYLE_NOT_ENABLED
from trader.automation.ai_paper_evidence import (
    AI_ENTRY_POLICY, check_paper_binding, entry_liquidity, planned_entry_limit, validate_entry_snapshot,
)
from trader.automation.ai_paper_sizing import max_entry_quantity, pending_entry_refusal, sizing_inputs
from trader.automation.discretionary_deployment import DiscretionaryDeployment
from trader.automation.discretionary_scope import OUT_OF_DISCRETIONARY_SCOPE
from trader.automation.liquidity_policy import MAX_SPREAD_BPS, LiquidityPolicy
from trader.automation.production_evidence import TwentySessionVolume, validate_entry_quote
from trader.research.market_context import LIVE_NOTIONAL_TOLERANCE
from trader.scoreboard.ports import SizedBaseline, SizingUnavailable
from trader.trading.approval_context import ApprovalContextError

QUOTE_UNAVAILABLE, QUOTE_NOT_EXECUTABLE = "quote_unavailable", "quote_not_executable"


@dataclass(frozen=True)
class _DeploymentBound:
    """What the deployment adds to a real ENTER's sizing, and the one quote the baseline is sized on.
    ``volume`` is the scope rule's 20-session window; a strategy deployment has none and reads local bars."""
    attested_notional: float
    quote: Any
    volume: Optional[TwentySessionVolume] = None


class AiPaperBaselineSizer:
    def __init__(self, *, broker: Any, quotes: Any, history: Any, policy: Any, deployments: Any,
                 accepted_feeds: frozenset[str], entry_filter: Any, now: Callable[[], dt.datetime],
                 config: Any, liquidity_policy: Optional[LiquidityPolicy] = None, scope: Any = None):
        self._broker, self._quotes, self._history = broker, quotes, history
        self._config = config                       # the AiPaperConfig whose styles a real ENTER admits
        self._filter = entry_filter                 # the AiEntryFilter a real ENTER applies
        self._scope = scope                         # the DiscretionaryScopeService a real ENTER admits on
        self._policy, self._deployments, self._now = policy, deployments, now
        self._feeds = frozenset(accepted_feeds)
        self._liquidity_policy = liquidity_policy or LiquidityPolicy(accepted_feeds=self._feeds)

    def size(self, *, account_id: str, deployment_digest: str, conid: int, reference_price: float,
             stop_price: float) -> SizedBaseline:
        # The real ENTER's evidence checks first (review 4210055360): paper only, a valid broker fence.
        self._step("PAPER_ONLY", lambda: check_paper_binding("paper", account_id))
        limits = self._step("NO_EFFECTIVE_LIMITS", self._policy.effective_limits)
        deployment = self._deployment(deployment_digest, conid)
        snapshot = self._step("EVIDENCE_UNAVAILABLE", lambda: self._broker.capture(account_id))
        snapshot = self._step("BROKER_EVIDENCE_INVALID", lambda: validate_entry_snapshot(snapshot, account_id))
        refusal = pending_entry_refusal(snapshot, conid, limits)
        if refusal:
            raise SizingUnavailable(refusal, {})
        # One quote read, where prepare_entry reads it; the scope rule, the quote checks and the size all use it.
        bound = self._bound(deployment_digest, deployment, conid)
        notional_cap = bound.attested_notional * (1.0 + LIVE_NOTIONAL_TOLERANCE)
        quote = self._checked_quote(conid, bound.quote)
        # The same price prepare_entry sizes on: the marketable limit through the fresh ask.
        price = planned_entry_limit(float(quote.ask), float(quote.bid), AI_ENTRY_POLICY.limit_offset_bps)
        if not stop_price < price:
            raise SizingUnavailable("STOP_INVALID", {"price": price, "stop": stop_price})
        self._check_filter(conid, price)
        liquidity = self._step("LIQUIDITY_UNAVAILABLE",
                               lambda: entry_liquidity(self._history, conid, quote, self._now(), bound.volume))
        inputs = sizing_inputs(snapshot, conid=conid, price=price, stop_price=stop_price,
                               liquidity_max_shares=self._liquidity_policy.max_quantity(liquidity),
                               notional_cap=notional_cap)
        quantity = max_entry_quantity(limits, inputs)
        record = {"limits": limits.to_json(), "price": price, "reference_price": reference_price, "feed": quote.feed_type,
                  "equity": inputs.equity, "existing_position_value": inputs.existing_position_value,
                  "current_gross_notional": inputs.current_gross_notional if math.isfinite(inputs.current_gross_notional) else None,
                  "liquidity_max_shares": inputs.liquidity_max_shares, "notional_cap": notional_cap,
                  "max_quantity": quantity}
        if quantity < 1:
            raise SizingUnavailable("QUANTITY_BELOW_ONE_SHARE", record)
        return SizedBaseline(quantity, record)

    def _read_quote(self, conid: int) -> Any:
        try:
            return self._quotes.executable_quote(conid, side="BUY")
        except Exception as exc:
            raise SizingUnavailable("QUOTE_UNAVAILABLE", {"error": type(exc).__name__}, reason=QUOTE_UNAVAILABLE) from None

    def _checked_quote(self, conid: int, quote: Any) -> Any:
        """Exactly the real ENTER's quote checks (second PR #75 review): never size on a quote it would refuse."""
        if quote is None:
            raise SizingUnavailable("QUOTE_UNAVAILABLE", {}, reason=QUOTE_UNAVAILABLE)
        try:
            validate_entry_quote(quote, conid=conid, side="BUY", now=self._now(), accepted_feeds=self._feeds)
        except ApprovalContextError as refused:
            raise SizingUnavailable(refused.code, {"feed": quote.feed_type, "session": quote.session_state},
                                    reason=QUOTE_NOT_EXECUTABLE) from None
        spread_bps = (float(quote.ask) - float(quote.bid)) / float(quote.price) * 10_000.0
        if spread_bps > MAX_SPREAD_BPS:
            raise SizingUnavailable("SPREAD_BPS", {"spread_bps": spread_bps}, reason=QUOTE_NOT_EXECUTABLE)
        return quote

    def _check_filter(self, conid: int, price: float) -> None:
        """The trading filter the real ENTER applies on the same price."""
        refusal = self._step("TRADING_FILTER_UNAVAILABLE", lambda: self._filter.refusal(conid, price))
        if refusal:
            raise SizingUnavailable(refusal, {"price": price})

    def _deployment(self, digest: str, conid: int) -> Any:
        """The real ENTER's deployment checks: a strategy one's verdict and conids, then either kind's style."""
        deployment = self._step("DEPLOYMENT_UNAVAILABLE", lambda: self._deployments.get_sealed_any(digest))
        if not isinstance(deployment, DiscretionaryDeployment):
            if deployment.decider_verdict != "DEPLOY":
                raise SizingUnavailable("DEPLOYMENT_NOT_DEPLOYABLE", {})
            if conid not in deployment.conids:
                raise SizingUnavailable("CONID_NOT_IN_DEPLOYMENT", {})
        if not self._config.style_enabled(deployment.style):
            raise SizingUnavailable(STYLE_NOT_ENABLED, {"style": deployment.style})
        return deployment

    def _bound(self, digest: str, deployment: Any, conid: int) -> _DeploymentBound:
        if isinstance(deployment, DiscretionaryDeployment):
            return self._scope_bound(digest, deployment, conid)
        return _DeploymentBound(float(deployment.evidence_order_notional), self._read_quote(conid))

    def _scope_bound(self, digest: str, deployment: DiscretionaryDeployment, conid: int) -> _DeploymentBound:
        """The real discretionary ENTER's scope check, without its record: out of scope is never sized. Its quote
        is read after the IB contract details, as at admission, and is the one quote the baseline is sized on."""
        if self._scope is None:
            raise SizingUnavailable(OUT_OF_DISCRETIONARY_SCOPE,
                                    {"part": "evidence_stale", "reason": "the scope service is not wired"})
        assessment = self._step(OUT_OF_DISCRETIONARY_SCOPE, lambda: self._scope.assess(
            digest=digest, deployment=deployment, conid=conid))
        admission = assessment.admission
        if admission is None:
            raise SizingUnavailable(OUT_OF_DISCRETIONARY_SCOPE,
                                    {"part": assessment.verdict.part, "reason": assessment.verdict.reason})
        return _DeploymentBound(admission.attested_notional, assessment.quote, admission.evidence.volume)

    @staticmethod
    def _step(code: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except SizingUnavailable:
            raise
        except Exception as exc:           # a refusal code or the class name, never provider text
            raise SizingUnavailable(getattr(exc, "code", None) or code, {"error": type(exc).__name__}) from None
