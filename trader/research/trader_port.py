"""The research service's signed calls to the trader (spec 5.1: research may call these methods only)."""
from __future__ import annotations

from typing import Any, Optional

from trader.messaging.typed_rpc import TypedRpcError, TypedRpcRemoteError

TIMEOUT_SECONDS = 15.0
# The trader refuses with this code before any handler runs, so a retry can never act twice.
NOT_STARTED_CODE = "SERVER_BUSY"


class TraderUnavailable(Exception):
    """The call did not complete: the trader may or may not have acted (read back before any retry)."""


class TraderPort:
    def __init__(self, query_client: Any, command_client: Any):
        self._query, self._command = query_client, command_client

    @staticmethod
    def _call(client: Any, method: str, body: dict) -> dict:
        try:
            return client.call(method, body, dict, timeout=TIMEOUT_SECONDS)
        except TypedRpcRemoteError as exc:
            if exc.code == NOT_STARTED_CODE:
                raise TraderUnavailable(f"{method}: {exc.code}") from exc
            raise                                   # any other typed refusal is a bug here: fail loudly
        except (ConnectionError, TimeoutError, OSError, TypedRpcError) as exc:
            raise TraderUnavailable(f"{method}: {type(exc).__name__}") from exc

    def claim(self, request_id: str, body: dict) -> dict:
        return self._call(self._command, "claim_evaluation", {"request_id": request_id, "body": body})

    def claim_readback(self, request_id: str) -> Optional[dict]:
        return self._call(self._query, "get_evaluation_claim", {"request_id": request_id})["claim"]

    def update_claim(self, request_id: str, state: str) -> dict:
        return self._call(self._command, "update_evaluation_claim", {"request_id": request_id, "state": state})

    def judgment(self, *, judgment_id: Optional[str] = None, case_digest: Optional[str] = None) -> Optional[dict]:
        """Plan 1's view, by judgment id or by case digest (Plan 1 sends both keys, exactly one set)."""
        if (judgment_id is None) == (case_digest is None):
            raise ValueError("name exactly one of judgment_id or case_digest")
        body = {"judgment_id": judgment_id, "case_digest": case_digest}
        return self._call(self._query, "get_backtest_judgment", body)["judgment"]

    def record_shadow(self, body: dict) -> dict:
        return self._call(self._command, "record_shadow_result", body)
