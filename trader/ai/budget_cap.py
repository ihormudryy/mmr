"""The owner's daily model cap, read from the trader (trader.yaml), never from ai.yaml (SP2 Plan 5 Ruling 19)."""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

from trader.ai.budget import window_date
from trader.ai.config import usd_to_micros_floor
from trader.ai.gateway import CallRefused

logger = logging.getLogger(__name__)
CAP_METHOD = "get_ai_model_budget"
CAP_SOURCE = "trader.yaml"


def parse_cap_reply(reply: Any) -> float:
    if not isinstance(reply, dict) or set(reply) != {"model_budget_usd_per_day", "source"}:
        raise ValueError("CAP_REPLY_SHAPE")
    value = reply["model_budget_usd_per_day"]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("CAP_REPLY_VALUE")
    if reply["source"] != CAP_SOURCE:
        raise ValueError("CAP_REPLY_SOURCE")
    return float(value)


class BudgetCapSync:
    def __init__(self, *, supervisor: Any, budget: Any, clock: Any):
        self._supervisor, self._budget, self._clock = supervisor, budget, clock
        self._synced_window: Optional[str] = None
        self.last_error: Optional[str] = "NOT_READ_YET"

    async def sync(self) -> bool:
        """One read and one set_cap. Any failure closes the gate until a later read succeeds."""
        try:
            value = parse_cap_reply(await self._supervisor.call(CAP_METHOD, {}))
            outcome = await self._budget.set_cap(usd_to_micros_floor(value))
        except Exception as exc:              # RpcNotSent, RpcOutcomeUnknown, RpcRefused, a bad reply
            self._synced_window, self.last_error = None, getattr(exc, "code", None) or str(exc) or type(exc).__name__
            logger.error("owner budget cap not read (%s): no new model calls until it is", self.last_error)
            return False
        self._synced_window, self.last_error = window_date(self._clock.now()), None
        logger.info("owner budget cap %.2f USD/day applied: %s", value, outcome)
        return True

    def ready(self) -> bool:
        return self._synced_window is not None and self._synced_window == window_date(self._clock.now())


class CapGatedGateway:
    """The ModelCaller the engine and the controller use: no model call without a current owner cap."""

    def __init__(self, gateway: Any, cap: BudgetCapSync):
        self._gateway, self._cap = gateway, cap
        self.budget, self.journal = gateway.budget, gateway.journal

    def new_deadline(self, label: str = ""):
        return self._gateway.new_deadline(label)

    def ready(self) -> bool:
        """False while the owner cap is unread or from another window: every call would be refused."""
        return self._cap.ready()

    async def call(self, role, request, deadline):
        if not self._cap.ready():
            raise CallRefused("BUDGET_CAP_UNKNOWN", self._cap.last_error or "the cap is from another window")
        return await self._gateway.call(role, request, deadline)
