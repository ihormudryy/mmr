"""Fresh entry evidence and the code-owned quantity ceiling (SP2 spec 5.3, 8, 10; Plan 6 Rulings 3-6, 16).

The quote comes from the trader's quote authority (get_ai_entry_quote) with the trader's own accepted-feed
set; this side checks the feed against that set and never decides it. The trader revalidates everything.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from trader.ai.ids import canonical_json
from trader.automation.ai_paper_sizing import SizingInputs, max_entry_quantity
from trader.automation.risk_limits import RiskLimits, RiskLimitsError

SP1_ADV_FRACTION = 0.0025                     # trader.automation.liquidity_policy.MAX_ADV_FRACTION (test pins it)
FUTURE_SKEW_SECONDS = 5.0
ENTRY_KINDS = ("strategy", "discretionary")


class EvidenceRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def evidence_digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _positive(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class Quote:
    conid: int
    bid: float
    ask: float
    time: str
    feed: str                                     # the trader's label: live or iex_realtime (owner #74)


def fresh_quote(reply: Any, conid: int, now: dt.datetime, max_age_seconds: int) -> Quote:
    """Ruling 3. The accepted feeds come from the trader's own reply; this side never decides them."""
    if not isinstance(reply, dict) or reply.get("conid") != conid:
        raise EvidenceRefused("QUOTE_UNAVAILABLE", "no entry quote for this conid")
    accepted = reply.get("accepted_feeds")
    if not isinstance(accepted, list) or not accepted or not all(isinstance(f, str) for f in accepted):
        raise EvidenceRefused("QUOTE_UNAVAILABLE", "the trader named no accepted feed")
    quote = reply.get("quote")
    if not isinstance(quote, dict):
        raise EvidenceRefused("QUOTE_UNAVAILABLE", "the trader has no executable quote")
    feed = quote.get("feed")
    if feed not in accepted:
        raise EvidenceRefused("QUOTE_FEED_NOT_ACCEPTED", f"feed {feed!r} is not in {sorted(accepted)}")
    if quote.get("session_state") != "continuous":
        raise EvidenceRefused("QUOTE_NOT_CONTINUOUS", f"session state {quote.get('session_state')!r}")
    bid, ask = quote.get("bid"), quote.get("ask")
    if not (_positive(bid) and _positive(ask)) or ask < bid:
        raise EvidenceRefused("QUOTE_INVALID", "bid and ask must be positive with ask >= bid")
    try:
        stamped = dt.datetime.fromisoformat(quote["market_timestamp"])
    except (TypeError, ValueError):
        raise EvidenceRefused("QUOTE_STALE", "the quote has no readable time") from None
    if stamped.utcoffset() is None:
        raise EvidenceRefused("QUOTE_STALE", "the quote time has no UTC offset")
    age = (now - stamped).total_seconds()
    if age > max_age_seconds or age < -FUTURE_SKEW_SECONDS:
        raise EvidenceRefused("QUOTE_STALE", f"quote age {age:.1f}s")
    return Quote(conid, float(bid), float(ask), stamped.astimezone(dt.timezone.utc).isoformat(), feed)


@dataclass(frozen=True)
class Bracket:
    stop: float
    target: float

    @classmethod
    def around(cls, ask: float, stop_fraction: float, target_fraction: float) -> "Bracket":
        stop, target = round(ask * (1 - stop_fraction), 2), round(ask * (1 + target_fraction), 2)
        if not 0 < stop < ask < target:
            raise EvidenceRefused("BRACKET_INVALID", f"no cent bracket around {ask}")
        return cls(stop, target)


@dataclass(frozen=True)
class PolicyFacts:
    revision: int
    limits: RiskLimits

    @classmethod
    def from_reply(cls, reply: Any) -> "PolicyFacts":
        revision = reply.get("latest_published_revision") if isinstance(reply, dict) else None
        if type(revision) is not int or revision < 1:
            raise EvidenceRefused("NO_ACCEPTED_POLICY", "no published risk policy")
        try:
            limits = RiskLimits.from_json(reply.get("effective") or reply.get("latest_published"))
        except (RiskLimitsError, TypeError, ValueError, KeyError, AttributeError):
            raise EvidenceRefused("POLICY_INVALID", "the policy limits cannot be read") from None
        return cls(revision, limits)


@dataclass(frozen=True)
class DeploymentFacts:
    kind: str
    notional_cap: Optional[float]
    max_order_share: Optional[float]

    @classmethod
    def from_reply(cls, reply: Any, source: "EntrySource") -> "DeploymentFacts":
        """Ruling 6: checked before any model call; the trader's own code when it refuses."""
        if not isinstance(reply, dict):
            raise EvidenceRefused("DEPLOYMENT_INVALID", "no deployment reply")
        if reply.get("error_code") is not None:
            raise EvidenceRefused(str(reply["error_code"]), "the trader refused the deployment")
        if reply.get("kind") != source.kind:
            raise EvidenceRefused("DEPLOYMENT_KIND_MISMATCH", f"expected a {source.kind} deployment")
        deployment = reply.get("deployment")
        if not isinstance(deployment, dict):
            raise EvidenceRefused("DEPLOYMENT_INVALID", "the deployment body is missing")
        if source.kind == "strategy":
            if deployment.get("decider_verdict") != "DEPLOY":
                raise EvidenceRefused("DEPLOYMENT_NOT_DEPLOYABLE", "the deployment was not judged DEPLOY")
            conids = deployment.get("conids")
            if not isinstance(conids, list) or source.conid not in conids:
                raise EvidenceRefused("CONID_NOT_IN_DEPLOYMENT", f"conid {source.conid} is not deployed")
            notional = deployment.get("evidence_order_notional")
            if not _positive(notional):
                raise EvidenceRefused("DEPLOYMENT_INVALID", "evidence_order_notional must be positive")
            return cls("strategy", float(notional), None)
        rule = deployment.get("scope_rule")
        share = rule.get("max_order_share_of_dollar_volume") if isinstance(rule, dict) else None
        if not _positive(share):
            raise EvidenceRefused("DEPLOYMENT_INVALID", "the scope rule has no order share")
        return cls("discretionary", None, float(share))

    def caps(self, *, price: float, median_dollar_volume: Optional[float]) -> tuple[float, Optional[float]]:
        """(notional cap, liquidity shares). A strategy has no ADV on the ai side: only its notional binds."""
        if self.kind == "strategy":
            return self.notional_cap, None
        if not _positive(median_dollar_volume):
            raise EvidenceRefused("EVIDENCE_STALE", "no 20-session median dollar volume")
        return self.max_order_share * median_dollar_volume, SP1_ADV_FRACTION * median_dollar_volume / price


@dataclass(frozen=True)
class AccountFacts:
    net_liquidation: float
    positions: Mapping[int, tuple[float, float]]           # conid -> (position, average_cost)

    @classmethod
    def from_replies(cls, account: Any, positions: Any) -> "AccountFacts":
        row = account.get("NetLiquidation") if isinstance(account, dict) else None
        try:
            value = float(row["value"]) if isinstance(row, dict) and row.get("currency") == "USD" else math.nan
        except (TypeError, ValueError):
            value = math.nan
        if not (math.isfinite(value) and value > 0):
            raise EvidenceRefused("ACCOUNT_UNAVAILABLE", "no positive USD NetLiquidation")
        rows = positions.get("positions") if isinstance(positions, dict) else None
        if not isinstance(rows, list):
            raise EvidenceRefused("ACCOUNT_UNAVAILABLE", "positions are unreadable")
        held: dict[int, tuple[float, float]] = {}
        try:
            for position in rows:
                quantity = float(position["position"])
                if quantity != 0:
                    held[int(position["instrument_id"])] = (quantity, float(position["average_cost"]))
        except (TypeError, ValueError, KeyError):
            raise EvidenceRefused("ACCOUNT_UNAVAILABLE", "a position row is unreadable") from None
        return cls(value, held)


def estimate_entry_ceiling(limits: RiskLimits, account: AccountFacts, *, conid: int, price: float, stop: float,
                           notional_cap: float, liquidity_max_shares: Optional[float]) -> int:
    """Ruling 4: SP1's own sizing on what the ai side can read. An upper bound; the trader sizes exactly."""
    held_quantity, _ = account.positions.get(conid, (0.0, 0.0))
    return max_entry_quantity(limits, SizingInputs(
        equity=account.net_liquidation, price=price, stop_price=stop,
        existing_position_value=abs(held_quantity) * price,
        current_gross_notional=sum(abs(q) * cost for q, cost in account.positions.values()),
        liquidity_max_shares=notional_cap / price if liquidity_max_shares is None else liquidity_max_shares,
        notional_cap=notional_cap))


_SOURCE_KEYS = frozenset({"kind", "conid", "deployment_digest", "stop_fraction", "target_fraction",
                          "median_dollar_volume", "facts", "untrusted"})


@dataclass(frozen=True)
class EntrySource:
    """What is being judged: built by code from a signal or a picked candidate, recorded for replay."""
    kind: str
    conid: int
    deployment_digest: str
    stop_fraction: float
    target_fraction: float
    median_dollar_volume: Optional[float]
    facts: Mapping[str, Any]
    untrusted: tuple[tuple[str, str], ...]

    @classmethod
    def for_signal(cls, opportunity: Any, strategy: Any) -> "EntrySource":
        facts = {"source": "strategy_signal", "strategy": opportunity.strategy_name, "conid": opportunity.conid,
                 "probability": opportunity.probability, "signal_time": opportunity.signal_time.isoformat()}
        return cls("strategy", opportunity.conid, strategy.deployment_digest, strategy.stop_fraction,
                   strategy.target_fraction, None, facts, ())

    @classmethod
    def for_candidate(cls, chosen: Any, decisions: Any, cycle_id: str) -> "EntrySource":
        """A self-found idea: the orchestrator's thesis and the news reach Jev as untrusted text only."""
        c = chosen.candidate
        facts = {"source": "self_found", "cycle_id": cycle_id, "symbol": c.symbol, "conid": c.conid,
                 "origins": list(c.origins), "change_pct": c.change_pct, "delayed_price": c.price}
        untrusted = (("orchestrator_thesis", chosen.thesis),) + tuple(
            ("news", f"{line.published} {line.source}: {line.title}. {line.summary}") for line in c.news)
        bracket = decisions.self_found_bracket
        return cls("discretionary", c.conid, decisions.discretionary_deployment_digest, bracket.stop_fraction,
                   bracket.target_fraction, c.median_dollar_volume, facts, untrusted)

    def to_json(self) -> dict:
        return {"kind": self.kind, "conid": self.conid, "deployment_digest": self.deployment_digest,
                "stop_fraction": self.stop_fraction, "target_fraction": self.target_fraction,
                "median_dollar_volume": self.median_dollar_volume, "facts": dict(self.facts),
                "untrusted": [[label, text] for label, text in self.untrusted]}

    @classmethod
    def from_json(cls, value: Any) -> "EntrySource":
        if not isinstance(value, dict) or set(value) != _SOURCE_KEYS:
            raise ValueError("an entry source has exactly its eight keys")
        if value["kind"] not in ENTRY_KINDS or type(value["conid"]) is not int or value["conid"] <= 0:
            raise ValueError("an entry source names its kind and a positive conid")
        if not isinstance(value["facts"], dict) or not isinstance(value["untrusted"], list):
            raise ValueError("facts must be a mapping and untrusted a list")
        untrusted = []
        for pair in value["untrusted"]:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
                raise ValueError("untrusted items are [label, text] pairs")
            untrusted.append((pair[0], pair[1]))
        return cls(value["kind"], value["conid"], value["deployment_digest"], value["stop_fraction"],
                   value["target_fraction"], value["median_dollar_volume"], dict(value["facts"]), tuple(untrusted))


@dataclass(frozen=True)
class EntryEvidence:
    conid: int
    read_at: dt.datetime
    quote: Quote
    policy_revision: int
    deployment_digest: str
    reference_price: float
    stop_price: float
    target_price: float
    ceiling: int
    digest: str


@dataclass(frozen=True)
class PricedEntry:
    """What a baseline needs: the fresh quote and the bracket around its ask (no sizing, no model)."""
    conid: int
    read_at: dt.datetime
    quote: Quote
    stop_price: float
    target_price: float

    @property
    def reference_price(self) -> float:
        return self.quote.ask


async def price_entry(tools: Any, source: EntrySource, *, quote_max_age_seconds: int) -> PricedEntry:
    """The first read of every entry (replay depends on the order). Raises EvidenceRefused or ToolUnavailable."""
    read_at = tools.clock.now()
    quote = fresh_quote(await tools.read("quote", {"conid": source.conid}), source.conid, read_at,
                        quote_max_age_seconds)
    bracket = Bracket.around(quote.ask, source.stop_fraction, source.target_fraction)
    return PricedEntry(source.conid, read_at, quote, bracket.stop, bracket.target)


async def gather_entry_evidence(tools: Any, source: EntrySource, *, quote_max_age_seconds: int) -> EntryEvidence:
    priced = await price_entry(tools, source, quote_max_age_seconds=quote_max_age_seconds)
    return await complete_entry_evidence(tools, source, priced)


async def complete_entry_evidence(tools: Any, source: EntrySource, priced: PricedEntry) -> EntryEvidence:
    """The reads after the quote, in a fixed order. Raises EvidenceRefused or ToolUnavailable."""
    read_at, quote = priced.read_at, priced.quote
    bracket = Bracket(priced.stop_price, priced.target_price)
    policy = PolicyFacts.from_reply(await tools.read("policy", {}))
    deployment = DeploymentFacts.from_reply(await tools.read("deployment", {"digest": source.deployment_digest}),
                                            source)
    account = AccountFacts.from_replies(await tools.read("account", {}), await tools.read("positions", {}))
    notional_cap, liquidity_shares = deployment.caps(price=quote.ask, median_dollar_volume=source.median_dollar_volume)
    ceiling = estimate_entry_ceiling(policy.limits, account, conid=source.conid, price=quote.ask, stop=bracket.stop,
                                     notional_cap=notional_cap, liquidity_max_shares=liquidity_shares)
    if ceiling < 1:
        raise EvidenceRefused("QUANTITY_BELOW_ONE_SHARE", "the limits leave less than one share")
    body = {"v": "entry_evidence.v1", "conid": source.conid, "quote": dataclasses.asdict(quote),
            "policy_revision": policy.revision, "deployment_digest": source.deployment_digest,
            "stop": bracket.stop, "target": bracket.target, "ceiling": ceiling, "equity": account.net_liquidation}
    return EntryEvidence(source.conid, read_at, quote, policy.revision, source.deployment_digest, quote.ask,
                         bracket.stop, bracket.target, ceiling, evidence_digest(body))
