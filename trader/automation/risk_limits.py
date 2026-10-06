"""Risk limits as data (spec 5.4). ``PAPER_LIMITS`` is what runs on the paper path today."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import ClassVar

from trader.promotion.allocation_policy import STEADY_MAX_GROSS_FRACTION

MAX_POSITIONS = 3
MAX_POSITION_FRACTION = 0.05
MAX_TRADE_RISK_FRACTION = 0.002
MAX_DAILY_LOSS_FRACTION = 0.005
MAX_DRAWDOWN_FRACTION = 0.03
PAPER_GROSS_FRACTION = 0.06
MAX_PENDING_ENTRY_ORDERS = 3

_COUNT_FIELDS = ("max_positions", "max_pending_entry_orders")


class RiskLimitsError(ValueError):
    def __init__(self, code: str, message: str, fields: tuple[str, ...] = ()):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.fields = fields


def _check_count(name: str, value: object) -> None:
    if type(value) is not int or value < 1:
        raise RiskLimitsError("LIMIT_INVALID", f"{name} must be an integer >= 1", (name,))


def _checked_fraction(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise RiskLimitsError("LIMIT_INVALID", f"{name} must be a finite number > 0", (name,))
    return float(value)


@dataclass(frozen=True)
class RiskLimits:
    max_positions: int
    position_fraction: float
    gross_fraction: float
    trade_risk_fraction: float
    daily_loss_fraction: float
    drawdown_fraction: float
    max_pending_entry_orders: int

    FIELDS: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self):
        # In-process callers bypass any wire model, so the types are checked here.
        for name in self.FIELDS:
            value = getattr(self, name)
            if name in _COUNT_FIELDS:
                _check_count(name, value)
            else:
                object.__setattr__(self, name, _checked_fraction(name, value))

    def tighter(self, other: "RiskLimits") -> "RiskLimits":
        return RiskLimits(**{name: min(getattr(self, name), getattr(other, name)) for name in self.FIELDS})

    def fields_above(self, other: "RiskLimits") -> tuple[str, ...]:
        return tuple(name for name in self.FIELDS if getattr(self, name) > getattr(other, name))

    def structural_problems(self) -> tuple[str, ...]:
        checks = (
            (self.gross_fraction <= 1.0, "GROSS_ABOVE_ONE"),
            (self.position_fraction <= self.gross_fraction, "POSITION_ABOVE_GROSS"),
            (self.daily_loss_fraction < 1.0, "DAILY_LOSS_NOT_BELOW_ONE"),
            (self.drawdown_fraction < 1.0, "DRAWDOWN_NOT_BELOW_ONE"),
        )
        return tuple(code for ok, code in checks if not ok)

    def to_json(self) -> dict:
        return {name: getattr(self, name) for name in self.FIELDS}

    @classmethod
    def from_json(cls, value: object) -> "RiskLimits":
        if not isinstance(value, dict) or set(value) != set(cls.FIELDS):
            raise RiskLimitsError("LIMIT_INVALID", f"limits must have exactly the keys {cls.FIELDS}")
        return cls(**value)


RiskLimits.FIELDS = tuple(field.name for field in fields(RiskLimits))

PAPER_LIMITS = RiskLimits(
    max_positions=MAX_POSITIONS,
    position_fraction=MAX_POSITION_FRACTION,
    gross_fraction=PAPER_GROSS_FRACTION,
    trade_risk_fraction=MAX_TRADE_RISK_FRACTION,
    daily_loss_fraction=MAX_DAILY_LOSS_FRACTION,
    drawdown_fraction=MAX_DRAWDOWN_FRACTION,
    max_pending_entry_orders=MAX_PENDING_ENTRY_ORDERS,
)
STEADY_LIMITS = replace(PAPER_LIMITS, gross_fraction=STEADY_MAX_GROSS_FRACTION)
