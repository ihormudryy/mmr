"""The experiment state the ai_paper path reads (Plan 3 R20; Plan 4 implements the port).

``ExperimentStateReader`` maps the newest experiment of the account to an
``ExperimentView``. Its ``entry_block`` (Plan 4 K19) names why an ``ENTER``
is refused even while the experiment is ``ARMED``: ``BOTH_MODES_ARMED``
(K17), ``EXPERIMENT_MONITOR_NOT_READY`` or ``KILL_LINE_UNKNOWN`` (K7).
Reductions ignore it. ``NoExperiment`` stays for tests and for a stack
without experiments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Protocol

ExperimentState = Literal["ARMED", "PAUSED", "KILLED", "STOPPED"]
EXPERIMENT_STATES = ("ARMED", "PAUSED", "KILLED", "STOPPED")


@dataclass(frozen=True)
class ExperimentView:
    experiment_id: str
    state: ExperimentState
    entry_block: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.experiment_id, str) or not self.experiment_id:
            raise ValueError("experiment_id must be a non-empty string")
        if self.state not in EXPERIMENT_STATES:
            raise ValueError(f"unknown experiment state {self.state!r}")
        if self.entry_block is not None and (not isinstance(self.entry_block, str) or not self.entry_block):
            raise ValueError("entry_block must be None or a non-empty code")


class ExperimentStatePort(Protocol):
    def current(self, account_id: str) -> Optional[ExperimentView]: ...


class NoExperiment:
    def current(self, account_id: str) -> Optional[ExperimentView]:
        return None


class ExperimentStateReader:
    """Plan 4's real port: the experiment store plus the kill monitor's entry block."""

    def __init__(self, store: Any, monitor: Any, mode_conflict: Callable[[], Optional[str]] = lambda: None):
        self._store = store
        self._monitor = monitor
        self._mode_conflict = mode_conflict

    def current(self, account_id: str) -> Optional[ExperimentView]:
        if account_id != self._store.account_id:
            return None
        record = self._store.latest()
        if record is None:
            return None
        block = self._mode_conflict() or self._monitor.entry_block(record)
        return ExperimentView(record.experiment_id, record.state, entry_block=block)


def experiment_entry_refusal(reader: ExperimentStatePort, account_id: str) -> Optional[str]:
    """K20: the dispatch gate's answer for an ai_paper entry. None lets it through."""
    view = reader.current(account_id)
    if view is None:
        return "NO_EXPERIMENT"
    if view.state != "ARMED":
        return "EXPERIMENT_NOT_ARMED"
    return view.entry_block
