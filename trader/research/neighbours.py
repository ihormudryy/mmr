"""The neighbour points of a cohort point (Plan 3 Ruling 2). Standard library only."""
from __future__ import annotations

import math
from typing import Any, Mapping

NEIGHBOUR_SHARE = 0.1


def _neighbour_values(value: Any) -> tuple:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ()
    if isinstance(value, int):
        step = max(1, round(abs(value) * NEIGHBOUR_SHARE))
        return (value - step, value + step)
    if not math.isfinite(value) or value == 0:
        return ()
    return tuple(float(f"{v:.6g}") for v in (value * (1 - NEIGHBOUR_SHARE), value * (1 + NEIGHBOUR_SHARE)))


def neighbours_of(point: Mapping[str, Any]) -> tuple[dict, ...]:
    """Ruling 2: one key changes at a time, +/- 10 %."""
    return tuple({**point, key: v} for key, value in point.items() for v in _neighbour_values(value) if v != value)
