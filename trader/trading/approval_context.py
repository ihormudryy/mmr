"""Immutable approval evidence with separate broker and market clocks."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from trader.data.broker_state import BrokerRiskSnapshot
from trader.trading.proposal_command_service import ExecutableQuote, QuoteAuthority

logger = logging.getLogger(__name__)


class BrokerRiskSnapshotAuthority(Protocol):
    def capture(self, account_id: str) -> BrokerRiskSnapshot: ...


class WhatIfMarginAuthority(Protocol):
    def what_if_margin(
        self, conid: int, side: str, quantity: float
    ) -> Optional[dict]: ...


class ApprovalContextError(RuntimeError):
    """A required approval-risk read is unavailable — fail closed."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ExecutableMarketEvidence:
    """Executable quote and its independent receipt clock."""

    quote: ExecutableQuote
    received_at: dt.datetime

    def age_seconds(self, now: dt.datetime) -> float:
        return (now - self.quote.market_timestamp).total_seconds()


@dataclass(frozen=True)
class WhatIfEvidence:
    response: dict
    received_at: dt.datetime


@dataclass(frozen=True)
class ApprovalContext:
    """One immutable decision context assembled from separately fenced inputs."""

    conid: int
    side: str
    broker: BrokerRiskSnapshot
    market: ExecutableMarketEvidence
    what_if: Optional[WhatIfEvidence]

    def notional(self, quantity: float) -> float:
        return abs(float(quantity) * self.market.quote.price)

    def quote_age_seconds(self, now: dt.datetime) -> float:
        return self.market.age_seconds(now)

    def is_quote_fresh(self, now: dt.datetime, max_age_seconds: float) -> bool:
        return self.quote_age_seconds(now) <= max_age_seconds

    # Compatibility properties are intentionally local to this module while
    # callers migrate to the explicit broker/market/what-if subobjects.
    @property
    def account_id(self) -> str:
        return self.broker.account_id

    @property
    def quote(self) -> ExecutableQuote:
        return self.market.quote

    @property
    def net_liquidation(self) -> float:
        return self.broker.net_liquidation

    @property
    def daily_pnl(self) -> float:
        return self.broker.daily_pnl

    @property
    def open_order_count(self) -> int:
        return self.broker.open_order_count

    @property
    def position_value(self) -> float:
        return self.broker.position_value(self.conid)

    @property
    def reducible_quantity(self) -> float:
        return self.broker.reducible_quantity(self.conid)

    @property
    def captured_at(self) -> dt.datetime:
        return self.market.received_at

    @property
    def what_if_margin(self) -> Optional[dict]:
        return None if self.what_if is None else self.what_if.response


def _required(label: str, fn: Callable[[], object]):
    try:
        return fn()
    except ApprovalContextError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failed core read is fail-closed
        raise ApprovalContextError(
            "READ_FAILED", f"{label} read failed: {exc}"
        ) from exc


def capture_approval_context(
    *,
    account_id: str,
    conid: int,
    side: str,
    quantity: float,
    quotes: QuoteAuthority,
    broker: BrokerRiskSnapshotAuthority,
    margin: WhatIfMarginAuthority,
    now: dt.datetime,
) -> ApprovalContext:
    """Capture broker state once, then independently timestamp market evidence."""
    broker_snapshot = _required(
        "broker_risk_snapshot", lambda: broker.capture(account_id)
    )
    if broker_snapshot.account_id != account_id:
        raise ApprovalContextError(
            "ACCOUNT_MISMATCH", "broker snapshot returned a different account"
        )

    quote = _required(
        "executable_quote", lambda: quotes.executable_quote(conid, side=side)
    )
    if quote is None:
        raise ApprovalContextError(
            "NO_QUOTE", f"no executable quote for conid {conid} side {side!r}"
        )

    try:
        what_if_margin = margin.what_if_margin(conid, side, quantity)
    except Exception as exc:  # noqa: BLE001 — mode-aware consumer decides availability policy
        logger.warning("what_if_margin unavailable: %s", exc)
        what_if_margin = None

    return ApprovalContext(
        conid=conid,
        side=side,
        broker=broker_snapshot,
        market=ExecutableMarketEvidence(quote=quote, received_at=now),
        what_if=(
            None
            if what_if_margin is None
            else WhatIfEvidence(response=dict(what_if_margin), received_at=now)
        ),
    )
