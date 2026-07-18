"""Single-generation approval risk context.

Design: docs/superpowers/specs/2026-07-18-command-plane-activation-design.md
(Phase 5 / sequence step 2a).

Every risk check for an approval — account assertion, notional cap, RiskGate
evaluate, leverage, drift — must read from ONE immutable snapshot captured at a
single instant, rather than re-reading account / positions / quote / margin at
arbitrary times across a multi-second approval->dispatch flow. That removes the
TOCTOU window AND the current "failed read degrades to 0 and proceeds" hazard
(``trading_runtime.py:1323``): a failed CORE read fails the capture **closed**.

This module is read-only — no mutation, no order placement. It reuses the port
protocols already consumed by ``ProposalCommandService`` (``QuoteAuthority``,
``PositionAuthority``) and adds ``BrokerAuthority`` for account/broker state. The
concrete, IB-backed adapters (which read live trader state, loop-safe) land
separately; here is the value object plus the fail-closed assembly, testable
with fake ports.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from trader.trading.proposal_command_service import (
    ExecutableQuote,
    PositionAuthority,
    QuoteAuthority,
)

logger = logging.getLogger(__name__)


class BrokerAuthority(Protocol):
    """Read-only broker/account state needed to risk-assess an approval.

    ``what_if_margin`` returns the projected margin impact of the order, or
    ``None`` when the broker's what-if is unavailable (it legitimately fails);
    leverage is a secondary check the mode-aware consumer gates, so a ``None``
    here does not fail the capture. Every other method is a required core read.
    """

    def is_ready(self) -> bool: ...
    def net_liquidation(self) -> float: ...
    def daily_pnl(self) -> float: ...
    def open_order_count(self) -> int: ...
    def position_value(self, conid: int) -> float: ...
    def what_if_margin(self, conid: int, side: str, quantity: float) -> Optional[dict]: ...


class ApprovalContextError(RuntimeError):
    """A required approval-risk read is unavailable — fail closed.

    Carries a stable ``code`` so the approval saga can surface a specific reject
    (never a silent degrade-to-zero).
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ApprovalContext:
    """Immutable, single-generation snapshot of every risk input for one
    approval. Constructed only by ``capture_approval_context``."""

    account_id: str
    conid: int
    side: str
    quote: ExecutableQuote
    net_liquidation: float
    daily_pnl: float
    open_order_count: int
    position_value: float
    reducible_quantity: float
    captured_at: dt.datetime
    # The whatIfOrder margin-impact dict (shape of RiskGate.check_leverage's
    # input), or None when the broker's what-if is unavailable.
    what_if_margin: Optional[dict] = None

    def notional(self, quantity: float) -> float:
        """Order notional (magnitude): ``|quantity| * quote price``."""
        return abs(float(quantity) * self.quote.price)

    def quote_age_seconds(self, now: dt.datetime) -> float:
        return (now - self.quote.market_timestamp).total_seconds()

    def is_quote_fresh(self, now: dt.datetime, max_age_seconds: float) -> bool:
        """Whether the captured quote is recent enough to act on. A quote older
        than ``max_age_seconds`` is stale; a current/future one is fresh (only
        staleness matters for a trade decision)."""
        return self.quote_age_seconds(now) <= max_age_seconds


def _required(label: str, fn: Callable[[], object]):
    """Run a required read; any failure fails the capture closed."""
    try:
        return fn()
    except ApprovalContextError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failed core read is fail-closed
        raise ApprovalContextError(
            "READ_FAILED", f"{label} read failed: {exc}") from exc


def capture_approval_context(
    *,
    account_id: str,
    conid: int,
    side: str,
    quantity: float,
    quotes: QuoteAuthority,
    positions: PositionAuthority,
    broker: BrokerAuthority,
    now: dt.datetime,
) -> ApprovalContext:
    """Capture every risk input for an approval in ONE pass, fail-closed.

    Each port is read exactly once so every downstream check sees the same
    generation. A missing quote, an un-ready broker, or any failed core read
    raises ``ApprovalContextError`` — the approval must reject, never proceed on
    zero-valued state. Only ``what_if_margin`` is best-effort (``None`` on
    failure); the mode-aware consumer decides whether a missing margin is
    fail-closed (live) or a skipped leverage check (paper).
    """
    if not _required("is_ready", broker.is_ready):
        raise ApprovalContextError(
            "BROKER_NOT_READY", "broker is not ready for approval")

    quote = _required(
        "executable_quote", lambda: quotes.executable_quote(conid, side=side))
    if quote is None:
        raise ApprovalContextError(
            "NO_QUOTE", f"no executable quote for conid {conid} side {side!r}")

    reducible = _required(
        "reducible_quantity",
        lambda: positions.reducible_quantity(account_id, conid))
    net_liq = _required("net_liquidation", broker.net_liquidation)
    daily_pnl = _required("daily_pnl", broker.daily_pnl)
    open_orders = _required("open_order_count", broker.open_order_count)
    position_value = _required("position_value", lambda: broker.position_value(conid))

    try:
        what_if_margin = broker.what_if_margin(conid, side, quantity)
    except Exception as exc:  # noqa: BLE001 — leverage is a secondary, consumer-gated check
        logger.warning("what_if_margin unavailable (leverage check will be "
                       "gated by mode): %s", exc)
        what_if_margin = None

    return ApprovalContext(
        account_id=account_id,
        conid=conid,
        side=side,
        quote=quote,
        net_liquidation=float(net_liq),
        daily_pnl=float(daily_pnl),
        open_order_count=int(open_orders),
        position_value=float(position_value),
        reducible_quantity=float(reducible),
        captured_at=now,
        what_if_margin=(None if what_if_margin is None else dict(what_if_margin)),
    )
