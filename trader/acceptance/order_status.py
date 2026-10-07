"""Which broker order rows can still fill (review #35).

An allowlist: only a terminal status, or a row the broker no longer lists as
open (deleted by a promoted generation), proves an order cannot fill. Any
other status, unknown or missing ones included, blocks "flat".
"""
from __future__ import annotations

from typing import Any, Mapping

TERMINAL_STATUSES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})


def may_still_fill(order: Mapping[str, Any]) -> bool:
    return order.get("deleted") is not True and order.get("status") not in TERMINAL_STATUSES
