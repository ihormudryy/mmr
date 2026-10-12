"""Refusal codes of the authored-strategy methods (SP3 spec 7.3) and their exception.

A refusal is a reply body, never an RPC error. Kept apart from the store so the submission checks and
the store can both raise it without importing each other.
"""
from __future__ import annotations

AUTHORED_STRATEGIES_DISABLED = "AUTHORED_STRATEGIES_DISABLED"
AUTHORED_CAP_FULL = "AUTHORED_CAP_FULL"
AUTHORED_STATIC_REJECTED = "AUTHORED_STATIC_REJECTED"
AUTHORED_BAD_TUNABLES = "AUTHORED_BAD_TUNABLES"
AUTHORED_BAD_BAR_SIZE = "AUTHORED_BAD_BAR_SIZE"
AUTHORED_ISOLATION_UNPROVEN = "AUTHORED_ISOLATION_UNPROVEN"
AUTHORED_STATE_CONFLICT = "AUTHORED_STATE_CONFLICT"
AUTHORED_STRATEGY_UNKNOWN = "AUTHORED_STRATEGY_UNKNOWN"


class AuthoredRefused(Exception):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def reply(self, **extra) -> dict:
        return {"status": "REFUSED", "code": self.code, "detail": self.detail, "retryable": self.retryable,
                "record": None, **extra}


class AuthoredStateConflict(AuthoredRefused):
    """The record is not in the state the caller read, or the move is not in the state table."""

    def __init__(self, detail: str):
        super().__init__(AUTHORED_STATE_CONFLICT, detail)
