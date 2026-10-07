"""The DecisionEngine contract between the controller (Plan 5) and the trading judgments (Plan 6).

The engine judges; the controller owns ids, expiry, persistence, submission and
reporting (spec 3, 8). Hooks never submit, never set an id and never raise for
a model failure: they return what they decided plus the baselines (spec 7).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from trader.ai.config import ROLE_NAMES
from trader.ai.ids import ACTION_KEY, derive_decision_id
from trader.ai.schedule import Slot

EXPERIMENT_STATES = ("ARMED", "PAUSED", "KILLED", "STOPPED")
ENTRY_ACTIONS = frozenset({"ENTER"})
EXIT_ACTIONS = frozenset({"CLOSE", "PARTIAL_CLOSE"})
ALLOWED_ACTIONS: Mapping[str, frozenset[str]] = {
    "entry_signal": ENTRY_ACTIONS, "entry_cycle": ENTRY_ACTIONS,
    "exit_signal": EXIT_ACTIONS, "position_cycle": EXIT_ACTIONS,
}
BASELINE_COHORTS: Mapping[str, str] = {
    "follow_signal.v1": "strategy_signal", "fixed_rule.v1": "self_found",
    "no_trade.v1": "self_found", "matched_entry_bracket_exit.v1": "model_close",
}
TRADER_SIZED_BASELINES = frozenset({"follow_signal.v1", "fixed_rule.v1"})     # Plan 2 Ruling 19
MATCHED_ENTRY_BASELINE = "matched_entry_bracket_exit.v1"
INCOMPLETE_REASONS = ("quote_unavailable", "feed_not_accepted", "quote_not_executable", "ranking_unavailable",
                      "budget_refused", "model_failed", "sizing_unavailable")     # Plan 2 Ruling 18
_EXPERIMENT_ID = re.compile(r"^exp-[0-9a-f]{20}$")
_DECIDER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPPORTUNITY_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DECISION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def parse_aware(text: Any, name: str) -> dt.datetime:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    value = dt.datetime.fromisoformat(text)
    if value.utcoffset() is None:
        raise ValueError(f"{name} must carry a UTC offset")
    return value.astimezone(dt.timezone.utc)


def _price(value: Any, name: str) -> None:
    if value is not None and (type(value) is not float or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a positive finite float or None")


def _positive_int(value: Any, name: str) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"{name} must be an integer >= 1 or None")


def _conid(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("conid must be a positive integer")
    return value


@dataclass(frozen=True)
class ExperimentView:
    experiment_id: str
    state: str
    started_at: dt.datetime
    entry_block: Optional[str]

    @classmethod
    def from_reply(cls, reply: Any) -> Optional["ExperimentView"]:
        if not isinstance(reply, dict) or "experiment" not in reply:
            raise ValueError("get_experiment reply has no experiment key")
        experiment = reply["experiment"]
        if experiment is None:
            return None
        experiment_id, state = experiment.get("experiment_id"), experiment.get("state")
        if not isinstance(experiment_id, str) or not _EXPERIMENT_ID.fullmatch(experiment_id):
            raise ValueError("experiment_id must match exp-<20 hex>")
        if state not in EXPERIMENT_STATES:
            raise ValueError(f"unknown experiment state {state!r}")
        block = reply.get("entry_block")
        if block is not None and not isinstance(block, str):
            raise ValueError("entry_block must be a string or null")
        return cls(experiment_id, state, parse_aware(experiment.get("started_at"), "started_at"), block)


@dataclass(frozen=True)
class SignalOpportunity:
    opportunity_id: str              # the trader's source_event_id
    signal_cursor: int
    strategy_name: str
    conid: int
    action: str                      # BUY | SELL
    probability: Optional[float]
    signal_time: dt.datetime
    recorded_at: dt.datetime


@dataclass(frozen=True)
class OwnedPosition:
    round_trip_id: str
    conid: int
    symbol: str
    open_quantity: float
    opened_at: dt.datetime
    decision_id: Optional[str]
    entry_price: Optional[float] = None          # the trip's entry fill average (SP2 Plan 6 Ruling 17)
    entry_quantity: Optional[float] = None       # the trip's opened quantity


def _optional_positive(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number or null")
    return float(value)


def owned_positions_from_trips(reply: Any) -> tuple[OwnedPosition, ...]:
    """Positions the experiment owns: OPEN trips with quantity left (get_experiment_trips, SP1 Plan 5)."""
    if not isinstance(reply, dict) or reply.get("error_code") or not isinstance(reply.get("trips"), list):
        raise ValueError(f"experiment trips unavailable: {reply.get('error_code') if isinstance(reply, dict) else None}")
    owned = []
    for trip in reply["trips"]:
        if trip.get("state") != "OPEN":
            continue
        left = float(trip["opened_quantity"]) - float(trip.get("closed_quantity") or 0.0)
        if left > 0:
            owned.append(OwnedPosition(str(trip["round_trip_id"]), _conid(trip["conid"]), str(trip["symbol"]), left,
                                       parse_aware(trip["opened_at"], "opened_at"), trip.get("decision_id"),
                                       _optional_positive(trip.get("entry_avg_price"), "entry_avg_price"),
                                       _optional_positive(trip.get("opened_quantity"), "opened_quantity")))
    return tuple(owned)


class ModelWork:
    """Identity and deadline of one piece of model work. The engine builds request keys only through it."""

    def __init__(self, *, context_key: str, served_kind: str, served_id: str, source_id: str, experiment_id: str,
                 gateway: Any, deadline: Any, register: Callable[[str, str, str], Awaitable[None]]):
        self.context_key, self.served_kind, self.served_id = context_key, served_kind, served_id
        self.source_id, self.experiment_id = source_id, experiment_id
        self.gateway, self.deadline = gateway, deadline
        self._register = register

    def request_key(self, role: str, call_seq: int) -> str:
        if role not in ROLE_NAMES or type(call_seq) is not int or call_seq < 1:
            raise ValueError("request keys need a known role and a call number >= 1")
        return f"{self.context_key}/{role}/{call_seq}"            # Plan 4 Ruling 10

    async def for_action(self, action_key: str) -> "ModelWork":
        """Work for one proposed action (e.g. its Jev call): costs link to the derived decision id."""
        decision_id = derive_decision_id(self.source_id, action_key)
        await self._register(decision_id, "decision", decision_id)
        return ModelWork(context_key=decision_id, served_kind="decision", served_id=decision_id,
                         source_id=self.source_id, experiment_id=self.experiment_id, gateway=self.gateway,
                         deadline=self.deadline, register=self._register)


@dataclass(frozen=True)
class SignalContext:
    now: dt.datetime
    experiment: ExperimentView
    opportunity: SignalOpportunity
    work: ModelWork


@dataclass(frozen=True)
class EntryCycleContext:
    now: dt.datetime
    experiment: ExperimentView
    slot: Slot
    work: ModelWork


@dataclass(frozen=True)
class PositionCycleContext:
    now: dt.datetime
    experiment: ExperimentView
    slot: Slot
    positions: tuple[OwnedPosition, ...]
    work: ModelWork


@dataclass(frozen=True)
class ProposedDecision:
    """One proposed trader command. The controller adds decision_id and expires_at (spec 8)."""
    action_key: str
    action: str
    conid: int
    side: str
    decider: str
    evidence_digest: str
    deployment_digest: Optional[str] = None
    policy_revision: Optional[int] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    quantity: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_key, str) or not ACTION_KEY.fullmatch(self.action_key):
            raise ValueError("action_key must look like 'enter:265598'")
        if self.action not in ENTRY_ACTIONS | EXIT_ACTIONS:
            raise ValueError("action must be ENTER, CLOSE or PARTIAL_CLOSE")
        _conid(self.conid)
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not isinstance(self.decider, str) or not _DECIDER.fullmatch(self.decider):
            raise ValueError("decider must match ^[a-z][a-z0-9_.-]{0,63}$")
        for name in ("evidence_digest", "deployment_digest"):
            value = getattr(self, name)
            if (value is not None or name == "evidence_digest") and (
                    not isinstance(value, str) or not _DIGEST.fullmatch(value)):
                raise ValueError(f"{name} must be sha256:<64 hex>")
        _positive_int(self.policy_revision, "policy_revision")
        _positive_int(self.quantity, "quantity")
        _price(self.stop_price, "stop_price")
        _price(self.target_price, "target_price")


@dataclass(frozen=True)
class SimulatedBaseline:
    """A baseline decision for the trader to simulate (spec 7; Plan 2 record_simulated_decision)."""
    baseline_id: str
    cohort: str
    opportunity_id: str
    decided_at: dt.datetime
    conid: Optional[int] = None
    side: Optional[str] = None
    quantity: Optional[int] = None
    reference_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    linked_action_key: Optional[str] = None       # a decision of the same result
    linked_decision_id: Optional[str] = None      # an earlier decision (matched-entry baseline)
    linked_round_trip_id: Optional[str] = None    # matched-entry only: the trip whose close this is (Plan 2 R21)
    deployment_digest: Optional[str] = None       # the sized baselines name the deployment a real ENTER uses
    incomplete_reason: Optional[str] = None       # Plan 2 Ruling 18: evidence missing, nothing invented

    def __post_init__(self) -> None:
        if BASELINE_COHORTS.get(self.baseline_id) != self.cohort:
            raise ValueError(f"{self.baseline_id}/{self.cohort} is not an allowed baseline pair")
        if not isinstance(self.opportunity_id, str) or not _OPPORTUNITY_ID.fullmatch(self.opportunity_id):
            raise ValueError("opportunity_id must match ^[A-Za-z0-9_.:-]{1,128}$")
        if not isinstance(self.decided_at, dt.datetime) or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be an aware datetime")
        if self.linked_action_key is not None and self.linked_decision_id is not None:
            raise ValueError("a baseline links to one decision at most")
        if self.linked_action_key is not None and not ACTION_KEY.fullmatch(self.linked_action_key):
            raise ValueError("linked_action_key must look like 'enter:265598'")
        if self.linked_decision_id is not None and not _DECISION_ID.fullmatch(self.linked_decision_id):
            raise ValueError("linked_decision_id has a bad shape")
        if self.conid is not None:
            _conid(self.conid)
        if self.side not in (None, "BUY"):
            raise ValueError("baselines are long only (Plan 2 Ruling 6)")
        _positive_int(self.quantity, "quantity")
        for name in ("reference_price", "stop_price", "target_price"):
            _price(getattr(self, name), name)
        self._check_shape()

    def _check_shape(self) -> None:
        """The same shapes Plan 2's RecordSimulatedDecisionRequest enforces, so a bad record never queues."""
        trade = (self.side, self.quantity, self.reference_price, self.stop_price, self.target_price)
        if self.deployment_digest is not None and not _DIGEST.fullmatch(self.deployment_digest):
            raise ValueError("deployment_digest must be sha256:<64 hex>")
        if self.linked_round_trip_id is not None and (
                self.baseline_id != "matched_entry_bracket_exit.v1"
                or not _OPPORTUNITY_ID.fullmatch(self.linked_round_trip_id)):
            raise ValueError("linked_round_trip_id is a matched-entry field with the opportunity id shape")
        if self.baseline_id == "no_trade.v1":
            if any(v is not None for v in (*trade, self.deployment_digest, self.incomplete_reason)):
                raise ValueError("no_trade carries no trade, deployment or incomplete reason")
            return
        unranked = self.incomplete_reason == "ranking_unavailable"
        if unranked and self.baseline_id != "fixed_rule.v1":
            raise ValueError("ranking_unavailable belongs to the fixed rule only")
        if self.conid is None and not unranked:
            raise ValueError("a trading baseline names its conid (unless nothing could be ranked)")
        if self.incomplete_reason is not None:
            if self.incomplete_reason not in INCOMPLETE_REASONS or any(v is not None for v in trade):
                raise ValueError("an incomplete baseline has a known reason and no side, quantity or prices")
            return
        if any(v is None for v in trade[:1] + trade[2:]):
            raise ValueError("a complete trading baseline needs side and all three prices")
        if self.baseline_id in TRADER_SIZED_BASELINES:
            if self.quantity is not None or self.deployment_digest is None:
                raise ValueError("the trader sizes this baseline: no quantity, and name the deployment")
        elif self.quantity is None or self.linked_decision_id is None:
            raise ValueError("the matched-entry baseline carries its close's quantity and its ENTER decision")


@dataclass(frozen=True)
class EngineResult:
    decisions: tuple[ProposedDecision, ...] = ()
    baselines: tuple[SimulatedBaseline, ...] = ()
    note: str = ""                    # short audit text, e.g. "JEV_SKIP"; stored with the opportunity or cycle


class DecisionEngine(Protocol):
    async def on_entry_signal(self, ctx: SignalContext) -> EngineResult: ...

    async def on_exit_signal(self, ctx: SignalContext) -> EngineResult: ...

    async def on_entry_cycle(self, ctx: EntryCycleContext) -> EngineResult: ...

    async def on_position_cycle(self, ctx: PositionCycleContext) -> EngineResult: ...
