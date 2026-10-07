"""A broker-proven close of one long position through the safe close (Plan 1), shared by the
one-strategy SELL intent and the ai_paper CLOSE / PARTIAL_CLOSE decisions (Plan 3 Task 8).

The proof is a reduction check on a fresh fenced snapshot, not a sizing: the
close sizes every reduce from its own broker generations, and never goes
through ``build_bracket_plan``.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional

CLOSE_PENDING = "CLOSE_PENDING"
DISPATCH_AMBIGUOUS = "DISPATCH_AMBIGUOUS"


@dataclass(frozen=True)
class CloseOutcome:
    state: str  # REJECTED | OUTCOME_UNKNOWN
    error_code: Optional[str]
    outcome: dict = field(default_factory=dict)

    @property
    def close_root_id(self) -> Optional[str]:
        return self.outcome.get("close_root_id")


def _rejected(code: str, outcome: dict) -> CloseOutcome:
    return CloseOutcome("REJECTED", code, outcome)


def start_broker_proven_close(*, liquidation: Any, broker: Any, account_id: str, command_id: str, conid: int,
                              side: str, quantity: Optional[float], deadline: dt.datetime,
                              stop_price: Optional[float] = None,
                              target_price: Optional[float] = None) -> CloseOutcome:
    from trader.trading.exit_owner import ExitInProgress
    from trader.trading.liquidation_service import LiquidationRefused

    try:
        snapshot = broker.capture(account_id)
        if getattr(snapshot, "account_id", None) != account_id:
            raise RuntimeError("broker snapshot is for another account")
    except Exception as ex:
        return _rejected("BROKER_SNAPSHOT_UNAVAILABLE", {"detail": str(ex)})
    held = float(snapshot.reducible_quantity(conid))
    # Long-only: only a SELL of a held long reduces exposure.
    if side != "SELL" or held <= 0 or (quantity is not None and quantity > held):
        return _rejected("NOT_A_REDUCTION", {"held": held, "requested": quantity})
    # A close of the whole position takes the broker quantity at reduce time (Plan 1 ruling 10).
    partial = None if quantity is None or quantity >= held else quantity
    prices = {} if partial is None else {"stop_price": stop_price, "target_price": target_price}
    try:
        receipt = liquidation.start(account_id, command_id, deadline, scope="conid", conid=conid,
                                    quantity=partial, **prices)
    except ExitInProgress as ex:
        return _rejected("EXIT_IN_PROGRESS", {"close_root_id": ex.root_id})
    except LiquidationRefused as ex:
        return _rejected(ex.code, {"detail": str(ex)})
    except Exception as ex:
        return CloseOutcome("OUTCOME_UNKNOWN", DISPATCH_AMBIGUOUS, {"detail": str(ex)})
    return CloseOutcome("OUTCOME_UNKNOWN", CLOSE_PENDING, {
        "close_root_id": receipt.cause_command_id, "liquidation_state": receipt.state,
        "generation_id": receipt.generation_id, "detail": receipt.detail})
