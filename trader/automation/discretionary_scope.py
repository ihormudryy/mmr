"""The discretionary scope rule (SP2 spec 6.6, Plan 3 rulings 3-10).

A pure evaluation of the sealed rule against fresh evidence, and the table that
records every check (migration 100). The trader checks the rule at admission and
again at dispatch; each refusal is ``OUT_OF_DISCRETIONARY_SCOPE`` with the failed
part. Reductions never come here.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from trader.automation.ai_deployments import DISCRETIONARY_KIND
from trader.automation.ai_paper_evidence import AI_ENTRY_POLICY, AI_PAPER_ACTION, planned_entry_limit
from trader.automation.liquidity_policy import MIN_MEDIAN_DOLLAR_VOLUME
from trader.automation.production_evidence import TwentySessionVolume
from trader.automation.scope_evidence import ContractEvidence, ScopeEvidenceUnavailable
from trader.data.schema_migrations import SchemaMigrator
from trader.research.market_context import LIVE_NOTIONAL_TOLERANCE
from trader.trading.dispatch_guard import MAX_QUOTE_AGE_SECONDS, MAX_SOURCE_CLOCK_SKEW_SECONDS

OUT_OF_DISCRETIONARY_SCOPE = "OUT_OF_DISCRETIONARY_SCOPE"
SCOPE_PARTS = ("exchange", "instrument_type", "price", "dollar_volume", "liquidity", "trading_filter",
               "evidence_stale")
SCOPE_CHECK_MIGRATION_VERSION = 100
PHASES = ("admission", "sizing", "dispatch")
DISPATCH_EVIDENCE_MAX_AGE = dt.timedelta(seconds=60)

FilterRefusal = Callable[[ContractEvidence, float], Optional[str]]


def apply_scope_check_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(SCOPE_CHECK_MIGRATION_VERSION, "sp2_discretionary_scope_checks", (
        """CREATE TABLE IF NOT EXISTS discretionary_scope_checks (
            command_id VARCHAR NOT NULL, phase VARCHAR NOT NULL, deployment_digest VARCHAR NOT NULL,
            conid BIGINT NOT NULL, passed BOOLEAN NOT NULL, part VARCHAR, reason VARCHAR NOT NULL,
            evidence_json VARCHAR NOT NULL, checked_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (command_id, phase))""",
    ))


@dataclass(frozen=True)
class ScopeInputs:
    """Everything one check reads. ``accepted_feeds`` is always the command stack's own set (PR #76)."""
    contract: Optional[ContractEvidence]
    quote: Any
    volume: Optional[TwentySessionVolume]
    order_notional: Optional[float]
    filter_refusal: FilterRefusal
    accepted_feeds: frozenset[str]
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopeVerdict:
    part: Optional[str]
    reason: str
    evidence: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.part is None


def effective_dollar_volume_floor(rule: Any) -> float:
    """Ruling 6: SP1's session_risk floor applies to every entry, so the stricter floor is the real one."""
    return max(rule.min_median_dollar_volume, MIN_MEDIAN_DOLLAR_VOLUME)


def static_scope_refusal(rule: Any, contract: ContractEvidence) -> Optional[tuple[str, str]]:
    """Ruling 3: listing exchange and instrument type from IB's own details; a blank never passes."""
    if contract.primary_exchange not in rule.primary_exchanges:
        return ("exchange", f"primary listing {contract.primary_exchange or '(blank)'} "
                            f"is not in {list(rule.primary_exchanges)}")
    if contract.sec_type != "STK" or contract.currency != "USD":
        return ("instrument_type", f"{contract.sec_type or '(blank)'}/{contract.currency or '(blank)'} "
                                   "is not a USD stock")
    if contract.stock_type not in rule.stock_types:
        return ("instrument_type", f"IB stockType {contract.stock_type or '(blank)'} "
                                   f"is not in {list(rule.stock_types)}")
    return None


def _finite_positive(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def quote_problem(quote: Any, now: dt.datetime, accepted_feeds: frozenset[str]) -> Optional[str]:
    """Ruling 4: the quote authority's quote, with a feed the command stack accepts (owner #74)."""
    if quote is None:
        return "no executable quote"
    age = (now - quote.market_timestamp).total_seconds()
    if age > MAX_QUOTE_AGE_SECONDS or age < -MAX_SOURCE_CLOCK_SKEW_SECONDS:
        return f"{quote.feed_type} quote is {age:.1f} s old"
    if quote.feed_type not in accepted_feeds:
        return f"quote feed {quote.feed_type} is not accepted here (accepted: {sorted(accepted_feeds)})"
    if quote.session_state != "continuous":
        return f"{quote.feed_type} quote is {quote.session_state}, not continuous trading"
    if not (_finite_positive(quote.bid) and _finite_positive(quote.ask)) or quote.ask < quote.bid:
        return f"{quote.feed_type} quote has no valid bid and ask"
    return None


def evaluate_scope(rule: Any, inputs: ScopeInputs, now: dt.datetime) -> ScopeVerdict:
    """The first failed part, in a fixed order; evidence that cannot be verified fresh is ``evidence_stale``."""
    evidence = _evidence_json(inputs)

    def refuse(part: str, reason: str) -> ScopeVerdict:
        return ScopeVerdict(part, reason, evidence)

    if inputs.contract is None:
        return refuse("evidence_stale", "; ".join(inputs.missing) or "no IB contract details")
    static = static_scope_refusal(rule, inputs.contract)
    if static is not None:
        return refuse(*static)
    problem = quote_problem(inputs.quote, now, inputs.accepted_feeds)
    if problem is not None:
        return refuse("evidence_stale", problem)
    # Ruling 4: the quote has no last; the bid is the stricter side.
    if inputs.quote.bid < rule.min_price:
        return refuse("price", f"{inputs.quote.feed_type} bid {inputs.quote.bid} is below {rule.min_price}")
    volume = inputs.volume
    if volume is None or not volume.is_current(now):
        return refuse("evidence_stale", "; ".join(inputs.missing) or "20-session volume is not the latest window")
    median, floor = volume.median_dollar_volume, effective_dollar_volume_floor(rule)
    if median < floor:
        return refuse("dollar_volume", f"20-session median dollar volume {median:,.0f} is below {floor:,.0f} "
                                       f"(rule {rule.min_median_dollar_volume:,.0f}, "
                                       f"SP1 {MIN_MEDIAN_DOLLAR_VOLUME:,.0f})")
    cap = rule.max_order_share_of_dollar_volume * median
    if inputs.order_notional is not None and inputs.order_notional > cap:
        return refuse("liquidity", f"order notional {inputs.order_notional:,.2f} is above "
                                   f"{rule.max_order_share_of_dollar_volume:.2%} of {median:,.0f}")
    try:
        denied = inputs.filter_refusal(inputs.contract, float(inputs.quote.ask))
    except Exception as ex:
        denied = f"trading filter unavailable: {type(ex).__name__}"
    if denied:
        return refuse("trading_filter", denied)
    return ScopeVerdict(None, "in scope", evidence)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, dt.datetime) else None


def _evidence_json(inputs: ScopeInputs) -> dict:
    contract, quote, volume = inputs.contract, inputs.quote, inputs.volume
    return {
        "contract": None if contract is None else {
            "conid": contract.conid, "symbol": contract.symbol, "sec_type": contract.sec_type,
            "currency": contract.currency, "primary_exchange": contract.primary_exchange,
            "stock_type": contract.stock_type, "fetched_at": _iso(contract.fetched_at)},
        "quote": None if quote is None else {
            "bid": getattr(quote, "bid", None), "ask": getattr(quote, "ask", None),
            "market_timestamp": _iso(getattr(quote, "market_timestamp", None)),
            "feed_type": getattr(quote, "feed_type", None), "session_state": getattr(quote, "session_state", None)},
        "accepted_feeds": sorted(inputs.accepted_feeds),
        "volume": None if volume is None else volume.to_json(),
        "order_notional": inputs.order_notional,
        "missing": list(inputs.missing),
    }


def trading_filter_refusal(load_filter: Callable[[], Any]) -> FilterRefusal:
    """``trading_filters.yaml`` on the IB identity: exact symbol, primary listing and security type."""

    def refusal(contract: ContractEvidence, price: float) -> Optional[str]:
        if not contract.symbol.strip() or not contract.primary_exchange.strip():
            # TradingFilter skips its rules on a blank field: that would read as "allowed".
            return "the instrument has no symbol or listing exchange"
        allowed, reason = load_filter().is_allowed(symbol=contract.symbol, exchange=contract.primary_exchange,
                                                   sec_type=contract.sec_type, price=price)
        return None if allowed else (reason or "denied by trading_filters.yaml")
    return refusal


class ScopeCheckStore:
    """One row per ``(command_id, phase)``; a replay keeps the first verdict."""

    def __init__(self, db: Any, now: Callable[[], dt.datetime]):
        self._db = db
        self._now = now

    def record(self, *, command_id: str, phase: str, deployment_digest: str, conid: int,
               verdict: ScopeVerdict) -> dict:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}")
        self._db.execute(
            "INSERT INTO discretionary_scope_checks (command_id, phase, deployment_digest, conid, passed, part, "
            "reason, evidence_json, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (command_id, phase) DO NOTHING",
            [command_id, phase, deployment_digest, int(conid), verdict.passed, verdict.part, verdict.reason,
             json.dumps(verdict.evidence, sort_keys=True, default=str), self._now()])
        return self.detail(command_id, phase)

    def detail(self, command_id: str, phase: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT part, reason FROM discretionary_scope_checks WHERE command_id = ? AND phase = ?",
            [command_id, phase], fetch="one")
        if row is None:
            return None
        return {"part": row[0], "reason": row[1], "phase": phase, "check_id": f"{phase}:{command_id}"}


# ---------------------------------------------------------------------------
# Admission (ruling 8): fresh IB details, a fresh quote, the volume window
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscretionaryScopeEvidence:
    """What admission proved; carried on the approval so dispatch re-checks without an IB or history read."""
    deployment_digest: str
    rule: Any
    contract: ContractEvidence
    volume: TwentySessionVolume


@dataclass(frozen=True)
class AdmissionScope:
    evidence: DiscretionaryScopeEvidence
    attested_notional: float


class ScopeRefused(Exception):
    def __init__(self, detail: dict):
        super().__init__(OUT_OF_DISCRETIONARY_SCOPE)
        self.detail = detail


class DiscretionaryScopeService:
    """Ruling 8 at admission: fresh IB contract details, a fresh quote from the quote authority and the volume
    source. ``quotes`` and ``accepted_feeds`` are the command stack's own (Ruling 4, PR #76)."""

    def __init__(self, *, contracts: Any, volumes: Any, quotes: Any, accepted_feeds: frozenset[str],
                 filter_refusal: FilterRefusal, checks: ScopeCheckStore, now: Callable[[], dt.datetime]):
        self._contracts, self._volumes, self._quotes = contracts, volumes, quotes
        self._accepted_feeds = frozenset(accepted_feeds)
        self._filter_refusal, self._checks, self._now = filter_refusal, checks, now

    @property
    def checks(self) -> ScopeCheckStore:
        return self._checks

    def check_admission(self, *, command_id: str, digest: str, deployment: Any, conid: int) -> AdmissionScope:
        missing: list[str] = []
        # Order matters: the contract read remembers the conid in the universe, which the quote read needs.
        contract = self._fetch(lambda: self._contracts.by_conid(conid), missing)
        volume = None if contract is None else self._fetch(
            lambda: self._volumes.twenty_sessions(conid, contract.symbol), missing)
        quote = self._fetch(lambda: self._quotes.executable_quote(conid, side="BUY"), missing)
        rule = deployment.scope_rule
        verdict = evaluate_scope(rule, ScopeInputs(contract, quote, volume, None, self._filter_refusal,
                                                   self._accepted_feeds, tuple(missing)), self._now())
        detail = self._checks.record(command_id=command_id, phase="admission", deployment_digest=digest,
                                     conid=conid, verdict=verdict)
        if not verdict.passed:
            raise ScopeRefused(detail)
        cap = rule.max_order_share_of_dollar_volume * volume.median_dollar_volume
        # Ruling 7: SP1 adds LIVE_NOTIONAL_TOLERANCE to the attested notional; this keeps the bound at the cap.
        return AdmissionScope(DiscretionaryScopeEvidence(digest, rule, contract, volume),
                              cap / (1.0 + LIVE_NOTIONAL_TOLERANCE))

    def refuse_size(self, *, command_id: str, scope: AdmissionScope, conid: int, reason: str) -> dict:
        verdict = ScopeVerdict("liquidity", reason, {"volume": scope.evidence.volume.to_json()})
        return self._checks.record(command_id=command_id, phase="sizing",
                                   deployment_digest=scope.evidence.deployment_digest, conid=conid, verdict=verdict)

    @staticmethod
    def _fetch(read: Callable[[], Any], missing: list[str]) -> Any:
        try:
            return read()
        except ScopeEvidenceUnavailable as ex:
            missing.append(ex.reason)
        except Exception as ex:
            missing.append(f"evidence read failed: {type(ex).__name__}")
        return None


# ---------------------------------------------------------------------------
# Dispatch (rulings 8-10): inside the saga's entry lock, no IB call and no history read
# ---------------------------------------------------------------------------

EntryGate = Callable[[Any, Any, Any, dt.datetime], Optional[str]]


def compose_entry_gates(*gates: EntryGate) -> EntryGate:
    """The first refusal code wins; every gate runs only while the earlier ones pass."""

    def gate(request: Any, approval: Any, quote: Any, now: dt.datetime) -> Optional[str]:
        for each in gates:
            code = each(request, approval, quote, now)
            if code:
                return code
        return None
    return gate


def discretionary_scope_gate(*, kind_of: Callable[[str], str], checks: ScopeCheckStore,
                             filter_refusal: FilterRefusal, accepted_feeds: frozenset[str]) -> EntryGate:
    """Ruling 8 at dispatch: price, liquidity and the filter on the guard's fresh quote; the rest from
    the admission evidence on the approval. The verdict is recorded before the code is returned (ruling 9)."""
    accepted = frozenset(accepted_feeds)

    def gate(request: Any, approval: Any, quote: Any, now: dt.datetime) -> Optional[str]:
        if getattr(request, "action", None) != AI_PAPER_ACTION:
            return None
        digest = (getattr(request, "body", None) or {}).get("deployment_digest")
        evidence = getattr(approval, "discretionary_scope", None)
        if evidence is None:
            # Ruling 10: a discretionary approval that lost its evidence fails closed.
            if digest is None or kind_of(digest) != DISCRETIONARY_KIND:
                return None
            verdict = ScopeVerdict("evidence_stale", "the approval carries no scope evidence", {})
        elif evidence.deployment_digest != digest:
            verdict = ScopeVerdict("evidence_stale", "the scope evidence names another deployment", {})
        else:
            verdict = evaluate_scope(evidence.rule, _dispatch_inputs(evidence, approval, quote, now,
                                                                     filter_refusal, accepted), now)
        checks.record(command_id=request.command_id, phase="dispatch", deployment_digest=str(digest),
                      conid=int(approval.conid), verdict=verdict)
        return None if verdict.passed else OUT_OF_DISCRETIONARY_SCOPE

    return gate


def _dispatch_inputs(evidence: DiscretionaryScopeEvidence, approval: Any, quote: Any, now: dt.datetime,
                     filter_refusal: FilterRefusal, accepted_feeds: frozenset[str]) -> ScopeInputs:
    age = now - evidence.contract.fetched_at
    fresh = dt.timedelta(seconds=-MAX_SOURCE_CLOCK_SKEW_SECONDS) <= age <= DISPATCH_EVIDENCE_MAX_AGE
    notional = None
    if quote_problem(quote, now, accepted_feeds) is None:
        # The limit the saga will send on this quote: the order's real notional.
        limit = planned_entry_limit(float(quote.ask), float(quote.bid), AI_ENTRY_POLICY.limit_offset_bps)
        notional = abs(float(approval.quantity)) * limit
    return ScopeInputs(contract=evidence.contract if fresh else None, quote=quote, volume=evidence.volume,
                       order_notional=notional, filter_refusal=filter_refusal, accepted_feeds=accepted_feeds,
                       missing=() if fresh else (f"admission evidence is {age.total_seconds():.0f} s old",))
