"""Kill-line evaluation (SP1 Plan 4 Task 2). Pure: no I/O, no clock.

The kill line is a drawdown from the experiment start (``start``) or from the
highest net liquidation seen since the start (``peak``). The line frozen in
the experiment and the line loaded at process start both act; the tighter
one wins (K9). The basis is always the frozen one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Optional


class KillLineInputError(ValueError):
    """An input cannot be trusted; the caller treats it as unknown, never as a hit."""


@dataclass(frozen=True)
class KillLine:
    pct: float
    basis: Literal["start", "peak"]

    def to_json(self) -> dict:
        return {"pct": self.pct, "basis": self.basis}


@dataclass(frozen=True)
class KillEvaluation:
    hit: bool
    reference: float
    net_liquidation: float
    drawdown_pct: float


def effective_kill_line(record: Any, config: Any) -> Optional[KillLine]:
    candidates = [value for value in (record.kill_drawdown_pct,
                                      getattr(config, "experiment_kill_drawdown_pct", None))
                  if value is not None]
    if not candidates:
        return None
    return KillLine(float(min(candidates)), record.kill_basis)


def evaluate_kill_line(line: KillLine, *, anchor: float, peak: float, net_liquidation: float) -> KillEvaluation:
    for name, value in (("anchor", anchor), ("peak", peak), ("net_liquidation", net_liquidation)):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise KillLineInputError(f"{name} must be a finite positive number, got {value!r}")
    reference = float(anchor) if line.basis == "start" else max(float(peak), float(anchor))
    drawdown_pct = (reference - float(net_liquidation)) / reference * 100.0
    return KillEvaluation(drawdown_pct >= line.pct, reference, float(net_liquidation), drawdown_pct)
