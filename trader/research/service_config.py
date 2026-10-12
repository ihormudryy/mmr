"""The research service's evaluation defaults (`research_service:` in trader.yaml; SP2c Plan 3 ruling 3)."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping

_INTEGER_FIELDS = ("period_sessions", "folds", "embargo_sessions", "holdout_sessions", "queue_max",
                   "shadow_incomplete_after_hours", "authored_evaluation_sessions")
# folds, embargo_sessions and holdout_sessions are bounded by period_sessions through the walk-forward check.
_INTEGER_MAX = {"period_sessions": 2520, "queue_max": 1000, "shadow_incomplete_after_hours": 720}


class ResearchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchServiceConfig:
    period_sessions: int = 690
    folds: int = 6
    embargo_sessions: int = 5
    holdout_sessions: int = 90
    order_notional: float = 1900.0
    account_equity: float = 100_000.0
    max_gross_allocation: float = 0.05
    queue_max: int = 20
    shadow_incomplete_after_hours: int = 16
    # Owner ruling 2026-10-11 (SP3 spec 14): an authored strategy is evaluated on the most recent N sessions.
    authored_evaluation_sessions: int = 302


def _integer(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchConfigError(f"research_service.{name}: must be an integer")
    if value < 0 or (value == 0 and name != "embargo_sessions"):
        raise ResearchConfigError(f"research_service.{name}: must be positive, got {value!r}")
    if name in _INTEGER_MAX and value > _INTEGER_MAX[name]:
        raise ResearchConfigError(f"research_service.{name}: at most {_INTEGER_MAX[name]}")
    return value


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchConfigError(f"research_service.{name}: must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise ResearchConfigError(f"research_service.{name}: too large") from None
    if not math.isfinite(number) or number <= 0:
        raise ResearchConfigError(f"research_service.{name}: must be positive, got {value!r}")
    return number


def _value(name: str, value: Any) -> Any:
    return _integer(name, value) if name in _INTEGER_FIELDS else _number(name, value)


def _require_buildable_walk_forward(config: ResearchServiceConfig) -> None:
    """The per-segment rule of ``generate_walk_forward``, checked once at load instead of on every request."""
    keys = "period_sessions, holdout_sessions, folds and embargo_sessions"
    pool = config.period_sessions - config.holdout_sessions
    if pool < config.folds + 1:
        raise ResearchConfigError(
            f"research_service: {keys} leave {pool} sessions before the holdout; "
            f"{config.folds} folds need at least {config.folds + 1}")
    segment = pool // (config.folds + 1)
    if segment < config.embargo_sessions + 1:
        raise ResearchConfigError(
            f"research_service: {keys} give {segment} sessions per segment; "
            f"embargo_sessions={config.embargo_sessions} needs at least {config.embargo_sessions + 1}")


def authored_walk_forward(config: ResearchServiceConfig) -> ResearchServiceConfig:
    """The research config an authored evaluation uses: the most recent ``authored_evaluation_sessions`` sessions,
    the same folds and embargo, and the holdout scaled in proportion (SP2c's 90 of 690 becomes 39 of 302)."""
    sessions = min(config.authored_evaluation_sessions, config.period_sessions)
    if sessions == config.period_sessions:
        return config
    holdout = max(1, round(config.holdout_sessions * sessions / config.period_sessions))
    return replace(config, period_sessions=sessions, holdout_sessions=holdout)


def _require_buildable_authored_walk_forward(config: ResearchServiceConfig) -> None:
    if config.authored_evaluation_sessions > config.period_sessions:
        raise ResearchConfigError(
            f"research_service.authored_evaluation_sessions: at most period_sessions ({config.period_sessions})")
    try:
        _require_buildable_walk_forward(authored_walk_forward(config))
    except ResearchConfigError as exc:
        raise ResearchConfigError(f"research_service.authored_evaluation_sessions: {exc}") from None


def load_research_service_config(raw: Mapping[str, Any]) -> ResearchServiceConfig:
    block = raw.get("research_service")
    if block is None:
        block = {}
    if not isinstance(block, Mapping):
        raise ResearchConfigError("research_service: must be a mapping")
    known = {f.name for f in fields(ResearchServiceConfig)}
    unknown = sorted((key for key in block if key not in known), key=repr)
    if unknown:
        raise ResearchConfigError(f"research_service: unknown keys {unknown}")
    config = ResearchServiceConfig(**{name: _value(name, value) for name, value in block.items()})
    if config.max_gross_allocation > 1:
        raise ResearchConfigError("research_service.max_gross_allocation: must be in (0, 1]")
    _require_buildable_walk_forward(config)
    _require_buildable_authored_walk_forward(config)
    return config
