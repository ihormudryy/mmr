"""The shares a model close is broker-proven to have removed from its round trip (SP2 Plan 2 Ruling 21).

A count is proven only when the close decision is final and every SELL execution of the trip has a known
owner: a model close, the ENTER saga's own stop or target, or a re-protect child of a close root. One SELL
nobody owns makes the count unproven; nothing is guessed.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from trader.scoreboard.ports import CloseFill
from trader.trading.order_correlation import decode_order_ref, liquidation_child_kind, liquidation_child_root

FINAL_STATES = frozenset({"RESOLVED", "REJECTED"})
ENTRY_SAGA_PREFIX = "og-aip-"           # the ENTER's own bracket: og-{command_id}, command ids start aip-
REPROTECT_KINDS = frozenset({"reprotect-stop", "reprotect-target"})
UNPROVEN = CloseFill(False, None)


def _is_known_non_close(order_ref: Optional[str]) -> bool:
    group = decode_order_ref(order_ref)
    if group is None:
        return False
    if group.startswith(ENTRY_SAGA_PREFIX):
        return True
    return liquidation_child_kind(group) in REPROTECT_KINDS and liquidation_child_root(group) is not None


class JournalCloseFills:
    def __init__(self, store: Any, close_links: Any, decisions: Any,
                 executions: Callable[[str], Optional[Sequence[Any]]]):
        self._store = store
        self._links = close_links
        self._decisions = decisions
        self._executions = executions

    def removed(self, round_trip_id: str, close_decision_id: str) -> CloseFill:
        close = self._decisions.get(close_decision_id)
        if close is None or close.state not in FINAL_STATES:
            return UNPROVEN
        executions = self._executions(round_trip_id)
        if executions is None:
            return UNPROVEN
        shares = 0
        for execution in executions:
            if execution.side != "SELL":
                continue
            owner = self._links.close_decision_for_order_ref(execution.order_ref)
            if owner == close_decision_id:
                shares += int(execution.quantity)
            elif owner is None and not _is_known_non_close(execution.order_ref):
                return UNPROVEN
        return CloseFill(True, shares)
