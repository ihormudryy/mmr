"""What research tells the trader about an authored evaluation (SP3 spec 7.6, 8; Plan 4 rulings 6-11).

The end of a run stopped by the sandbox is kept in ``research_requests.code`` / ``detail`` before anything is
sent, so a restart resends the report and never evaluates the file again.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.sandbox.protocol import SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR

AUTHORED_END_CODE = "AUTHORED_ENDED"
STARTED = "STARTED"
SANDBOX_ERROR_EVENT = "SANDBOX_ERROR"
SANDBOX_UNAVAILABLE_EVENT = "SANDBOX_UNAVAILABLE"
STRATEGY_FAULT_CODES = (SANDBOX_ERROR, SANDBOX_DIGEST_MISMATCH)


def started_body(request_id: str, digest: str, image_digest: str) -> dict:
    """Plan 3's report_authored_evaluation STARTED body; its shape wins."""
    return {"digest": digest, "request_id": request_id, "event": STARTED, "sandbox_image_digest": image_digest,
            "detail": ""}


@dataclass(frozen=True)
class AuthoredEnd:
    event: str          # SANDBOX_ERROR (REJECTED) or SANDBOX_UNAVAILABLE (back to STORED)
    detail: str         # a code or an exception type, never free text

    def marker(self) -> str:
        return f"{self.event}|{self.detail}"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Optional["AuthoredEnd"]:
        if row.get("code") != AUTHORED_END_CODE:
            return None
        event, detail = str(row["detail"]).split("|", 1)
        return cls(event, detail)

    def report_body(self, request_id: str, digest: str) -> dict:
        """Plan 3's report_authored_evaluation body for an end; its shape wins."""
        return {"digest": digest, "request_id": request_id, "event": self.event, "sandbox_image_digest": None,
                "detail": self.detail}


class AuthoredEvaluationEnded(Exception):
    def __init__(self, end: AuthoredEnd):
        super().__init__(f"{end.event}: {end.detail}")
        self.end = end


def sandbox_cause(exc: BaseException) -> Optional[Exception]:
    """The sandbox error behind ``exc``: the SP2c evaluation wraps backtest failures in EvaluationError."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (SandboxFault, SandboxUnavailable)):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def end_for(cause: Exception) -> AuthoredEnd:
    """Only a fault of the strategy (SANDBOX_ERROR, SANDBOX_DIGEST_MISMATCH) rejects the file; everything else on
    the sandbox side is SANDBOX_UNAVAILABLE (Plan 4 rulings 8, 9)."""
    if isinstance(cause, SandboxFault) and cause.code in STRATEGY_FAULT_CODES:
        return AuthoredEnd(SANDBOX_ERROR_EVENT, cause.code)
    return AuthoredEnd(SANDBOX_UNAVAILABLE_EVENT, type(cause).__name__)
