"""Ledger steps shared by the automated command services (moved from automated_intent_command)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Optional

from trader.domain.commands import CommandReceipt
from trader.trading.command_coordinator import CommandRequest

# Runs inside the transition's own journal transaction, on the same connection.
ExtraWrite = Callable[[Any], None]


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class CommandSteps:
    def __init__(self, *, ledger: Any, journal: Any, controls: Any, account_id: str,
                 now: Callable[[], dt.datetime]):
        self._ledger = ledger
        self._journal = journal
        self._controls = controls
        self._account_id = account_id
        self._now = now

    def now_utc(self) -> dt.datetime:
        return _as_utc(self._now())

    def transition(self, cmd: CommandRequest, from_state: str, to_state: str, *,
                   outcome: Optional[dict[str, Any]] = None, error_code: Optional[str] = None,
                   extra: Optional[ExtraWrite] = None) -> None:
        from trader.trading.command_coordinator import _command_updated_mutation

        now = self.now_utc()

        def _write(conn, _revision: int) -> None:
            self._ledger.transition_in_tx(
                conn, cmd.command_id, from_state, to_state,
                outcome=outcome, error_code=error_code, now=now,
            )
            if extra is not None:
                extra(conn)

        self._journal.mutate(
            self._journal.connect(),
            _command_updated_mutation(cmd, to_state, now, outcome=outcome, error_code=error_code),
            _write,
            event_id=f"command:{cmd.command_id}:{to_state.lower()}",
        )

    def claim(self, cmd: CommandRequest, *, require_unpaused: bool, extra: Optional[ExtraWrite] = None) -> None:
        """VALIDATED -> SUBMITTING in one journal transaction; an entry also checks the pause."""
        from trader.trading.command_coordinator import _command_updated_mutation, _noop_write

        def claim(conn, append):
            if require_unpaused:
                self._controls.require_unpaused_in_tx(conn, self._account_id)
            self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
            if extra is not None:
                extra(conn)
            append(
                _command_updated_mutation(cmd, "SUBMITTING", self.now_utc()),
                _noop_write,
                f"command:{cmd.command_id}:submitting",
            )
        self._journal.mutate_batch_work(self._journal.connect(), claim)

    @staticmethod
    def receipt(command_id: str, state: str, error_code: Optional[str], retryable: bool, *,
                outcome: Optional[dict[str, Any]] = None) -> CommandReceipt:
        return CommandReceipt(
            command_id=command_id,
            correlation_id=command_id,
            state=state,
            outcome=outcome,
            error_code=error_code,
            retryable=retryable,
        )
