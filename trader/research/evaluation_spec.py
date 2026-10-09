"""The YAML spec behind `mmr research evaluate`, loaded and validated.

Every refusal raises ``EvaluationSpecError`` naming the field, so a bad spec
fails before any backtest runs.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from trader.objects import BarSize
from trader.research.strategy_paths import normalize_strategy_path
from trader.simulation.execution_costs import (
    ExecutionCostError,
    ExecutionCostsConfig,
    build_realistic_costs,
)

MIN_INSTRUMENTS = 8
# Calendars with a live paper-automation calendar policy (calendar_policy.py).
LIVE_CALENDARS = ('XNYS',)
# Live paper automation flattens every position at 15:45 ET, so coarser bars
# would hold positions overnight or block every entry.
LONGEST_BAR_SIZE = BarSize.Mins15


class EvaluationSpecError(ValueError):
    """The evaluation spec is invalid; the message names the field."""


@dataclass(frozen=True)
class EvaluationSpec:
    name: str
    strategy_path: str
    strategy_file: Path
    class_name: str
    params: Mapping[str, Any]
    neighbourhood: Mapping[str, tuple]
    conids: tuple[int, ...]
    bar_size: str
    period_start: dt.date
    period_end: dt.date
    folds: int
    embargo_sessions: int
    holdout_sessions: int
    order_notional: float
    account_equity: float
    max_gross_allocation: float
    calendar: str


def neighbour_points(spec: EvaluationSpec) -> list[dict]:
    points = []
    for key, values in spec.neighbourhood.items():
        for value in values:
            points.append({**spec.params, key: value})
    return points


def _require(raw: Mapping[str, Any], key: str) -> Any:
    if key not in raw or raw[key] in (None, '', [], {}):
        raise EvaluationSpecError(f'{key}: required')
    return raw[key]


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationSpecError(f'{field}: must be a mapping, got {value!r}')
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvaluationSpecError(f'{field}: must be a list, got {value!r}')
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationSpecError(f'{field}: must be an integer, got {value!r}')
    return value


def _date(value: Any, field: str) -> dt.date:
    # datetime is a date subclass; YAML turns an unquoted `2024-02-01 09:30` into one.
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    refusal = EvaluationSpecError(f'{field}: not an ISO date: {value!r}')
    if not isinstance(value, str):
        raise refusal
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise refusal from exc


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_number(raw: Mapping[str, Any], key: str, section: str) -> float:
    value = raw.get(key)
    if not _is_number(value) or not math.isfinite(value) or value <= 0:
        raise EvaluationSpecError(f'{section}.{key}: must be a positive number, got {value!r}')
    return float(value)


def _conids(raw_conids: Any) -> tuple[int, ...]:
    conids = tuple(
        _integer(conid, f'conids[{index}]')
        for index, conid in enumerate(_sequence(raw_conids, 'conids')))
    duplicates = sorted(conid for conid, count in Counter(conids).items() if count > 1)
    if duplicates:
        raise EvaluationSpecError(f'conids: duplicate conids {duplicates}')
    if len(conids) < MIN_INSTRUMENTS:
        raise EvaluationSpecError(f'conids: need at least {MIN_INSTRUMENTS} distinct conids, got {len(conids)}')
    return conids


def _neighbourhood(raw_neighbourhood: Any, params: Mapping[str, Any]) -> dict[str, tuple]:
    if not raw_neighbourhood:
        raise EvaluationSpecError('neighbourhood: required (adjacent values for at least one param)')
    neighbourhood = {}
    for key, raw_values in _mapping(raw_neighbourhood, 'neighbourhood').items():
        if key not in params:
            raise EvaluationSpecError(f'neighbourhood: {key} is not one of params')
        values = tuple(_sequence(raw_values, f'neighbourhood.{key}'))
        if not values or any(v == params[key] for v in values):
            raise EvaluationSpecError(
                f'neighbourhood: {key} needs values different from the main value {params[key]!r}')
        neighbourhood[key] = values
    return neighbourhood


def declared_tunables(strategy_file: Path, class_name: str) -> set[str]:
    module_spec = importlib.util.spec_from_file_location(f'_mmr_eval_{strategy_file.stem}', strategy_file)
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise EvaluationSpecError(
            f'strategy: {strategy_file.name} cannot be imported: {type(exc).__name__}: {exc}') from exc
    cls = getattr(module, class_name, None)
    if cls is None:
        raise EvaluationSpecError(f'class: {class_name} not found in {strategy_file.name}')
    return {name for name in dir(cls) if name.isupper() and not name.startswith('_')}


def _strategy_file(raw_path: str, repo_root: Path) -> tuple[str, Path]:
    strategies_dir = (repo_root / 'strategies').resolve()
    file = (repo_root / raw_path).resolve()
    if strategies_dir not in file.parents:
        raise EvaluationSpecError(f'strategy: {raw_path} must be inside strategies/')
    if not file.is_file():
        raise EvaluationSpecError(f'strategy: {raw_path} does not exist')
    return normalize_strategy_path(str(file), repo_root), file


def _calendar(conids: tuple[int, ...], universe_accessor: Any,
              costs_config: ExecutionCostsConfig) -> str:
    try:
        costs = build_realistic_costs(conids, universe_accessor, costs_config)
    except ExecutionCostError as exc:
        raise EvaluationSpecError(f'conids: {exc}') from exc
    venues = {venue.name: venue for venue in costs.venue_by_conid.values()}
    if len(venues) != 1:
        raise EvaluationSpecError(
            f'conids: one market per evaluation; got venues {sorted(venues)}')
    venue = next(iter(venues.values()))
    if not venue.calendar:
        raise EvaluationSpecError(
            f"conids: venue {venue.name!r} in execution_costs.yaml has no calendar; "
            f"add `calendar: XNYS` (or the venue's exchange calendar)")
    if venue.calendar not in LIVE_CALENDARS:
        raise EvaluationSpecError(
            f'conids: venue calendar {venue.calendar} has no live paper-automation policy; '
            f'paper automation only runs on {", ".join(LIVE_CALENDARS)}')
    return venue.calendar


def _bar_size(raw_bar_size: str) -> str:
    try:
        parsed = BarSize.parse_str(raw_bar_size)
    except ValueError as exc:
        raise EvaluationSpecError(f'bar_size: {raw_bar_size!r} is not a valid bar size') from exc
    if parsed > LONGEST_BAR_SIZE:
        raise EvaluationSpecError(
            f'bar_size: {raw_bar_size!r} is longer than 15 minutes; live paper automation '
            f'flattens at 15:45 ET, so evidence needs bars of 15 minutes or less')
    return raw_bar_size


def load_evaluation_spec(path: str | Path, *, universe_accessor: Any,
                         costs_config: ExecutionCostsConfig, repo_root: Path) -> EvaluationSpec:
    try:
        loaded = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise EvaluationSpecError(f'spec: not valid YAML: {exc}') from exc
    return build_evaluation_spec(_mapping(loaded or {}, 'spec'), universe_accessor=universe_accessor,
                                 costs_config=costs_config, repo_root=repo_root)


def build_evaluation_spec(raw: Mapping[str, Any], *, universe_accessor: Any,
                          costs_config: ExecutionCostsConfig, repo_root: Path) -> EvaluationSpec:
    name = str(_require(raw, 'name'))
    strategy_path, strategy_file = _strategy_file(str(_require(raw, 'strategy')), repo_root)
    class_name = str(_require(raw, 'class'))
    tunables = declared_tunables(strategy_file, class_name)

    params = dict(_mapping(_require(raw, 'params'), 'params'))
    unknown = sorted((k for k in params if k not in tunables), key=str)
    if unknown:
        raise EvaluationSpecError(f'params: not upper-case tunables of {class_name}: {unknown}')

    neighbourhood = _neighbourhood(raw.get('neighbourhood'), params)

    conids = _conids(_require(raw, 'conids'))
    calendar = _calendar(conids, universe_accessor, costs_config)

    bar_size = _bar_size(str(_require(raw, 'bar_size')))

    period = _mapping(_require(raw, 'period'), 'period')
    period_start = _date(period.get('start'), 'period.start')
    period_end = _date(period.get('end'), 'period.end')
    if period_start >= period_end:
        raise EvaluationSpecError('period: start must be before end')

    walk_forward = _mapping(_require(raw, 'walk_forward'), 'walk_forward')
    folds = _integer(walk_forward.get('folds', 0), 'walk_forward.folds')
    embargo = _integer(walk_forward.get('embargo_sessions', -1), 'walk_forward.embargo_sessions')
    holdout = _integer(walk_forward.get('holdout_sessions', 0), 'walk_forward.holdout_sessions')
    if folds < 1 or embargo < 0 or holdout < 1:
        raise EvaluationSpecError(
            'walk_forward: folds >= 1, embargo_sessions >= 0 and holdout_sessions >= 1 required')

    sizing = _mapping(_require(raw, 'sizing'), 'sizing')
    order_notional = _positive_number(sizing, 'order_notional', 'sizing')
    account_equity = _positive_number(sizing, 'account_equity', 'sizing')
    max_gross_allocation = raw.get('max_gross_allocation')
    if not _is_number(max_gross_allocation) or not 0 < max_gross_allocation <= 1:
        raise EvaluationSpecError(f'max_gross_allocation: must be in (0, 1], got {max_gross_allocation!r}')

    return EvaluationSpec(
        name=name, strategy_path=strategy_path, strategy_file=strategy_file,
        class_name=class_name, params=params, neighbourhood=neighbourhood,
        conids=conids, bar_size=bar_size, period_start=period_start,
        period_end=period_end, folds=folds, embargo_sessions=embargo,
        holdout_sessions=holdout, order_notional=order_notional,
        account_equity=account_equity, max_gross_allocation=float(max_gross_allocation),
        calendar=calendar,
    )
