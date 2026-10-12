"""``ai_paper.authored_strategies`` in trader.yaml (SP3 spec 6).

Operator only: ``load_ai_paper_config`` refuses every ``AI_PAPER*`` environment name and no RPC method
writes it. A trader restart applies an edit. The trader never reads ai.yaml, so the bar sizes an authored
file may use are listed here (Plan 3 ruling 3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trader.research.request_rules import check_bar_size

DEFAULT_BAR_SIZES = ("1 min", "5 mins", "15 mins")
# Controller decision C2: at most 3, the sandbox live pool (Plan 2 LIVE_POOL_SIZE: one session per deployment).
MAX_ACTIVE_RANGE = (1, 3)
_KEYS = ("enabled", "max_active", "bar_sizes")
_PREFIX = "ai_paper.authored_strategies"


class AuthoredStrategiesConfigError(ValueError):
    """The block is invalid; the message names the key path."""


@dataclass(frozen=True)
class AuthoredStrategiesConfig:
    enabled: bool = False
    max_active: int = 3
    bar_sizes: tuple[str, ...] = DEFAULT_BAR_SIZES


def load_authored_strategies_config(raw: object) -> AuthoredStrategiesConfig:
    if raw is None:
        return AuthoredStrategiesConfig()
    if not isinstance(raw, dict):
        raise AuthoredStrategiesConfigError(f"{_PREFIX} must be a mapping")
    unknown = sorted(set(raw) - set(_KEYS))
    if unknown:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.{unknown[0]}: unknown key")
    return AuthoredStrategiesConfig(enabled=_enabled(raw), max_active=_max_active(raw),
                                    bar_sizes=_bar_sizes(raw.get("bar_sizes", list(DEFAULT_BAR_SIZES))))


def _enabled(raw: Mapping[str, Any]) -> bool:
    value = raw.get("enabled", False)
    if type(value) is not bool:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.enabled must be true or false")
    return value


def _max_active(raw: Mapping[str, Any]) -> int:
    value = raw.get("max_active", 3)
    low, high = MAX_ACTIVE_RANGE
    if type(value) is not int or not low <= value <= high:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.max_active must be an integer from {low} to {high}")
    return value


def _bar_sizes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes must be a non-empty list")
    for size in value:
        if not isinstance(size, str):
            raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes: {size!r} is not text")
        try:
            check_bar_size(size)
        except ValueError as exc:
            raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes: {exc}") from None
    if len(set(value)) != len(value):
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes must not repeat a bar size")
    return tuple(value)
