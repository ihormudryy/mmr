"""P5 Task 7 — combined account portfolio risk budget.

Evaluates multi-strategy intents against signed authorities while preserving
the unchanged 0.50% daily-loss ceiling and single-strategy safety limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "ACCOUNT_DAILY_LOSS_LIMIT",
    "BLOCK_COMBINED_GROSS",
    "BLOCK_DAILY_LOSS",
    "BLOCK_MISSING_PORTFOLIO_AUTHORITY",
    "BLOCK_POSITION_COUNT",
    "PortfolioRiskBudget",
    "PortfolioRiskDecision",
]

ACCOUNT_DAILY_LOSS_LIMIT = 0.005
MAX_POSITION_COUNT = 3

BLOCK_COMBINED_GROSS = "combined_gross_exposure_breach"
BLOCK_DAILY_LOSS = "combined_daily_loss_breach"
BLOCK_POSITION_COUNT = "position_count_breach"
BLOCK_MISSING_PORTFOLIO_AUTHORITY = "portfolio_authority_absent"


@dataclass(frozen=True)
class PortfolioRiskDecision:
    passed: bool
    blockers: tuple[str, ...]
    combined_gross: float
    combined_daily_loss: float
    position_count: int
    remaining_capacity: Optional[float] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blockers": list(self.blockers),
            "combined_gross": self.combined_gross,
            "combined_daily_loss": self.combined_daily_loss,
            "position_count": self.position_count,
            "remaining_capacity": self.remaining_capacity,
        }


def _authority_ceiling(authority: Any) -> float:
    if authority is None:
        return 0.0
    if hasattr(authority, "max_gross_allocation"):
        return float(authority.max_gross_allocation)
    if isinstance(authority, Mapping):
        return float(authority.get("max_gross_allocation", 0.0))
    return 0.0


class PortfolioRiskBudget:
    """Pure evaluator consumed at coordinator serialization point."""

    def __init__(
        self,
        *,
        daily_loss_limit: float = ACCOUNT_DAILY_LOSS_LIMIT,
        max_position_count: int = MAX_POSITION_COUNT,
    ):
        self._daily_loss_limit = daily_loss_limit
        self._max_position_count = max_position_count

    def evaluate(
        self,
        intents: Sequence[Mapping[str, Any]],
        broker_snapshot: Mapping[str, Any],
        authorities: Sequence[Any],
        *,
        portfolio_authority_present: bool = False,
        strategy_count: int = 1,
    ) -> PortfolioRiskDecision:
        blockers: list[str] = []

        positions = broker_snapshot.get("positions") or ()
        position_count = len(positions)
        if position_count > self._max_position_count:
            blockers.append(BLOCK_POSITION_COUNT)

        gross_by_strategy = [abs(_float(i.get("proposed_gross", 0.0))) for i in intents]
        combined_gross = sum(gross_by_strategy) + abs(_float(broker_snapshot.get("gross_exposure", 0.0)))

        ceilings = [_authority_ceiling(a) for a in authorities]
        signed_ceiling = min(ceilings) if ceilings else 0.0
        if signed_ceiling > 0 and combined_gross > signed_ceiling + 1e-9:
            blockers.append(BLOCK_COMBINED_GROSS)

        daily_loss = abs(_float(broker_snapshot.get("daily_loss_pct", 0.0)))
        projected = daily_loss + sum(abs(_float(i.get("projected_daily_loss", 0.0))) for i in intents)
        if projected > self._daily_loss_limit:
            blockers.append(BLOCK_DAILY_LOSS)

        if strategy_count > 1 and not portfolio_authority_present:
            blockers.append(BLOCK_MISSING_PORTFOLIO_AUTHORITY)

        remaining = None
        if signed_ceiling > 0:
            remaining = max(0.0, signed_ceiling - combined_gross)

        blockers = list(dict.fromkeys(blockers))
        return PortfolioRiskDecision(
            passed=not blockers,
            blockers=tuple(blockers),
            combined_gross=combined_gross,
            combined_daily_loss=projected,
            position_count=position_count,
            remaining_capacity=remaining,
        )


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
