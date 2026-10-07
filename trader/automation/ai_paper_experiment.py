"""The experiment state the ai_paper path reads (Plan 3 R20; Plan 4 implements the port).

Until Plan 4 lands, production wires ``NoExperiment``: there is no experiment,
so every decision is refused ``NO_EXPERIMENT``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Protocol

ExperimentState = Literal["ARMED", "PAUSED", "KILLED", "STOPPED"]
EXPERIMENT_STATES = ("ARMED", "PAUSED", "KILLED", "STOPPED")


@dataclass(frozen=True)
class ExperimentView:
    experiment_id: str
    state: ExperimentState

    def __post_init__(self):
        if not isinstance(self.experiment_id, str) or not self.experiment_id:
            raise ValueError("experiment_id must be a non-empty string")
        if self.state not in EXPERIMENT_STATES:
            raise ValueError(f"unknown experiment state {self.state!r}")


class ExperimentStatePort(Protocol):
    def current(self, account_id: str) -> Optional[ExperimentView]: ...


class NoExperiment:
    def current(self, account_id: str) -> Optional[ExperimentView]:
        return None
