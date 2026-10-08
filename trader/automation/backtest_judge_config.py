"""``ai_paper.backtest_judge`` in trader.yaml (SP2c spec 6.1).

Operator only: no environment override (``load_ai_paper_config`` refuses ``AI_PAPER*``
names) and no RPC method writes it. A trader restart applies an edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trader.research.strategy_key import is_strategy_key

MAX_COHORT_POINTS_LIMIT = 10
MAX_SESSION_COUNT = 120     # about six months of XNYS sessions
# key: (default, lowest, highest)
_INTEGER_KEYS: Mapping[str, tuple[int, int, int]] = {
    "evaluations_per_day": (10, 1, 100),
    "family_cooldown_sessions": (10, 1, MAX_SESSION_COUNT),
    "max_active_deploys": (3, 1, 20),
    "deploy_expiry_sessions": (20, 1, MAX_SESSION_COUNT),
    "max_cohort_points": (3, 1, MAX_COHORT_POINTS_LIMIT),
    "shadow_warmup_sessions": (5, 0, 60),
}
_KEYS = (*_INTEGER_KEYS, "strategy_allowlist")


class BacktestJudgeConfigError(ValueError):
    """The block is invalid; the message names the key path."""


@dataclass(frozen=True)
class BacktestJudgeConfig:
    evaluations_per_day: int = 10
    family_cooldown_sessions: int = 10
    max_active_deploys: int = 3
    deploy_expiry_sessions: int = 20
    max_cohort_points: int = 3
    shadow_warmup_sessions: int = 5
    strategy_allowlist: tuple[str, ...] = ()

    def allows(self, strategy_key: str) -> bool:
        return strategy_key in self.strategy_allowlist


def load_backtest_judge_config(raw: object) -> BacktestJudgeConfig:
    if raw is None:
        return BacktestJudgeConfig()
    if not isinstance(raw, dict):
        raise BacktestJudgeConfigError("ai_paper.backtest_judge must be a mapping")
    unknown = sorted(set(raw) - set(_KEYS))
    if unknown:
        raise BacktestJudgeConfigError(f"ai_paper.backtest_judge.{unknown[0]}: unknown key")
    integers = {key: _bounded(raw, key, *bounds) for key, bounds in _INTEGER_KEYS.items()}
    return BacktestJudgeConfig(**integers, strategy_allowlist=_allowlist(raw.get("strategy_allowlist", [])))


def _bounded(raw: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise BacktestJudgeConfigError(f"ai_paper.backtest_judge.{key} must be an integer from {low} to {high}")
    return value


def _allowlist(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BacktestJudgeConfigError("ai_paper.backtest_judge.strategy_allowlist must be a list")
    for item in value:
        if not is_strategy_key(item):
            raise BacktestJudgeConfigError(
                f"ai_paper.backtest_judge.strategy_allowlist: {item!r} is not strategies/<file>.py:<Class>")
    if len(set(value)) != len(value):
        raise BacktestJudgeConfigError("ai_paper.backtest_judge.strategy_allowlist must not repeat a key")
    return tuple(value)
