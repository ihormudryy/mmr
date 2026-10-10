"""The reconciler's evidence for a crashed experiment command (issue #121).

Each of the four experiment commands is one coordinator step whose store transaction writes its command id
with the state change, so the transition row proves the commit. A crashed idempotent no-op (pause when already
PAUSED, resume when already ARMED) wrote no row and is rejected as never committed: fail-safe, the operator
sends it again.
"""
from __future__ import annotations

from typing import Any, Optional

from trader.automation.experiment_service import record_view
from trader.automation.experiments import ExperimentStore

STATE_AFTER_COMMAND = {
    "start_experiment": "ARMED", "pause_experiment": "PAUSED",
    "resume_experiment": "ARMED", "stop_experiment": "STOPPED",
}


class ExperimentCommandEvidence:
    def __init__(self, store: ExperimentStore, config: Any):
        self._store = store
        self._config = config

    def committed_outcome(self, action: str, command_id: str) -> Optional[dict]:
        """The success receipt of the command, rebuilt from the experiment as it is now, or None.

        Raises when the row is not the transition this action writes: that is no proof either way."""
        committed = self._store.committed_transition(command_id)
        if committed is None:
            return None
        if committed.to_state != STATE_AFTER_COMMAND[action]:
            raise ValueError(f"{action} {command_id} wrote a transition to {committed.to_state}")
        return {**record_view(committed.record, self._config), "reconciled": "committed_transition",
                "committed_state": committed.to_state, "committed_revision": committed.revision}
