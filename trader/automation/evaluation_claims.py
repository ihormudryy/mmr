"""Evaluation claims: the trader's daily slots for research evaluations (SP2c spec 5.1, 5.2 item 1).

One durable row per request id. A claim is written in one transaction that
checks, in order: the same id (retry or conflict), the id is the body's digest,
the allowlist and cohort size, the strategy key's cooldown, and the New York
day's cap. The clock is read inside that transaction, so the cap, the cooldown
check and the stored row use the New York day on which the lock was taken. The
per-instance lock of the shared ``DuckDBConnection.get_instance`` (one instance
per journal file) serialises claims, so the last slot goes to exactly one
caller; DuckDB's file lock only keeps other processes out. States only move
forward.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.automation.backtest_judge_schema import cooling_until_in_tx
from trader.automation.calendar_policy import ET
from trader.research.evaluation_request import EvaluationRequestBody, canonical_request_json, evaluation_request_id

EVALUATION_REQUEST_CONFLICT = "EVALUATION_REQUEST_CONFLICT"
EVALUATION_REQUEST_ID_MISMATCH = "EVALUATION_REQUEST_ID_MISMATCH"
STRATEGY_NOT_ALLOWED = "STRATEGY_NOT_ALLOWED"
COHORT_TOO_LARGE = "COHORT_TOO_LARGE"
FAMILY_COOLING_DOWN = "FAMILY_COOLING_DOWN"
EVALUATION_LIMIT_REACHED = "EVALUATION_LIMIT_REACHED"
CLAIM_UNKNOWN = "CLAIM_UNKNOWN"
CLAIM_STATE_BACKWARD = "CLAIM_STATE_BACKWARD"
_RANK = {"QUEUED": 0, "RUNNING": 1, "DONE": 2, "FAILED": 2}
_COLUMNS = "request_id, body_json, strategy_key, ny_day, state, principal, claimed_at, updated_at"


def utc(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("trader times must be timezone-aware")
    return moment.astimezone(dt.timezone.utc)


def ny_day(moment: dt.datetime) -> dt.date:
    return utc(moment).astimezone(ET).date()


class ClaimRefused(Exception):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def reply(self) -> dict:
        return {"status": "REFUSED", "code": self.code, "detail": self.detail, "retryable": self.retryable,
                "claim": None}


@dataclass(frozen=True)
class EvaluationClaim:
    request_id: str
    body_json: str
    strategy_key: str
    ny_day: dt.date
    state: str
    principal: str
    claimed_at: dt.datetime
    updated_at: dt.datetime

    def body(self) -> EvaluationRequestBody:
        return EvaluationRequestBody.model_validate(json.loads(self.body_json))

    def to_json(self) -> dict:
        return {"request_id": self.request_id, "strategy_key": self.strategy_key, "ny_day": self.ny_day.isoformat(),
                "state": self.state, "claimed_at": self.claimed_at.isoformat(),
                "updated_at": self.updated_at.isoformat(), "body": json.loads(self.body_json)}


@dataclass(frozen=True)
class ClaimResult:
    status: str   # ACCEPTED | EXISTING | UPDATED | UNCHANGED
    claim: EvaluationClaim

    def reply(self) -> dict:
        return {"status": self.status, "code": None, "detail": None, "retryable": False,
                "claim": self.claim.to_json()}


def claim_row_in_tx(conn: Any, request_id: str) -> Optional[EvaluationClaim]:
    row = conn.execute(f"SELECT {_COLUMNS} FROM evaluation_claims WHERE request_id = ?", [request_id]).fetchone()
    if row is None:
        return None
    return EvaluationClaim(row[0], row[1], row[2], row[3], row[4], row[5], utc(row[6]), utc(row[7]))


class EvaluationClaims:
    def __init__(self, db: Any, *, config: BacktestJudgeConfig, now: Callable[[], dt.datetime]):
        self._db = db
        self._config = config
        self._now = now

    def claim(self, request_id: str, body: EvaluationRequestBody, *, principal: str) -> ClaimResult:
        canonical = canonical_request_json(body)
        expected_id = evaluation_request_id(body)

        def write(conn: Any) -> ClaimResult:
            now = utc(self._now())
            day = ny_day(now)
            existing = claim_row_in_tx(conn, request_id)
            if existing is not None:
                if existing.body_json != canonical:
                    raise ClaimRefused(EVALUATION_REQUEST_CONFLICT, "this request id already names another body")
                return ClaimResult("EXISTING", existing)
            if request_id != expected_id:
                raise ClaimRefused(EVALUATION_REQUEST_ID_MISMATCH, "the request id must be the digest of the body")
            self._check_config(body)
            until = cooling_until_in_tx(conn, body.strategy_key, day)
            if until is not None:
                raise ClaimRefused(FAMILY_COOLING_DOWN,
                                   f"{body.strategy_key} cools down until session {until.isoformat()}")
            used = conn.execute("SELECT COUNT(*) FROM evaluation_claims WHERE ny_day = ?", [day]).fetchone()[0]
            if used >= self._config.evaluations_per_day:
                raise ClaimRefused(EVALUATION_LIMIT_REACHED, f"{used} of {self._config.evaluations_per_day} "
                                                             f"evaluations already claimed on {day.isoformat()}")
            conn.execute(f"INSERT INTO evaluation_claims ({_COLUMNS}) VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                         [request_id, canonical, body.strategy_key, day, principal, now, now])
            return ClaimResult("ACCEPTED", EvaluationClaim(request_id, canonical, body.strategy_key, day, "QUEUED",
                                                           principal, now, now))
        return self._db.transaction(write)

    def _check_config(self, body: EvaluationRequestBody) -> None:
        if not self._config.allows(body.strategy_key):
            raise ClaimRefused(STRATEGY_NOT_ALLOWED,
                               f"{body.strategy_key} is not on ai_paper.backtest_judge.strategy_allowlist")
        if len(body.cohort) > self._config.max_cohort_points:
            raise ClaimRefused(COHORT_TOO_LARGE,
                               f"{len(body.cohort)} points; the limit is {self._config.max_cohort_points}")

    def get(self, request_id: str) -> Optional[EvaluationClaim]:
        return self._db.transaction(lambda conn: claim_row_in_tx(conn, request_id))

    def update(self, request_id: str, state: str) -> ClaimResult:
        if state not in ("RUNNING", "DONE", "FAILED"):
            raise ValueError(f"unknown claim state {state!r}")

        def write(conn: Any) -> ClaimResult:
            now = utc(self._now())
            claim = claim_row_in_tx(conn, request_id)
            if claim is None:
                raise ClaimRefused(CLAIM_UNKNOWN, "the trader holds no claim with this request id")
            if claim.state == state:
                return ClaimResult("UNCHANGED", claim)
            if _RANK[state] <= _RANK[claim.state]:
                raise ClaimRefused(CLAIM_STATE_BACKWARD, f"{claim.state} cannot move to {state}")
            conn.execute("UPDATE evaluation_claims SET state = ?, updated_at = ? WHERE request_id = ?",
                         [state, now, request_id])
            return ClaimResult("UPDATED", replace(claim, state=state, updated_at=now))
        return self._db.transaction(write)
