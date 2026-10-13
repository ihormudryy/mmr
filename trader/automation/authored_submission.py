"""Checks of one submit_authored_strategy body before anything is written (SP3 spec 7.3; Plan 3 ruling 5).

Pure: no file and no journal. The order follows spec 7.3: the static check, the bar size, the tunables
(the store checks the switch and the cap first). The trader reads the file with ``ast`` and Plan 1's
checker only; it never imports or runs it. The ai service's author runs ``check_authored_file`` on the
body it is about to send (SP3 Plan 5), so this module imports neither the wire models nor the store.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Mapping, Optional, Sequence

from trader.authored.static_check import AuthoredStaticRejected, _parse_quietly, require_clean
from trader.automation.authored_refusals import (
    AUTHORED_BAD_BAR_SIZE, AUTHORED_BAD_TUNABLES, AUTHORED_STATIC_REJECTED, AuthoredRefused,
)
from trader.automation.strategy_binding import bar_size_key
from trader.research.request_rules import TUNABLE_NAME

if TYPE_CHECKING:
    from trader.automation.authored_config import AuthoredStrategiesConfig
    from trader.automation.authored_wire import SubmitAuthoredStrategyRequest

NOT_TUNABLE = frozenset({"LOOKBACK_BARS"})     # fixed by the API (spec 7.1)
MAX_STATIC_DETAIL_CHARS = 2000
# Every tunable number (low, high, default, class value) stays within this; a larger int cannot become a float.
MAX_TUNABLE_ABS = 1e15
# Their bodies are scopes of their own: a name bound inside them is not a class attribute.
_OWN_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


@dataclass(frozen=True)
class CheckedSubmission:
    class_name: str
    bar_size: str
    tunables: dict[str, dict[str, float]]


def check_submission(request: SubmitAuthoredStrategyRequest, config: AuthoredStrategiesConfig) -> CheckedSubmission:
    tunables = {name: bounds.model_dump() for name, bounds in request.tunables.items()}
    return check_authored_file(request.source, request.bar_size, tunables, config.bar_sizes)


def check_authored_file(source: str, bar_size: str, tunables: Mapping[str, Mapping[str, float]],
                        bar_sizes: Sequence[str]) -> CheckedSubmission:
    """The checks of one submitted file, from the body's own fields: ``tunables`` maps each name to
    ``{"low", "high", "default"}`` and ``bar_sizes`` is the configured list."""
    class_name = _static_class(source)
    checked_bar_size = _bar_size(bar_size, bar_sizes)
    return CheckedSubmission(class_name, checked_bar_size, _tunables(source, class_name, tunables))


def _static_class(source: str) -> str:
    try:
        return require_clean(source)
    except AuthoredStaticRejected as rejected:
        detail = "; ".join(f"{v.rule}@{v.line}: {v.detail}" for v in rejected.violations)
        raise AuthoredRefused(AUTHORED_STATIC_REJECTED,
                              detail[:MAX_STATIC_DETAIL_CHARS] or "the static check refused the file") from None


def _bar_size(bar_size: str, bar_sizes: Sequence[str]) -> str:
    if bar_size_key(bar_size) not in {bar_size_key(size) for size in bar_sizes}:
        raise AuthoredRefused(AUTHORED_BAD_BAR_SIZE, f"{bar_size!r} is not one of {list(bar_sizes)}")
    return bar_size


def _tunables(source: str, class_name: str,
              tunables: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    if not tunables:
        raise AuthoredRefused(AUTHORED_BAD_TUNABLES, "an authored file needs at least one tunable: SP2c varies one "
                                                     "for its neighbour points")
    constants = class_constants(source, class_name)
    checked = {}
    for name in sorted(tunables):
        bounds = tunables[name]
        low, high, default = bounds["low"], bounds["high"], bounds["default"]
        problem = _tunable_problem(name, low, high, default, constants)
        if problem is not None:
            raise AuthoredRefused(AUTHORED_BAD_TUNABLES, problem)
        checked[name] = {"low": low, "high": high, "default": default}
    if all(bounds["low"] == bounds["high"] for bounds in checked.values()):
        raise AuthoredRefused(AUTHORED_BAD_TUNABLES, "nothing to vary: at least one tunable needs low < high")
    return checked


def _bounded(value: float) -> bool:
    """Finite and within MAX_TUNABLE_ABS; compares a huge int exactly instead of converting it."""
    return abs(value) <= MAX_TUNABLE_ABS


def _tunable_problem(name: str, low: float, high: float, default: float,
                     constants: Mapping[str, float]) -> Optional[str]:
    if not TUNABLE_NAME.fullmatch(name):
        return f"{name!r} is not an upper-case tunable name"
    if name in NOT_TUNABLE:
        return f"{name} is fixed by the API, not a tunable"
    if name not in constants:
        return f"{name} is not a numeric class constant of the strategy"
    if not (_bounded(low) and _bounded(high)):
        return f"{name}: the range is not a finite, ordered range within ±{MAX_TUNABLE_ABS:g}"
    if low > high:
        return f"{name}: [{low}, {high}] is not a finite, ordered range"
    if not _bounded(constants[name]):
        return f"{name}: the class value is not a finite number within ±{MAX_TUNABLE_ABS:g}"
    if not low <= constants[name] <= high:
        return f"{name}: the class value {constants[name]} is outside [{low}, {high}]"
    if not _bounded(default):
        return f"{name}: the default is not a finite number within ±{MAX_TUNABLE_ABS:g}"
    if default != constants[name]:
        return f"{name}: the default {default} is not the class value {constants[name]}"
    return None


def class_constants(source: str, class_name: str) -> dict[str, float]:
    """Upper-case class-level constants of ``class_name`` that are plain numbers (a bool is not one).

    A name the class body binds more than once (a reassignment, ``+=``, a tuple target, a method of
    that name) is left out: its first number need not be the class value.
    """
    return {name: value for name, (value, bindings) in _number_constants(source, class_name).items()
            if bindings == 1}


def rebound_constants(source: str, class_name: str) -> frozenset[str]:
    """The upper-case number constants that ``class_constants`` leaves out because the class binds them
    more than once (a bare annotation before the value counts as a binding)."""
    return frozenset(name for name, (_, bindings) in _number_constants(source, class_name).items()
                     if bindings > 1)


def _number_constants(source: str, class_name: str) -> dict[str, tuple[float, int]]:
    """Each upper-case number constant of the class body: its first number and how often the class binds it."""
    for node in _parse_quietly(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            bindings = Counter(_bound_names(node.body))
            found: dict[str, tuple[float, int]] = {}
            for name, value in map(_constant, node.body):
                if name is not None and name not in found:
                    found[name] = (value, bindings[name])
            return found
    return {}


def _bound_names(body: list[ast.stmt]) -> Iterator[str]:
    """Every name the class scope binds, once per binding."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
            continue
        if isinstance(node, _OWN_SCOPES):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield node.id
        elif isinstance(node, ast.alias):
            yield (node.asname or node.name).split(".")[0]
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        pending.extend(ast.iter_child_nodes(node))


def _constant(statement: ast.stmt) -> tuple[Optional[str], Optional[float]]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 \
            and isinstance(statement.targets[0], ast.Name):
        name, value = statement.targets[0].id, statement.value
    elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) \
            and statement.value is not None:
        name, value = statement.target.id, statement.value
    else:
        return None, None
    number = _number(value)
    if number is None or not TUNABLE_NAME.fullmatch(name):
        return None, None
    return name, number


def _number(node: ast.expr) -> Optional[float]:
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return sign * node.value                # unbounded here; a tunable's value is bounded when checked
    return None
