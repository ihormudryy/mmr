"""The research menu and the screening of the orchestrator's picks (SP2c spec 2, 5.3; Plan 4 Rulings 4-7).

Models pick refs from a code-built menu. Code owns conids, files, tunable names and types, and every limit.
Strategy files are only read as text (AST), never imported."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Union

from trader.ai.config import ResearchCycleConfig
from trader.research.neighbours import neighbours_of
from trader.research.request_rules import (
    MAX_CONIDS, MAX_PARAMS, MIN_INSTRUMENTS, TUNABLE_NAME, check_bar_size, check_cohort, check_conids,
)
from trader.strategy.inspect import scan_strategies

Scalar = Union[bool, int, float]
OFFERED_TYPES = (int, float)              # type() is exact, so a bool default is not offered
MAX_MAGNITUDE = 2 ** 53                   # larger numbers are not exact as floats and break the neighbour steps


@dataclass(frozen=True)
class StrategyChoice:
    ref: str
    strategy_key: str
    tunables: Mapping[str, Scalar]


@dataclass(frozen=True)
class ResearchMenu:
    strategies: Mapping[str, StrategyChoice]
    universes: Mapping[str, tuple[str, tuple[int, ...]]]
    bar_sizes: Mapping[str, str]
    max_candidates: int
    max_points: int

    def to_json(self) -> dict:
        return {"max_candidates": self.max_candidates, "max_points": self.max_points,
                "strategies": [{"strategy": c.ref, "key": c.strategy_key, "tunables": dict(c.tunables)}
                               for c in self.strategies.values()],
                "universes": [{"universe": ref, "name": name, "conids": list(conids)}
                              for ref, (name, conids) in self.universes.items()],
                "bar_sizes": [{"bar_size": ref, "value": size} for ref, size in self.bar_sizes.items()]}


@dataclass(frozen=True)
class Cohort:
    strategy_key: str
    conids: tuple[int, ...]
    bar_size: str
    points: tuple[Mapping[str, Scalar], ...]
    thesis: str

    def submit_body(self) -> dict:
        """Plan 3's INITIAL submit_evaluation body. The research service adds the day (Plan 3 Ruling 1)."""
        return {"kind": "INITIAL", "strategy_key": self.strategy_key,
                "cohort": [dict(sorted(point.items())) for point in self.points],
                "conids": sorted(self.conids), "bar_size": self.bar_size}


@dataclass(frozen=True)
class Dropped:
    pick: int                 # index in the model's list; -1 for a menu entry
    code: str
    detail: str = ""


def _in_range(value: Scalar) -> bool:
    return abs(value) < MAX_MAGNITUDE         # False for inf and nan too


def _offered_tunables(declared: Mapping[str, Any]) -> dict[str, Scalar]:
    return {name: value for name, value in declared.items()
            if TUNABLE_NAME.fullmatch(name) and type(value) in OFFERED_TYPES and _in_range(value)}


def _menu_strategies(config: ResearchCycleConfig, strategies_root: Path, cooling: frozenset[str],
                     dropped: list[Dropped]) -> dict[str, StrategyChoice]:
    try:
        rows = {(row["file"], row["class"]): row for row in scan_strategies(strategies_root / "strategies", include_params=False)}
    except (OSError, ValueError, RecursionError) as error:         # one unreadable file must not stop the cycle
        dropped.append(Dropped(-1, "STRATEGY_SCAN_FAILED", type(error).__name__))
        return {}
    strategies: dict[str, StrategyChoice] = {}
    for key in config.strategy_keys:
        path, class_name = key.split(":")
        row = rows.get((Path(path).name, class_name))
        if key in cooling:
            dropped.append(Dropped(-1, "COOLING_DOWN", key))
        elif row is None:
            dropped.append(Dropped(-1, "STRATEGY_NOT_FOUND", key))
        else:
            tunables = _offered_tunables(row["tunables"])
            if not tunables:
                dropped.append(Dropped(-1, "NO_NUMERIC_TUNABLES", key))     # the service needs a numeric tunable
                continue
            if len(tunables) > MAX_PARAMS:
                dropped.append(Dropped(-1, "TOO_MANY_TUNABLES", key))
                continue
            ref = f"S{len(strategies) + 1}"
            strategies[ref] = StrategyChoice(ref, key, tunables)
    return strategies


def _menu_universes(config: ResearchCycleConfig, dropped: list[Dropped]) -> dict[str, tuple[str, tuple[int, ...]]]:
    universes: dict[str, tuple[str, tuple[int, ...]]] = {}
    for name, conids in sorted(config.universes.items()):
        try:
            checked = tuple(check_conids(sorted(conids)))
            if not MIN_INSTRUMENTS <= len(checked) <= MAX_CONIDS:
                raise ValueError("universe size")
        except ValueError:
            dropped.append(Dropped(-1, "UNIVERSE_INVALID", name))
            continue
        universes[f"U{len(universes) + 1}"] = (name, checked)
    return universes


def _menu_bar_sizes(config: ResearchCycleConfig, dropped: list[Dropped]) -> dict[str, str]:
    bar_sizes: dict[str, str] = {}
    for size in dict.fromkeys(config.bar_sizes):
        try:
            bar_sizes[f"B{len(bar_sizes) + 1}"] = check_bar_size(size)
        except ValueError:
            dropped.append(Dropped(-1, "BAR_SIZE_NOT_ELIGIBLE", size))
    return bar_sizes


def build_menu(config: ResearchCycleConfig, *, strategies_root: Path,
               cooling: frozenset[str]) -> tuple[ResearchMenu, tuple[Dropped, ...]]:
    dropped: list[Dropped] = []
    strategies = _menu_strategies(config, strategies_root, cooling, dropped)
    universes = _menu_universes(config, dropped)
    bar_sizes = _menu_bar_sizes(config, dropped)
    menu = ResearchMenu(strategies, universes, bar_sizes, config.max_candidates_per_cycle, config.max_cohort_points)
    return menu, tuple(dropped)


def _normalize(raw: Mapping[str, Any], tunables: Mapping[str, Scalar]) -> Union[dict, str]:
    """The full point: every offered tunable, the model's overrides typed like the default (Ruling 6); or a code."""
    point = dict(tunables)
    for name, value in raw.items():
        if name not in tunables:
            return "UNDECLARED_TUNABLE"
        default = tunables[name]
        if type(value) in OFFERED_TYPES and not _in_range(value):
            return "POINT_INVALID"
        if type(default) is float and type(value) is int:
            value = float(value)
        if type(value) is not type(default):
            return "TUNABLE_TYPE"
        point[name] = value
    return point


def _off_menu_code(pick: Any, menu: ResearchMenu) -> Union[str, None]:
    if pick.strategy not in menu.strategies:
        return "OFF_MENU_STRATEGY"
    if pick.universe not in menu.universes:
        return "OFF_MENU_UNIVERSE"
    if pick.bar_size not in menu.bar_sizes:
        return "OFF_MENU_BAR_SIZE"
    return None


def screen_picks(picks: Sequence[Any], menu: ResearchMenu) -> tuple[tuple[Cohort, ...], tuple[Dropped, ...]]:
    """One frozen cohort per strategy key (Ruling 7). The first kept pick fixes conids and bar size.
    Every point also passes the research service's own request rules (`check_cohort`)."""
    open_cohorts: dict[str, dict] = {}
    dropped: list[Dropped] = []
    for index, pick in enumerate(picks):
        problem = _off_menu_code(pick, menu)
        if problem is not None:
            dropped.append(Dropped(index, problem))
            continue
        choice = menu.strategies[pick.strategy]
        conids, bar_size = menu.universes[pick.universe][1], menu.bar_sizes[pick.bar_size]
        entry = open_cohorts.get(choice.strategy_key)
        if entry is None and len(open_cohorts) >= menu.max_candidates:
            dropped.append(Dropped(index, "CANDIDATE_LIMIT"))
            continue
        if entry is not None and (entry["conids"], entry["bar_size"]) != (conids, bar_size):
            dropped.append(Dropped(index, "COHORT_CONFLICT"))
            continue
        points = []
        for raw in pick.points:
            point = _normalize(raw, choice.tunables)
            if isinstance(point, str):
                dropped.append(Dropped(index, point, ",".join(sorted(raw))))
            elif not neighbours_of(point):
                dropped.append(Dropped(index, "POINT_INVALID", "no neighbours"))
            else:
                points.append(point)
        pending = entry or {"conids": conids, "bar_size": bar_size, "points": [], "thesis": pick.thesis}
        for point in points:
            if point in pending["points"]:
                dropped.append(Dropped(index, "DUPLICATE_POINT"))
            elif len(pending["points"]) >= menu.max_points:
                dropped.append(Dropped(index, "COHORT_POINT_LIMIT"))
            else:
                try:
                    check_cohort(pending["points"] + [point])
                except ValueError:
                    dropped.append(Dropped(index, "POINT_INVALID"))
                else:
                    pending["points"].append(point)
        if pending["points"]:
            open_cohorts[choice.strategy_key] = pending
        else:
            dropped.append(Dropped(index, "NO_VALID_POINTS"))
    cohorts = tuple(Cohort(key, entry["conids"], entry["bar_size"], tuple(entry["points"]), entry["thesis"])
                    for key, entry in open_cohorts.items())
    return cohorts, tuple(dropped)
