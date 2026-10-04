"""P5 — react to breaker trips by restricting active allocation authority.

Automatic path may only reduce/suspend. Reinstatement requires a new signed
operator authority (never restored by restart or breaker reset alone).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from trader.promotion.allocation_attestation import STAGE_CANARY
from trader.promotion.degradation_monitor import DegradationAction, DegradationMonitor
from trader.promotion.evidence_store import EvidenceWindow

logger = logging.getLogger(__name__)

__all__ = [
    "react_to_breaker_trip",
]


def _minimal_breaker_window(*, strategy_id: str, as_of: dt.datetime, reason: str) -> EvidenceWindow:
    return EvidenceWindow(
        strategy_id=strategy_id,
        as_of=as_of,
        window_reset_at=None,
        first_event_at=as_of,
        last_event_at=as_of,
        calendar_days=(),
        session_ids=(),
        round_trip_ids=(),
        instrument_ids=(),
        corrections=(),
        breaker_trips=({"incident_id": "breaker_trip", "detail": reason},),
        cost_breaches=(),
        drawdown_breaches=(),
        stale=False,
        event_count=1,
    )


def react_to_breaker_trip(
    authority_store: Any,
    *,
    account_id: str,
    breaker_state: Any,
    now: Optional[dt.datetime] = None,
    monitor: Optional[DegradationMonitor] = None,
) -> Optional[Any]:
    """On CLEAR→TRIPPED, suspend active allocation authority for ``account_id``.

    Returns the override record when one was written, else ``None``. Failures
    are logged and swallowed so breaker persistence is never blocked.
    """
    try:
        resolved_now = now or dt.datetime.now(dt.timezone.utc)
        if resolved_now.tzinfo is None:
            resolved_now = resolved_now.replace(tzinfo=dt.timezone.utc)

        active = authority_store.active_for_account(account_id, now=resolved_now)
        if active is None:
            return None

        reason = getattr(breaker_state, "reason_code", None) or getattr(breaker_state, "state", "TRIPPED")
        decision = (monitor or DegradationMonitor()).evaluate(
            _minimal_breaker_window(
                strategy_id=active.strategy_id,
                as_of=resolved_now,
                reason=str(reason),
            ),
            active.stage or STAGE_CANARY,
            current_max_gross=float(active.max_gross_allocation),
        )
        if not decision.requires_override:
            return None

        stage = decision.recommended_stage or active.stage
        max_gross = (
            0.0
            if decision.action in (DegradationAction.SUSPEND, DegradationAction.RETIRE)
            else float(decision.recommended_max_gross or 0.0)
        )
        return authority_store.apply_override(
            account_id,
            active.artifact_digest,
            new_stage=stage,
            new_max_gross=max_gross,
            reason=f"breaker trip: {reason}",
            now=resolved_now,
        )
    except Exception:
        logger.exception(
            "allocation degradation override failed after breaker trip for account %s",
            account_id,
        )
        return None
