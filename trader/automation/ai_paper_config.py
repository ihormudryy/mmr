"""The ``ai_paper`` owner block of trader.yaml (spec 5.4, Plan 3 R22).

Only the validated config file grants ``ai_paper`` authority. No AI principal
can write it, and no environment variable overrides it.
"""
from __future__ import annotations

import copy
import logging
import math
import os
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

from trader.automation.risk_limits import PAPER_LIMITS, STEADY_LIMITS, RiskLimits, RiskLimitsError

logger = logging.getLogger(__name__)

SUPPORTED_STYLES = frozenset({"intraday_long"})
STYLE_NOT_ENABLED = "STYLE_NOT_ENABLED"
KILL_BASES = ("start", "peak")
OUTAGE_PAUSE_RANGE = (60, 3600)
DEFAULT_MODEL_BUDGET_USD_PER_DAY = 2000.0
_REFUSED_ENV_PREFIXES = ("AI_PAPER", "MMR_AI_PAPER")
_PARSED_KEYS = (
    "enabled", "styles", "limits_ceiling", "experiment_kill_drawdown_pct", "experiment_kill_basis",
    "broker_outage_pause_seconds", "acceptance_probe", "model_budget_usd_per_day",
)
_RAW_ONLY_KEYS = ("telegram",)  # parsed by Plan 5


class AiPaperConfigError(ValueError):
    """The ai_paper block is invalid; the message names the key path."""


@dataclass(frozen=True)
class AiPaperConfig:
    enabled: bool = False
    styles: tuple[str, ...] = ("intraday_long",)
    limits_ceiling: RiskLimits = PAPER_LIMITS
    experiment_kill_drawdown_pct: Optional[float] = None
    experiment_kill_basis: Literal["start", "peak"] = "start"
    broker_outage_pause_seconds: int = 300
    acceptance_probe: bool = False
    # SP2 Plan 2 Ruling 20: the owner's cap on model spend per New York day; served read-only to the ai service.
    model_budget_usd_per_day: float = DEFAULT_MODEL_BUDGET_USD_PER_DAY
    raw_section: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def style_enabled(self, style: str) -> bool:
        return style in self.styles


def load_ai_paper_config(
    raw: object, *, trading_mode: str, code_maximum: RiskLimits = STEADY_LIMITS,
) -> AiPaperConfig:
    _refuse_env_overrides(os.environ)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AiPaperConfigError("ai_paper must be a mapping")
    unknown = sorted(set(raw) - set(_PARSED_KEYS) - set(_RAW_ONLY_KEYS))
    if unknown:
        raise AiPaperConfigError(f"ai_paper.{unknown[0]}: unknown key")
    paper = trading_mode == "paper"
    enabled = _parse_bool(raw, "enabled", False)
    if enabled and not paper:
        raise AiPaperConfigError("ai_paper.enabled: true requires trading_mode: paper")
    acceptance_probe = _parse_bool(raw, "acceptance_probe", False)
    if acceptance_probe and not paper:
        raise AiPaperConfigError("ai_paper.acceptance_probe: true is allowed only with trading_mode: paper")
    ceiling = _parse_ceiling(raw.get("limits_ceiling", {}), code_maximum)
    kill_pct = _parse_kill(raw.get("experiment_kill_drawdown_pct"))
    _check_drawdown_guard(ceiling, kill_pct)
    return AiPaperConfig(
        enabled=enabled,
        styles=_parse_styles(raw.get("styles", ["intraday_long"])),
        limits_ceiling=ceiling,
        experiment_kill_drawdown_pct=kill_pct,
        experiment_kill_basis=_parse_kill_basis(raw.get("experiment_kill_basis", "start")),
        broker_outage_pause_seconds=_parse_outage_pause(raw),
        acceptance_probe=acceptance_probe,
        model_budget_usd_per_day=_parse_budget(raw.get("model_budget_usd_per_day", DEFAULT_MODEL_BUDGET_USD_PER_DAY)),
        raw_section=MappingProxyType(copy.deepcopy(dict(raw))),
    )


def _bare_names() -> frozenset[str]:
    return frozenset(name.upper() for name in (*_PARSED_KEYS, *RiskLimits.FIELDS))


def _refuse_env_overrides(environ: Mapping[str, str]) -> None:
    for name in sorted(environ):
        if name.upper().startswith(_REFUSED_ENV_PREFIXES):
            raise AiPaperConfigError(f"environment override refused: {name}")
    for name in sorted(_bare_names() & set(environ)):
        # A bare name is too common to refuse: it would stop the trader on an unrelated variable.
        logger.warning("%s ignored: ai_paper settings come only from trader.yaml", name)


def _parse_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if type(value) is not bool:
        raise AiPaperConfigError(f"ai_paper.{key} must be true or false")
    return value


def _parse_styles(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AiPaperConfigError("ai_paper.styles must be a non-empty list")
    for style in value:
        if not isinstance(style, str) or style not in SUPPORTED_STYLES:
            raise AiPaperConfigError(
                f"ai_paper.styles: {style!r} is not supported until the style phases "
                f"(supported: {sorted(SUPPORTED_STYLES)})")
    if len(set(value)) != len(value):
        raise AiPaperConfigError("ai_paper.styles must not repeat a style")
    return tuple(value)


def _parse_ceiling(value: object, code_maximum: RiskLimits) -> RiskLimits:
    if not isinstance(value, dict):
        raise AiPaperConfigError("ai_paper.limits_ceiling must be a mapping")
    ceiling = PAPER_LIMITS
    for key, field_value in value.items():
        if key not in RiskLimits.FIELDS:
            raise AiPaperConfigError(f"ai_paper.limits_ceiling.{key}: unknown key")
        try:
            ceiling = replace(ceiling, **{key: field_value})
        except RiskLimitsError as exc:
            raise AiPaperConfigError(f"ai_paper.limits_ceiling.{key}: {exc}") from None
    above = ceiling.fields_above(code_maximum)
    if above:
        names = ", ".join(f"ai_paper.limits_ceiling.{name}" for name in above)
        raise AiPaperConfigError(f"{names}: above the code maximum")
    problems = ceiling.structural_problems()
    if problems:
        raise AiPaperConfigError(f"ai_paper.limits_ceiling: {', '.join(problems)}")
    return ceiling


def _parse_kill(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not 0 < value < 100:
        raise AiPaperConfigError(
            "ai_paper.experiment_kill_drawdown_pct must be null or a percent strictly between 0 and 100")
    return float(value)


def _parse_kill_basis(value: object) -> Literal["start", "peak"]:
    if value not in KILL_BASES:
        raise AiPaperConfigError(f"ai_paper.experiment_kill_basis must be one of {KILL_BASES}")
    return value  # type: ignore[return-value]


def _parse_outage_pause(raw: Mapping[str, Any]) -> int:
    if "broker_outage_pause_seconds" not in raw:
        return 300
    value = raw["broker_outage_pause_seconds"]
    low, high = OUTAGE_PAUSE_RANGE
    if type(value) is not int or not low <= value <= high:
        raise AiPaperConfigError(
            f"ai_paper.broker_outage_pause_seconds must be an integer from {low} to {high}")
    return value


def _parse_budget(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise AiPaperConfigError("ai_paper.model_budget_usd_per_day: must be a finite number >= 0")
    return float(value)


def _check_drawdown_guard(ceiling: RiskLimits, kill_pct: Optional[float]) -> None:
    if ceiling.drawdown_fraction <= PAPER_LIMITS.drawdown_fraction:
        return
    if kill_pct is None or kill_pct / 100.0 > ceiling.drawdown_fraction:
        raise AiPaperConfigError(
            "ai_paper.experiment_kill_drawdown_pct must be set and no looser than "
            f"limits_ceiling.drawdown_fraction ({ceiling.drawdown_fraction})")
