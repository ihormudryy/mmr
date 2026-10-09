"""The pure rules of one evaluation request: names, limits and the checks both sides run.

Standard library, the canonical JSON and BarSize only, so the ai controller can screen a cohort
exactly as the research service will, without loading the backtester or ib_async.
"""
from __future__ import annotations

import math
import re

from trader.bar_size import BarSize
from trader.research.canonical import canonical_json_bytes

TUNABLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_CONIDS = 20
MAX_PARAMS = 32
MIN_INSTRUMENTS = 8
# Live paper automation flattens every position at 15:45 ET, so coarser bars
# would hold positions overnight or block every entry.
LONGEST_BAR_SIZE = BarSize.Mins15


def check_params(params: dict) -> dict:
    """One parameter point: upper-case tunable names and finite scalar values only."""
    if len(params) > MAX_PARAMS:
        raise ValueError(f"a parameter point has at most {MAX_PARAMS} tunables")
    for name, value in params.items():
        if not TUNABLE_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an upper-case tunable name")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if isinstance(value, str) and not 1 <= len(value) <= 128:
            raise ValueError(f"{name} must be 1-128 characters")
    return params


def check_cohort(points: list[dict]) -> list[dict]:
    """Every point is a valid parameter set and no point repeats (by its canonical spelling)."""
    for point in points:
        check_params(point)
    if len({canonical_json_bytes(point) for point in points}) != len(points):
        raise ValueError("cohort points must be distinct")
    return points


def check_conids(conids: list[int]) -> list[int]:
    if any(conid <= 0 for conid in conids):
        raise ValueError("conids must be positive")
    if any(later <= earlier for earlier, later in zip(conids, conids[1:])):
        raise ValueError("conids must be strictly increasing (sorted, no repeats)")
    return conids


def check_bar_size(bar_size: str) -> str:
    try:
        parsed = BarSize.parse_str(bar_size)
    except ValueError:
        raise ValueError(f"{bar_size!r} is not a bar size") from None
    if parsed > LONGEST_BAR_SIZE:
        raise ValueError(f"{bar_size!r} is longer than 15 minutes")
    return bar_size
