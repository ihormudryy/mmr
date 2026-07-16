"""Command-authority wire contract. [M1-F3] Task 3 — FROZEN.

``CommandReceipt`` is the cross-plan interface-freeze contract returned by
every ``TradingCommandCoordinator.execute`` call (new, replay, or conflict)
and by the ``get_command`` read. Field order and count are frozen by the
plan-index cross-plan interface freeze
(``docs/superpowers/plans/2026-07-15-command-center-plan-index.md``): any
downstream plan ([M1-R], [M1-C]) that constructs or destructures this
dataclass positionally breaks if a field is added, removed, renamed, or
reordered.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    correlation_id: str
    state: str
    outcome: Optional[dict[str, Any]]
    error_code: Optional[str]
    retryable: bool
