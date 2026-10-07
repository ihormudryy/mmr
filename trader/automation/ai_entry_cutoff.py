"""Cancel ai_paper entries at the entry cutoff (Plan 3 R26, owner decision).

The calendar stops new entries 30 minutes before the close but cancels
working entries only 25 minutes before. For ai_paper the unfilled rest of
every working AI entry is cancelled at the entry cutoff itself:

1. Cancel at the cutoff, with deterministic child ids, and record ``ISSUED``.
2. An ambiguous cancel (it raised, or the entry still works on a newer broker
   generation) is ``AMBIGUOUS`` and reconciled from later fenced snapshots;
   a still-working entry gets the same cancel again (idempotent at the
   broker). Nothing here sends a new entry or a reduce.
3. When no AI entry works any more (``DONE``), a conid whose working AI stop
   or target is larger than the position is re-protected to the position
   (``LiquidationService`` goal ``reprotect``), never closed.

The 15:35 session cancel and the 15:45 flatten stay as backstops.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from trader.automation.session_controller import SessionController

logger = logging.getLogger(__name__)

AI_GROUP_PREFIX = "og-aip-"
INCIDENT_AMBIGUOUS_CANCEL = "ai_entry_cancel_ambiguous"
_PROTECTIVE_LEGS = ("stop", "take_profit")


def is_ai_entry(order: Any) -> bool:
    return ((getattr(order, "order_group_id", None) or "").startswith(AI_GROUP_PREFIX)
            and getattr(order, "leg", None) == "entry" and not getattr(order, "is_external", False))


def _outstanding(order: Any) -> float:
    return float(order.total_quantity) - float(order.filled_quantity)


def cutoff_child_id(cancel_root: str, order_entity_id: str) -> str:
    """``{cancel_root}-aip-{order_entity_id}``, colon-free: it becomes an ``mmr:`` order ref."""
    return f"{cancel_root}-aip-{order_entity_id.replace(':', '-')}"


def _log_incident(code: str, detail: str) -> None:
    logger.error("%s: %s", code, detail)


class AiEntryCutoff:
    def __init__(self, *, broker: Any, cancel: Any, liquidation: Any, policy: Any, account_id: str,
                 now: Callable[[], dt.datetime], deadline_seconds: float = 300.0,
                 on_incident: Callable[[str, str], None] = _log_incident):
        self._broker = broker
        self._cancel = cancel
        self._liquidation = liquidation
        self._policy = policy
        self._account_id = account_id
        self._now = now
        self._deadline_seconds = deadline_seconds
        self._on_incident = on_incident

    def on_entry_cutoff(self, state: Any, now: dt.datetime) -> None:
        """Called on every session tick from the entry cutoff until the flatten starts."""
        view = self._policy.current()
        if view is None or view.session_date != state.session_date:
            return  # no ai_paper session today: no AI entry was admitted
        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception:
            logger.warning("ai entry cutoff: broker snapshot unavailable; retrying on the next tick")
            return
        root = SessionController.cancel_command_id(self._account_id, state.session_date)
        stage = view.cutoff_cancel_state
        if stage is None:
            stage = self._first_cancel(view.session_date, root, snapshot)
        elif stage in ("ISSUED", "AMBIGUOUS"):
            stage = self._reconcile(view, root, snapshot)
        if stage == "DONE":
            self._reprotect_oversized(root, snapshot)

    # -- rule 1: cancel at the cutoff ------------------------------------------

    def _first_cancel(self, session_date: dt.date, root: str, snapshot: Any) -> str:
        entries = [order for order in snapshot.working_orders if is_ai_entry(order)]
        if not entries:
            return self._record(session_date, "DONE", snapshot)
        if self._send(root, entries):
            self._on_incident(INCIDENT_AMBIGUOUS_CANCEL, f"an AI entry cancel at the cutoff failed ({root})")
            return self._record(session_date, "AMBIGUOUS", snapshot)
        return self._record(session_date, "ISSUED", snapshot)

    # -- rule 2: an ambiguous cancel is reconciled, never guessed ----------------

    def _reconcile(self, view: Any, root: str, snapshot: Any) -> str:
        if snapshot.generation_id <= (view.cutoff_cancel_generation or 0):
            return view.cutoff_cancel_state  # judge only on a broker generation newer than the cancel
        working = [order for order in snapshot.working_orders if is_ai_entry(order)]
        if not working:
            return self._record(view.session_date, "DONE", snapshot)
        self._send(root, [order for order in working if order.status != "PendingCancel"])
        self._on_incident(INCIDENT_AMBIGUOUS_CANCEL,
                          f"{len(working)} AI entries still work after the cutoff cancel ({root})")
        return self._record(view.session_date, "AMBIGUOUS", snapshot)

    def _send(self, root: str, entries: list) -> bool:
        """Cancel each entry; True when any cancel raised (its outcome is unknown)."""
        from trader.trading.liquidation_service import DispatchRefused
        ambiguous = False
        for order in entries:
            try:
                self._cancel.cancel(order, cutoff_child_id(root, order.order_entity_id))
            except DispatchRefused as ex:
                logger.info("AI entry %s needs no cancel: %s", order.order_entity_id, ex)
            except Exception:
                logger.exception("AI entry cancel %s failed", order.order_entity_id)
                ambiguous = True
        return ambiguous

    def _record(self, session_date: dt.date, stage: str, snapshot: Any) -> str:
        self._policy.set_cutoff_cancel_state(session_date, stage, generation=int(snapshot.generation_id))
        return stage

    # -- rule 3: protection stays sized to a partial fill ------------------------

    def _reprotect_oversized(self, root: str, snapshot: Any) -> None:
        from trader.trading.exit_owner import ExitInProgress
        from trader.trading.liquidation_service import LiquidationRefused

        for conid in sorted(self._oversized_conids(snapshot)):
            deadline = self._now() + dt.timedelta(seconds=self._deadline_seconds)
            try:
                self._liquidation.start(self._account_id, f"{root}-aip-reprotect-{conid}", deadline,
                                        scope="conid", conid=conid, goal="reprotect")
            except ExitInProgress:
                continue  # another close owns the conid and its protection
            except LiquidationRefused as ex:
                logger.warning("ai re-protect of conid %s refused: %s", conid, ex)

    @staticmethod
    def _oversized_conids(snapshot: Any) -> set[int]:
        held = {int(p.conid): abs(float(p.quantity)) for p in snapshot.positions if float(p.quantity) != 0.0}
        return {
            int(order.conid) for order in snapshot.working_orders
            if (order.order_group_id or "").startswith(AI_GROUP_PREFIX) and order.leg in _PROTECTIVE_LEGS
            and not order.is_external and int(order.conid) in held and _outstanding(order) > held[int(order.conid)]
        }
