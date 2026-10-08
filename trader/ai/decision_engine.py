"""The paper decision engine (SP2 spec 3, 5.5, 7, 9; Plan 6 Rulings 1-16).

Jev judges every ENTER; the orchestrator only picks from code-built menus; strategy SELLs go to SP1's safe
close without a model. Any model failure is a recorded refusal, never a TAKE and never a close.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional, Union

from trader.ai.baselines import (RANKING_UNAVAILABLE, fixed_rule, follow_signal, incomplete, incomplete_reason_for,
                                 matched_entry, no_trade, pick_fixed_rule)
from trader.ai.discovery_client import DiscoveryClient, DiscoveryRead
from trader.ai.config import StrategyBracket
from trader.ai.engine import (
    EngineResult, EntryCycleContext, ModelWork, OwnedPosition, PositionCycleContext, ProposedDecision, SignalContext,
    owned_positions_from_trips,
)
from trader.ai.evidence import (EntryEvidence, EntrySource, EvidenceRefused, PricedEntry, complete_entry_evidence,
                                evidence_digest, price_entry)
from trader.ai.gateway import CallFailed, CallRefused
from trader.ai.ids import derive_decision_id
from trader.ai.model_client import REJECTED, ModelRequest
from trader.ai.roles import (
    ChosenClose, PositionChoice, close_messages, entry_messages, jev_messages, parse_close_picks,
    parse_entry_picks, parse_jev,
)
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.schedule import ET
from trader.ai.tools import LiveTools, ToolUnavailable
from trader.ai.untrusted import OutputRefusal
from trader.automation.calendar_policy import XNYSCalendarPolicy

logger = logging.getLogger(__name__)
CONFIG_REFUSALS = frozenset({"ROLE_UNKNOWN", "PRICE_UNAVAILABLE", "OUTPUT_LIMIT_ABOVE_ROLE"})
FLATTEN_ET = dt.time(15, 45)
NEVER_SENT_STATES = frozenset({"ABANDONED", "NOT_ADMITTED", "FAILED"})
ENTRY_DONE_STATUSES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})   # ib_async DoneStates
WORKING, FILLED, ENDED_UNFILLED = "WORKING", "FILLED", "ENDED_UNFILLED"
PARTLY_FILLED = "PARTLY_FILLED"        # a live entry with some shares already bought


class RoleHealth:
    """Ruling 10: a config-class failure takes a role down for a while; a success brings it back."""

    def __init__(self, clock: Any, recheck_seconds: int):
        self._clock, self._recheck = clock, dt.timedelta(seconds=recheck_seconds)
        self._down_until: dict[str, dt.datetime] = {}

    def healthy(self, role: str) -> bool:
        until = self._down_until.get(role)
        return until is None or self._clock.now() >= until

    def observe(self, role: str, error: Exception) -> None:
        config_refusal = isinstance(error, CallRefused) and error.code in CONFIG_REFUSALS
        rejected = isinstance(error, CallFailed) and error.outcome == REJECTED
        if config_refusal or rejected:
            logger.error("ai role %s is down for %ss after %s", role, self._recheck.seconds, error.code)
            self._down_until[role] = self._clock.now() + self._recheck

    def ok(self, role: str) -> None:
        self._down_until.pop(role, None)


@dataclass(frozen=True)
class JudgeSettings:
    quote_max_age_seconds: int
    news_chars: int
    max_entries: int
    max_output_tokens: dict
    health: Optional[RoleHealth]

    @classmethod
    def from_config(cls, config: Any, health: Optional[RoleHealth]) -> "JudgeSettings":
        d = config.decisions
        return cls(d.quote_max_age_seconds, d.news_chars_per_item, d.max_entries_per_cycle,
                   {role: config.role(role).max_output_tokens for role in ("orchestrator", "jev")}, health)


async def ask_model(tools: Any, role: str, messages: tuple, settings: JudgeSettings) -> Union[str, OutputRefusal]:
    """One model call. Any failure is a refusal code; it never authorizes anything (spec 9)."""
    healthy = await tools.given(f"health:{role}", True if settings.health is None else settings.health.healthy(role))
    if not healthy:
        return OutputRefusal(f"{role.upper()}_UNHEALTHY")
    request = ModelRequest(request_key=tools.request_key(role, 1), messages=messages,
                           max_output_tokens=settings.max_output_tokens[role])
    try:
        result = await tools.gateway.call(role, request, tools.deadline)
    except CallRefused as exc:
        if settings.health is not None:
            settings.health.observe(role, exc)
        return OutputRefusal(f"MODEL_REFUSED_{exc.code}")
    except CallFailed as exc:
        if settings.health is not None:
            settings.health.observe(role, exc)
        return OutputRefusal(f"MODEL_FAILED_{exc.outcome}")
    if settings.health is not None:
        settings.health.ok(role)
    return result.response.text


@dataclass(frozen=True)
class Judgment:
    outcome: str                      # TAKE | SKIP | REDUCE | REFUSED
    code: str
    evidence: Optional[EntryEvidence]
    quantity: Optional[int] = None
    detail: str = ""
    priced: Optional[PricedEntry] = None   # the quote and bracket, kept even when a later read failed

    @property
    def enters(self) -> bool:
        return self.outcome in ("TAKE", "REDUCE")

    def summary(self) -> dict:
        return {"outcome": self.outcome, "code": self.code, "quantity": self.quantity,
                "ceiling": None if self.evidence is None else self.evidence.ceiling,
                "evidence_digest": None if self.evidence is None else self.evidence.digest}


async def judge_entry(tools: Any, source_json: Optional[dict], settings: JudgeSettings) -> Judgment:
    """One replayable Jev decision: source, evidence, ceiling, prompt, verdict (Rulings 1-4, 9)."""
    source = EntrySource.from_json(await tools.given("source", source_json))
    try:
        priced = await price_entry(tools, source, quote_max_age_seconds=settings.quote_max_age_seconds)
    except (ToolUnavailable, EvidenceRefused) as exc:
        return Judgment("REFUSED", exc.code, None)                   # no quote: the baseline goes incomplete
    try:
        evidence = await complete_entry_evidence(tools, source, priced)
    except (ToolUnavailable, EvidenceRefused) as exc:
        return Judgment("REFUSED", exc.code, None, priced=priced)
    facts = {**source.facts, "side": "BUY", "reference_ask": evidence.reference_price, "stop": evidence.stop_price,
             "target": evidence.target_price, "quantity_ceiling": evidence.ceiling,
             "quote_time": evidence.quote.time, "quote_feed": evidence.quote.feed, "evidence_digest": evidence.digest}
    text = await ask_model(tools, "jev", jev_messages(facts, source.untrusted, news_chars=settings.news_chars),
                           settings)
    if isinstance(text, OutputRefusal):
        return Judgment("REFUSED", text.code, evidence, detail=text.detail, priced=priced)
    verdict = parse_jev(text, ceiling=evidence.ceiling)
    if isinstance(verdict, OutputRefusal):
        return Judgment("REFUSED", verdict.code, evidence, detail=verdict.detail, priced=priced)
    return Judgment(verdict.verdict, f"JEV_{verdict.verdict}", evidence, verdict.quantity, verdict.reason[:300],
                    priced=priced)


def enter_decision(action_key: str, judgment: Judgment, binding: Optional[Any] = None) -> ProposedDecision:
    """``binding`` is the strategy signal of an AI-deployment instance; self-found entries pass none."""
    evidence = judgment.evidence
    return ProposedDecision(action_key=action_key, action="ENTER", conid=evidence.conid, side="BUY", decider="jev",
                            evidence_digest=evidence.digest, deployment_digest=evidence.deployment_digest,
                            policy_revision=evidence.policy_revision, stop_price=evidence.stop_price,
                            target_price=evidence.target_price, quantity=judgment.quantity,      # TAKE: trader sizes
                            deployment_version=None if binding is None else binding.deployment_version,
                            source_digest=None if binding is None else binding.source_digest)


def close_decision(chosen: ChosenClose) -> ProposedDecision:
    position = chosen.choice.position
    partial = chosen.action == "PARTIAL_CLOSE"
    digest = evidence_digest({"v": "close.v1", "round_trip_id": position.round_trip_id, "conid": position.conid,
                              "open_quantity": position.open_quantity, "bid": chosen.choice.bid,
                              "ask": chosen.choice.ask, "action": chosen.action, "quantity": chosen.quantity})
    return ProposedDecision(action_key=f"{'partial_close' if partial else 'close'}:{position.conid}",
                            action=chosen.action, conid=position.conid, side="SELL", decider="orchestrator",
                            evidence_digest=digest, quantity=chosen.quantity if partial else None)


async def record_ruling(store: Any, *, unit_key: str, step: str, action_key: Optional[str], outcome: str, code: str,
                        quantity: Optional[int] = None, ceiling: Optional[int] = None,
                        evidence_digest: Optional[str] = None, detail: str = "", now: dt.datetime) -> None:
    """Ruling 15: one row per model step, written live only (replay writes nothing)."""
    def work(conn: Any) -> None:
        count = conn.execute("SELECT COUNT(*) FROM ai_rulings WHERE unit_key = ? AND step = ?",
                             [unit_key, step]).fetchone()[0]
        conn.execute(
            "INSERT INTO ai_rulings (ruling_id, unit_key, step, action_key, outcome, code, quantity, ceiling, "
            "evidence_digest, detail, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [f"{unit_key}/{step}#{count + 1}", unit_key, step, action_key, outcome, code, quantity, ceiling,
             evidence_digest, (detail or "")[:500], now])
    await store.atransaction(work)


def _minutes_to_flatten(now: dt.datetime) -> int:
    """A prompt hint only: SP1's session controller owns the real flatten, early closes included."""
    local = now.astimezone(ET)
    flatten = dt.datetime.combine(local.date(), FLATTEN_ET, tzinfo=ET)
    return max(0, math.floor((flatten - local).total_seconds() / 60))


class PaperDecisionEngine:
    def __init__(self, *, config: Any, reads: Any, recorder: Any, store: Any, clock: Any):
        self._config, self._cfg = config, config.decisions
        self._reads, self._recorder, self._store, self._clock = reads, recorder, store, clock
        self._health = RoleHealth(clock, self._cfg.role_recheck_seconds)
        self._calendar = XNYSCalendarPolicy()
        self._settings = JudgeSettings.from_config(config, self._health)
        digest = self._cfg.discretionary_deployment_digest
        self._discovery = None if digest is None else DiscoveryClient(
            store=store, settings=self._cfg.discovery, deployment_digest=digest, clock=clock)

    def _tools(self, work: ModelWork) -> LiveTools:
        return LiveTools(unit_key=work.context_key, reads=self._reads, recorder=self._recorder, clock=self._clock,
                         gateway=work.gateway, deadline=work.deadline)

    async def _judge(self, parent: ModelWork, action_key: str, source: EntrySource) -> Judgment:
        work = await parent.for_action(action_key)
        tools = self._tools(work)
        try:
            judgment = await judge_entry(tools, source.to_json(), self._settings)
        finally:
            await tools.finish(self._config.digest())
        await record_ruling(self._store, unit_key=work.context_key, step="jev", action_key=action_key,
                            outcome=judgment.outcome, code=judgment.code, quantity=judgment.quantity,
                            ceiling=None if judgment.evidence is None else judgment.evidence.ceiling,
                            evidence_digest=None if judgment.evidence is None else judgment.evidence.digest,
                            detail=judgment.detail, now=self._clock.now())
        return judgment

    # -- strategy signals --------------------------------------------------------------------------
    async def on_entry_signal(self, ctx: SignalContext) -> EngineResult:
        opportunity = ctx.opportunity
        if opportunity.deployment_version is None:          # the trader refuses it: DEPLOYMENT_VERSION_REQUIRED
            return EngineResult(note="STRATEGY_NOT_BOUND")
        strategy = self._bound_bracket(opportunity)
        action_key = f"enter:{opportunity.conid}"
        judgment = await self._judge(ctx.work, action_key, EntrySource.for_signal(opportunity, strategy))
        if judgment.priced is None:                                 # no usable quote: sent incomplete, never invented
            return EngineResult(baselines=(incomplete(
                "follow_signal.v1", "strategy_signal", opportunity.opportunity_id, ctx.now, conid=opportunity.conid,
                reason=incomplete_reason_for(judgment.code), deployment_digest=strategy.deployment_digest),),
                note=judgment.code)
        follow = follow_signal(opportunity, judgment.priced, strategy.deployment_digest)
        if not judgment.enters:
            return EngineResult(baselines=(follow,), note=judgment.code)
        return EngineResult(decisions=(enter_decision(action_key, judgment, binding=opportunity),),
                            baselines=(dataclasses.replace(follow, linked_action_key=action_key),), note=judgment.code)

    def _bound_bracket(self, opportunity: Any) -> StrategyBracket:
        """An AI-deployment instance: its own base digest; the trader checks version, cap and source."""
        return StrategyBracket(deployment_digest=opportunity.deployment_digest,
                               stop_fraction=self._cfg.ai_deployments.stop_fraction,
                               target_fraction=self._cfg.ai_deployments.target_fraction)

    async def on_exit_signal(self, ctx: SignalContext) -> EngineResult:
        opportunity = ctx.opportunity
        trips = await self._trips(ctx.experiment.experiment_id)
        if trips is None:                                  # unknown ownership proves nothing held (PR #86 4212667433)
            return EngineResult(note="EXIT_WAITING_FOR_TRIPS", wait_until=self._wait_backstop(ctx.now))
        here = [p for p in owned_positions_from_trips(trips) if p.conid == opportunity.conid]
        versions = await self._entry_versions(tuple(p.decision_id for p in here if p.decision_id))
        mine = [p for p in here if versions.get(p.decision_id) == opportunity.deployment_version]
        if not mine:
            return await self._exit_before_any_fill(ctx, trips)
        if len(mine) < len(here):
            logger.warning("exit %s: conid %s is also held by another deployment version; no CLOSE",
                           opportunity.opportunity_id, opportunity.conid)
            return EngineResult(note="EXIT_CONID_SHARED")
        rival = await self._other_version_entry_state(ctx, trips)
        if rival is not None:                              # a conid-wide CLOSE would cancel or flatten that entry
            return rival
        action_key = await self._close_action_key(opportunity)
        digest = evidence_digest({"v": "exit_signal.v1", "opportunity_id": opportunity.opportunity_id,
                                  "conid": opportunity.conid, "signal_time": opportunity.signal_time.isoformat(),
                                  "action_key": action_key})
        close = ProposedDecision(action_key=action_key, action="CLOSE", conid=opportunity.conid,
                                 side="SELL", decider="strategy", evidence_digest=digest)
        return EngineResult(decisions=(close,), note="EXIT_SIGNAL")

    async def _other_version_entry_state(self, ctx: SignalContext, trips: dict) -> Optional[EngineResult]:
        """An entry of another version on this conid that is working, or filled without a trip yet, shares the
        conid: the CLOSE would reach it (ruling 21). A rival that is only working may still end unfilled, so the exit
        waits; one with fills is final. None when no such entry exists."""
        conid = ctx.opportunity.conid
        projected = {trip.get("decision_id") for trip in trips["trips"]}
        rivals = tuple(decision_id for decision_id in await self._fillable_entries(
            conid, ctx.now, ctx.opportunity.deployment_version, other_versions=True) if decision_id not in projected)
        if not rivals:
            return None
        states = await self._entry_states(conid, rivals, separate_partial_fills=True)
        if states is None or WORKING in states.values():      # unreadable or still working: that may end unfilled
            return EngineResult(note="EXIT_WAITING_FOR_ENTRY", wait_until=self._wait_backstop(ctx.now))
        if not any(state in (FILLED, PARTLY_FILLED) for state in states.values()):
            return None
        logger.warning("exit %s: conid %s has another deployment version's fills in flight; no CLOSE",
                       ctx.opportunity.opportunity_id, conid)
        return EngineResult(note="EXIT_CONID_SHARED")

    async def _entry_versions(self, decision_ids: tuple[str, ...]) -> dict[str, Optional[str]]:
        """Ruling 21: the version each trip's ENTER was bound to, from the bodies this controller sent."""
        if not decision_ids:
            return {}
        marks = ", ".join("?" for _ in decision_ids)
        rows = await self._store.aquery(f"SELECT decision_id, body_json FROM ai_submissions "
                                        f"WHERE action = 'ENTER' AND decision_id IN ({marks})", list(decision_ids))
        return {decision_id: json.loads(body_json).get("deployment_version") for decision_id, body_json in rows}

    async def _close_action_key(self, opportunity: Any) -> str:
        """``close:<conid>``, then ``close:<conid>:r<n>`` for the n-th attempt after a refused one: every
        attempt keeps its own stable decision id (Plan 5 Ruling 11)."""
        earlier = (await self._store.aquery(
            "SELECT COUNT(*) FROM ai_submissions WHERE source_id = ? AND action = 'CLOSE'",
            [opportunity.opportunity_id], fetch="one"))[0]
        return f"close:{opportunity.conid}" if earlier == 0 else f"close:{opportunity.conid}:r{earlier + 1}"

    async def _exit_before_any_fill(self, ctx: SignalContext, trips: dict) -> EngineResult:
        """Nothing is held. While any of our entries in this conid may still fill, the exit waits (never a close
        for shares not held). It ends only on broker proof: every such entry ended with zero fill, or its fill
        reached a trip (held: closed above; already closed: nothing to do). No clock deadline ends it here; the
        controller's backstop after ``wait_until`` is a loud incident, never NOT_HELD (PR #86 4211898491)."""
        conid = ctx.opportunity.conid
        entries = await self._fillable_entries(conid, ctx.now, ctx.opportunity.deployment_version)
        if not entries:
            return EngineResult(note="NOT_HELD")
        states = await self._entry_states(conid, entries)
        if states is None or WORKING in states.values():
            return EngineResult(note="EXIT_WAITING_FOR_ENTRY", wait_until=self._wait_backstop(ctx.now))
        projected = {trip.get("decision_id") for trip in trips["trips"]}
        filled = [decision_id for decision_id, state in states.items() if state == FILLED]
        if any(decision_id not in projected for decision_id in filled):
            return EngineResult(note="EXIT_WAITING_FOR_FILL", wait_until=self._wait_backstop(ctx.now))
        return EngineResult(note="ENTRY_FILLED_AND_CLOSED" if filled else "ENTRY_UNFILLED")

    async def _fillable_entries(self, conid: int, now: dt.datetime, version: Optional[str], *,
                                other_versions: bool = False) -> tuple[str, ...]:
        """Every ENTER of ours in this conid this session that may have become an order (PR #86 4211895474):
        a newer refused entry never hides an older working one. Only ENTERs of ``version`` count, or only those
        of every other version when ``other_versions`` is set (ruling 21)."""
        schedule = self._calendar.resolve(now)
        since = now - dt.timedelta(days=1) if schedule is None else schedule.open_utc - dt.timedelta(hours=1)
        rows = await self._store.aquery(
            "SELECT decision_id, state, receipt_state, error_code, body_json FROM ai_submissions "
            "WHERE action = 'ENTER' AND created_at >= ? ORDER BY created_at", [since])
        fillable = []
        for decision_id, state, receipt_state, error_code, body_json in rows:
            body = json.loads(body_json)
            same_version = body.get("deployment_version") == version
            if body.get("conid") != conid or same_version == other_versions or state in NEVER_SENT_STATES:
                continue
            if state in ("ACCEPTED", "FINAL") and (receipt_state == "REJECTED" or error_code is not None):
                continue                                   # the trader refused it: no order exists
            fillable.append(decision_id)                   # PENDING, SENDING, UNKNOWN or an admitted order
        return tuple(fillable)

    async def _entry_states(self, conid: int, entries: tuple[str, ...], *,
                            separate_partial_fills: bool = False) -> Optional[dict[str, str]]:
        """Each entry's leg from the trader's fenced broker evidence: WORKING (live, partly filled, or not shown
        yet), FILLED (terminal with a fill) or ENDED_UNFILLED (terminal with zero fill). None if unreadable.
        With ``separate_partial_fills`` a live entry that already filled some shares is PARTLY_FILLED."""
        try:
            evidence = await self._reads.call("get_broker_order_evidence", {"conid": conid})
        except (RpcNotSent, RpcOutcomeUnknown, RpcRefused) as exc:
            logger.warning("broker evidence unreadable for a waiting exit (%s); it keeps waiting", exc.code)
            return None
        if not isinstance(evidence, dict) or evidence.get("capture_error") or not isinstance(evidence.get("orders"), list):
            return None
        states = {}
        for decision_id in entries:
            group = f"og-aip-{decision_id}"
            legs = [o for o in evidence["orders"] if o.get("order_group_id") == group and o.get("leg") == "entry"]
            ended = bool(legs) and all(o.get("deleted") or o.get("status") in ENTRY_DONE_STATUSES for o in legs)
            filled = any(float(o.get("filled_quantity") or 0.0) > 0 for o in legs)
            if ended:
                states[decision_id] = FILLED if filled else ENDED_UNFILLED
            else:
                states[decision_id] = PARTLY_FILLED if filled and separate_partial_fills else WORKING
        return states

    def _wait_backstop(self, now: dt.datetime) -> dt.datetime:
        """The session close: every DAY entry order has ended by then. Past it the controller raises an incident."""
        schedule = self._calendar.resolve(now)
        return now + dt.timedelta(hours=1) if schedule is None else schedule.close_utc

    async def _trips(self, experiment_id: str) -> Optional[dict]:
        try:
            reply = await self._reads.call("get_experiment_trips", {"experiment_id": experiment_id})
            owned_positions_from_trips(reply)              # checks the reply's shape
            return reply
        except (RpcNotSent, RpcOutcomeUnknown, RpcRefused, ValueError) as exc:
            logger.warning("owned positions unreadable for an exit signal (%s); the trader proves ownership", exc)
            return None

    # -- entry cycles ------------------------------------------------------------------------------
    async def on_entry_cycle(self, ctx: EntryCycleContext) -> EngineResult:
        if self._discovery is None:
            return EngineResult(note="DISCRETIONARY_NOT_CONFIGURED")
        if not self._health.healthy("orchestrator"):
            return EngineResult(note="ORCHESTRATOR_UNHEALTHY")          # spec 9: no discovery
        cycle_id, tools = ctx.slot.cycle_id, self._tools(ctx.work)
        notes, baselines, chosen = [], [], ()
        try:
            read = await self._discovery.read(tools, cycle_id)
            if not read.ok:
                return EngineResult(note=read.error_code)
            baselines.append(no_trade(cycle_id, ctx.now))            # every cycle with a good discovery read
            await ctx.work.record_baselines(tuple(baselines))        # owed now, whatever the models do later
            if not read.eligible:
                return EngineResult(baselines=tuple(baselines), note="NO_ELIGIBLE_CANDIDATES")
            fixed, code = await self._fixed_rule(tools, read, cycle_id)
            baselines.append(fixed)
            await ctx.work.record_baselines((fixed,))
            notes.extend([code] if code is not None else [])
            if not self._health.healthy("jev"):
                notes.append("JEV_UNHEALTHY")                         # no ENTER could pass its judge
            else:
                picked = await self._pick_entries(tools, read)
                if isinstance(picked, OutputRefusal):
                    notes.append(picked.code)
                else:
                    chosen = picked
        finally:
            await tools.finish(self._config.digest())
        decisions = []
        for choice in chosen:
            action_key = f"enter:{choice.candidate.conid}"
            judgment = await self._judge(ctx.work, action_key, EntrySource.for_candidate(choice, self._cfg, cycle_id))
            notes.append(f"{choice.candidate.symbol}:{judgment.code}")
            if judgment.enters:
                decisions.append(enter_decision(action_key, judgment))
        return EngineResult(tuple(decisions), tuple(baselines), note=",".join(notes) or "NO_PICKS")

    async def _fixed_rule(self, tools: LiveTools, read: DiscoveryRead, cycle_id: str):
        candidate = pick_fixed_rule(read.eligible)
        bracket, digest = self._cfg.fixed_rule, self._cfg.discretionary_deployment_digest
        if candidate is None:                     # eligible, but none has a change_pct: nothing to rank
            return incomplete("fixed_rule.v1", "self_found", cycle_id, tools.clock.now(), conid=None,
                              reason=RANKING_UNAVAILABLE, deployment_digest=digest), "FIXED_RULE_RANKING_UNAVAILABLE"
        source = EntrySource("discretionary", candidate.conid, digest, bracket.stop_fraction,
                             bracket.target_fraction, candidate.median_dollar_volume, {}, ())
        try:
            # The quote and the bracket only: the trader sizes the fixed rule (Plan 2 Ruling 19).
            priced = await price_entry(tools, source, quote_max_age_seconds=self._cfg.quote_max_age_seconds)
        except (ToolUnavailable, EvidenceRefused) as exc:
            return incomplete("fixed_rule.v1", "self_found", cycle_id, tools.clock.now(), conid=candidate.conid,
                              reason=incomplete_reason_for(exc.code), deployment_digest=digest), f"FIXED_RULE_{exc.code}"
        return fixed_rule(cycle_id, priced, digest), None

    async def _pick_entries(self, tools: LiveTools, read: DiscoveryRead):
        messages = entry_messages(read, max_entries=self._cfg.max_entries_per_cycle,
                                  news_chars=self._cfg.news_chars_per_item)
        text = await ask_model(tools, "orchestrator", messages, self._settings)
        picked = text if isinstance(text, OutputRefusal) else parse_entry_picks(
            text, menu={c.ref: c for c in read.eligible}, max_entries=self._cfg.max_entries_per_cycle)
        await self._record_step(tools.unit_key, "entries", picked)
        return picked

    # -- position cycles ---------------------------------------------------------------------------
    async def on_position_cycle(self, ctx: PositionCycleContext) -> EngineResult:
        if not self._health.healthy("orchestrator"):
            return EngineResult(note="ORCHESTRATOR_UNHEALTHY")
        tools = self._tools(ctx.work)
        try:
            menu = await self._position_menu(tools, ctx.positions)
            text = await ask_model(tools, "orchestrator",
                                   close_messages(menu, minutes_to_flatten=_minutes_to_flatten(ctx.now)), self._settings)
        finally:
            await tools.finish(self._config.digest())
        picked = text if isinstance(text, OutputRefusal) else parse_close_picks(text, menu=menu)
        await self._record_step(tools.unit_key, "closes", picked)
        if isinstance(picked, OutputRefusal):
            return EngineResult(note=picked.code)
        decisions, baselines, notes = [], [], []
        for chosen in picked:
            close = close_decision(chosen)
            decisions.append(close)
            # One record per close: its opportunity is the close's own decision id (Plan 2 Ruling 21).
            closed = chosen.quantity if chosen.action == "PARTIAL_CLOSE" else chosen.choice.whole_shares
            matched = matched_entry(chosen.choice.position, chosen.choice.entry_body,
                                    close_decision_id=derive_decision_id(ctx.slot.cycle_id, close.action_key),
                                    closed_quantity=closed)
            baselines.append(matched)                      # every model close is visible in its book
            if matched.incomplete_reason is not None:
                notes.append(f"{chosen.choice.position.symbol}:MATCHED_ENTRY_INCOMPLETE")
        return EngineResult(tuple(decisions), tuple(baselines), note=",".join(notes) or ("CLOSES" if decisions else "HOLD"))

    async def _position_menu(self, tools: LiveTools, positions: tuple[OwnedPosition, ...]) -> dict[str, PositionChoice]:
        menu: dict[str, PositionChoice] = {}
        seen: set[int] = set()
        for position in sorted(positions, key=lambda p: p.conid):
            if position.conid in seen:
                continue
            seen.add(position.conid)
            bid = ask = None
            try:
                quote = (await tools.read("quote", {"conid": position.conid})).get("quote")
            except (ToolUnavailable, AttributeError):
                quote = None
            if isinstance(quote, dict):
                bid, ask = quote.get("bid"), quote.get("ask")
            ref = f"P{len(menu) + 1}"
            menu[ref] = PositionChoice(ref, position, math.floor(position.open_quantity + 1e-9), bid, ask,
                                       await self._entry_body(position))
        return menu

    async def _entry_body(self, position: OwnedPosition) -> Optional[dict]:
        if position.decision_id is None:
            return None
        row = await self._store.aquery("SELECT body_json FROM ai_submissions WHERE decision_id = ? AND action = 'ENTER'",
                                       [position.decision_id], fetch="one")
        return None if row is None else json.loads(row[0])

    async def _record_step(self, unit_key: str, step: str, picked: Any) -> None:
        if isinstance(picked, OutputRefusal):
            outcome, code, detail = "REFUSED", picked.code, picked.detail
        else:
            outcome, code, detail = ("PICKS" if step == "entries" else "CLOSES"), f"{len(picked)}_{step.upper()}", ""
        await record_ruling(self._store, unit_key=unit_key, step=step, action_key=None, outcome=outcome, code=code,
                            detail=detail, now=self._clock.now())
