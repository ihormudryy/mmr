"""Attempt journal: every model call is written before it is made and finished after.

Rules: the attempt row exists before the call (STARTED); attempt_key = "<request_key>#<n>";
an unknown outcome never becomes SUCCEEDED; only a late usage report moves UNKNOWN or
COST_UNKNOWN to RECONCILED, once; the stored request has no headers or credentials. Every
finished attempt writes one cost event with a stable id for the reporting outbox.
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from trader.ai.model_client import (
    COST_UNKNOWN, NOT_SENT, REJECTED, UNKNOWN, ModelRequest, ModelResponse, Usage,
)
from trader.ai.store import AiStore, to_utc


STARTED = "STARTED"
SUCCEEDED = "SUCCEEDED"
RECONCILED = "RECONCILED"
FAILURE_STATUSES = frozenset({NOT_SENT, REJECTED, UNKNOWN, COST_UNKNOWN})
LATE_USAGE_STATUSES = frozenset({UNKNOWN, COST_UNKNOWN})
MAX_ERROR_DETAIL = 500

COST_CONFIRMED = "CONFIRMED"
COST_ESTIMATED_UNKNOWN = "ESTIMATED_UNKNOWN"
COST_NONE = "NONE"
COST_CORRECTION = "CORRECTION"


class JournalError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class AttemptRecord:
    attempt_key: str
    request_key: str
    attempt_no: int
    role: str
    backend: str
    model: str
    status: str
    reservation_id: str
    request_json: str
    request_sha256: str
    response_text: Optional[str]
    finish_reason: Optional[str]
    provider_request_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    error_code: Optional[str]
    error_detail: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]


@dataclass(frozen=True)
class CostEvent:
    event_seq: int
    event_id: str
    attempt_key: str
    role: str
    backend: str
    model: str
    kind: str
    cost_micros: int
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    occurred_at: datetime


_ATTEMPT_COLUMNS = (
    "attempt_key, request_key, attempt_no, role, backend, model, status, reservation_id, request_json, "
    "request_sha256, response_text, finish_reason, provider_request_id, input_tokens, output_tokens, "
    "error_code, error_detail, started_at, finished_at"
)
_COST_COLUMNS = ("event_seq, event_id, attempt_key, role, backend, model, kind, cost_micros, "
                 "input_tokens, output_tokens, occurred_at")


def canonical_request_json(request: ModelRequest) -> str:
    """What the model was asked. Excludes attempt_key so the hash is stable across replays."""
    body = {
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
    }
    return json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)


def request_sha256(request_json: str) -> str:
    return hashlib.sha256(request_json.encode("utf-8")).hexdigest()


def _attempt(row: tuple) -> AttemptRecord:
    values = list(row)
    values[17] = to_utc(values[17])
    values[18] = to_utc(values[18]) if values[18] is not None else None
    return AttemptRecord(*values)


def _cost_event(row: tuple) -> CostEvent:
    values = list(row)
    values[10] = to_utc(values[10])
    return CostEvent(*values)


class AttemptJournal:
    def __init__(self, store: AiStore):
        self.store = store

    def begin_in_tx(self, conn: Any, *, request: ModelRequest, role: str, backend: str, model: str,
                    reservation_id: str, now: datetime) -> AttemptRecord:
        top = conn.execute("SELECT COALESCE(MAX(attempt_no), 0) FROM ai_model_attempts WHERE request_key = ?",
                           [request.request_key]).fetchone()[0]
        attempt_no = top + 1
        attempt_key = f"{request.request_key}#{attempt_no}"
        request_json = canonical_request_json(request)
        conn.execute(
            "INSERT INTO ai_model_attempts (attempt_key, request_key, attempt_no, role, backend, model, status, "
            "reservation_id, request_json, request_sha256, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [attempt_key, request.request_key, attempt_no, role, backend, model, STARTED, reservation_id,
             request_json, request_sha256(request_json), now])
        return self.get_in_tx(conn, attempt_key)

    def get_in_tx(self, conn: Any, attempt_key: str) -> Optional[AttemptRecord]:
        row = conn.execute(f"SELECT {_ATTEMPT_COLUMNS} FROM ai_model_attempts WHERE attempt_key = ?",
                           [attempt_key]).fetchone()
        return _attempt(row) if row else None

    def _require_status(self, conn: Any, attempt_key: str, allowed: frozenset[str]) -> AttemptRecord:
        record = self.get_in_tx(conn, attempt_key)
        if record is None:
            raise JournalError("ATTEMPT_UNKNOWN", attempt_key)
        if record.status not in allowed:
            raise JournalError("ATTEMPT_STATE", f"{attempt_key} is {record.status}")
        return record

    def finish_success_in_tx(self, conn: Any, attempt_key: str, response: ModelResponse, now: datetime) -> None:
        self._require_status(conn, attempt_key, frozenset({STARTED}))
        conn.execute(
            "UPDATE ai_model_attempts SET status = ?, response_text = ?, finish_reason = ?, provider_request_id = ?, "
            "input_tokens = ?, output_tokens = ?, finished_at = ? WHERE attempt_key = ?",
            [SUCCEEDED, response.text, response.finish_reason, response.provider_request_id,
             response.usage.input_tokens, response.usage.output_tokens, now, attempt_key])

    def finish_failure_in_tx(self, conn: Any, attempt_key: str, *, outcome: str, error_code: str,
                             error_detail: str, now: datetime) -> None:
        if outcome not in FAILURE_STATUSES:
            raise JournalError("OUTCOME_INVALID", outcome)
        self._require_status(conn, attempt_key, frozenset({STARTED}))
        conn.execute("UPDATE ai_model_attempts SET status = ?, error_code = ?, error_detail = ?, finished_at = ? "
                     "WHERE attempt_key = ?", [outcome, error_code, error_detail[:MAX_ERROR_DETAIL], now, attempt_key])

    def mark_started_as_unknown_in_tx(self, conn: Any, now: datetime) -> list[AttemptRecord]:
        """Restart only. A process that died mid-call cannot know what the provider did."""
        rows = conn.execute(f"SELECT {_ATTEMPT_COLUMNS} FROM ai_model_attempts WHERE status = ? ORDER BY started_at",
                            [STARTED]).fetchall()
        conn.execute("UPDATE ai_model_attempts SET status = ?, error_code = 'PROCESS_RESTARTED', finished_at = ? "
                     "WHERE status = ?", [UNKNOWN, now, STARTED])
        return [self.get_in_tx(conn, _attempt(row).attempt_key) for row in rows]

    def reconcile_late_usage_in_tx(self, conn: Any, attempt_key: str, usage: Usage, now: datetime) -> bool:
        """UNKNOWN or COST_UNKNOWN -> RECONCILED, once. A repeat with the same numbers is a no-op."""
        record = self._require_status(conn, attempt_key, LATE_USAGE_STATUSES | {RECONCILED})
        if record.status == RECONCILED:
            if (record.input_tokens, record.output_tokens) == (usage.input_tokens, usage.output_tokens):
                return False
            raise JournalError("LATE_USAGE_CONFLICT", attempt_key)
        conn.execute("UPDATE ai_model_attempts SET status = ?, input_tokens = ?, output_tokens = ?, finished_at = ? "
                     "WHERE attempt_key = ?", [RECONCILED, usage.input_tokens, usage.output_tokens, now, attempt_key])
        return True

    def add_cost_event_in_tx(self, conn: Any, *, attempt: AttemptRecord, kind: str, cost_micros: int,
                             usage: Optional[Usage], now: datetime) -> None:
        conn.execute(
            "INSERT INTO ai_cost_events (event_id, attempt_key, role, backend, model, kind, cost_micros, "
            "input_tokens, output_tokens, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [f"{attempt.attempt_key}:{kind}", attempt.attempt_key, attempt.role, attempt.backend, attempt.model,
             kind, cost_micros, usage.input_tokens if usage else None, usage.output_tokens if usage else None, now])

    def get(self, attempt_key: str) -> Optional[AttemptRecord]:
        return self.store.transaction(lambda conn: self.get_in_tx(conn, attempt_key))

    async def aget(self, attempt_key: str) -> Optional[AttemptRecord]:
        return await self.store.atransaction(lambda conn: self.get_in_tx(conn, attempt_key))

    def attempts_with_prefix(self, prefix: str) -> list[AttemptRecord]:
        rows = self.store.db.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM ai_model_attempts WHERE starts_with(request_key, ?) "
            "ORDER BY request_key, attempt_no", [prefix], fetch="all")
        return [_attempt(row) for row in rows]

    def unknown_attempts(self) -> list[AttemptRecord]:
        rows = self.store.db.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM ai_model_attempts WHERE status IN ('UNKNOWN', 'COST_UNKNOWN') "
            "ORDER BY started_at", None, fetch="all")
        return [_attempt(row) for row in rows]

    async def acost_events_after(self, event_seq: int, limit: int = 100) -> list[CostEvent]:
        """For the reporting outbox (Plan 5). Cursor = last seen event_seq; gaps are normal."""
        rows = await self.store.aquery(
            f"SELECT {_COST_COLUMNS} FROM ai_cost_events WHERE event_seq > ? ORDER BY event_seq LIMIT ?",
            [event_seq, limit])
        return [_cost_event(row) for row in rows]
