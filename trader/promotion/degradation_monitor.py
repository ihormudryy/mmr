"""P5 Task 4 — automatic allocation degradation from live evidence."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from trader.promotion.allocation_attestation import (
    STAGE_CANARY,
    STAGE_MAX_CEILING,
    STAGE_SCALE_1,
    STAGE_SCALE_2,
    STAGE_STEADY,
)
from trader.promotion.evidence_store import EvidenceWindow

__all__ = [
    "TRIGGER_BREAKER_TRIP",
    "TRIGGER_CAPACITY_BREACH",
    "TRIGGER_COST_BREACH",
    "TRIGGER_DRAWDOWN_BREACH",
    "TRIGGER_NEGATIVE_EXPECTANCY",
    "TRIGGER_REPLAY_MISMATCH",
    "TRIGGER_STALE_EVIDENCE",
    "DegradationAction",
    "DegradationDecision",
    "DegradationMonitor",
    "STAGE_PREVIOUS",
]

TRIGGER_COST_BREACH = "cost_breach"
TRIGGER_NEGATIVE_EXPECTANCY = "negative_expectancy"
TRIGGER_DRAWDOWN_BREACH = "drawdown_breach"
TRIGGER_STALE_EVIDENCE = "stale_evidence"
TRIGGER_REPLAY_MISMATCH = "replay_mismatch"
TRIGGER_BREAKER_TRIP = "breaker_trip"
TRIGGER_CAPACITY_BREACH = "capacity_breach"

STAGE_PREVIOUS = {
    STAGE_STEADY: STAGE_SCALE_2,
    STAGE_SCALE_2: STAGE_SCALE_1,
    STAGE_SCALE_1: STAGE_CANARY,
    STAGE_CANARY: STAGE_CANARY,
}


class DegradationAction(str, enum.Enum):
    WARN = "WARN"
    REDUCE_TO_PREVIOUS_STAGE = "REDUCE_TO_PREVIOUS_STAGE"
    SUSPEND = "SUSPEND"
    RETIRE = "RETIRE"


@dataclass(frozen=True)
class DegradationDecision:
    action: Optional[DegradationAction]
    triggers: tuple[str, ...]
    recommended_stage: Optional[str] = None
    recommended_max_gross: Optional[float] = None

    @property
    def requires_override(self) -> bool:
        return self.action in {
            DegradationAction.REDUCE_TO_PREVIOUS_STAGE,
            DegradationAction.SUSPEND,
            DegradationAction.RETIRE,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": None if self.action is None else self.action.value,
            "triggers": list(self.triggers),
            "recommended_stage": self.recommended_stage,
            "recommended_max_gross": self.recommended_max_gross,
        }


class DegradationMonitor:
    """Pure evaluator — caller applies restrictive overrides via the store."""

    def evaluate(
        self,
        window: EvidenceWindow,
        current_allocation_stage: str,
        *,
        current_max_gross: Optional[float] = None,
        capacity_breach_signals: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> DegradationDecision:
        triggers: list[str] = []
        action: Optional[DegradationAction] = None

        if window.replay_mismatches or window.divergences:
            triggers.append(TRIGGER_REPLAY_MISMATCH)
            action = _max_action(action, DegradationAction.SUSPEND)

        if window.breaker_trips:
            triggers.append(TRIGGER_BREAKER_TRIP)
            action = _max_action(action, DegradationAction.SUSPEND)

        if window.stale:
            triggers.append(TRIGGER_STALE_EVIDENCE)
            action = _max_action(action, DegradationAction.SUSPEND)

        if window.drawdown_breaches:
            triggers.append(TRIGGER_DRAWDOWN_BREACH)
            action = _max_action(action, DegradationAction.SUSPEND)

        if window.cost_breaches:
            triggers.append(TRIGGER_COST_BREACH)
            action = _max_action(action, DegradationAction.REDUCE_TO_PREVIOUS_STAGE)

        if _negative_expectancy(window):
            triggers.append(TRIGGER_NEGATIVE_EXPECTANCY)
            if current_allocation_stage == STAGE_CANARY:
                action = _max_action(action, DegradationAction.RETIRE)
            else:
                action = _max_action(action, DegradationAction.REDUCE_TO_PREVIOUS_STAGE)

        if capacity_breach_signals:
            triggers.append(TRIGGER_CAPACITY_BREACH)
            action = _max_action(action, DegradationAction.WARN)

        if not triggers:
            return DegradationDecision(action=None, triggers=())

        triggers = list(dict.fromkeys(triggers))
        recommended_stage: Optional[str] = None
        recommended_max_gross: Optional[float] = None

        if action == DegradationAction.RETIRE:
            recommended_stage = current_allocation_stage
            recommended_max_gross = 0.0
        elif action == DegradationAction.SUSPEND:
            recommended_stage = current_allocation_stage
            recommended_max_gross = 0.0
        elif action == DegradationAction.REDUCE_TO_PREVIOUS_STAGE:
            recommended_stage = STAGE_PREVIOUS.get(current_allocation_stage, STAGE_CANARY)
            recommended_max_gross = float(STAGE_MAX_CEILING[recommended_stage])
        elif action == DegradationAction.WARN:
            recommended_stage = current_allocation_stage
            recommended_max_gross = current_max_gross

        return DegradationDecision(
            action=action,
            triggers=tuple(triggers),
            recommended_stage=recommended_stage,
            recommended_max_gross=recommended_max_gross,
        )


def _negative_expectancy(window: EvidenceWindow) -> bool:
    records = window.round_trip_records
    if not records:
        return False
    total = Decimal("0")
    for record in records:
        pnl = record.get("pnl_after_cost")
        if pnl is None:
            continue
        total += Decimal(str(pnl))
    return total < 0


def _action_rank(action: Optional[DegradationAction]) -> int:
    if action is None:
        return -1
    return {
        DegradationAction.WARN: 0,
        DegradationAction.REDUCE_TO_PREVIOUS_STAGE: 1,
        DegradationAction.SUSPEND: 2,
        DegradationAction.RETIRE: 3,
    }[action]


def _max_action(
    current: Optional[DegradationAction],
    candidate: DegradationAction,
) -> DegradationAction:
    return candidate if _action_rank(candidate) > _action_rank(current) else current
