"""record_shadow_result (SP2c spec 7): one sealed forward-replay row per judgment and session, research only.

A shadow row is a what-if result of the same backtester. It has no link to an order, a position or the
real account, and the real scoreboard never reads it as equity.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError, model_validator

from trader.research.canonical import sha256_digest
from trader.research.shadow_window import is_xnys_session, session_close_utc, shadow_window, xnys_sessions
from trader.scoreboard.seal import row_digest
from trader.scoreboard.store import ScoreboardConflict, ScoreboardStore, row_key

RESEARCH = "research"
SHADOW_RESULT_DOMAIN = "mmr.shadow-result.v1"
SHADOW_BODY_DOMAIN = "mmr.shadow-result-body.v1"
ROW_TAMPERED = "ROW_TAMPERED"

_Money = Field(default=None, allow_inf_nan=False)


class RecordShadowResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    judgment_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    case_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verdict: Literal["DEPLOY", "SHADOW", "REJECT"]
    session_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["COMPLETE", "INCOMPLETE"]
    reason: Optional[str] = Field(default=None, max_length=200)
    pnl_usd: Optional[StrictFloat] = _Money
    fees_usd: Optional[StrictFloat] = _Money
    trades: Optional[StrictInt] = Field(default=None, ge=0)
    end_equity_usd: Optional[StrictFloat] = _Money
    bar_size: str = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _status_matches_numbers(self) -> "RecordShadowResultRequest":
        numbers = (self.pnl_usd, self.fees_usd, self.trades, self.end_equity_usd)
        if self.status == "COMPLETE" and (any(n is None for n in numbers) or self.reason is not None):
            raise ValueError("a COMPLETE row carries pnl, fees, trades and end equity, and no reason")
        if self.status == "INCOMPLETE" and (not (self.reason or "").strip() or any(n is not None for n in numbers)):
            raise ValueError("an INCOMPLETE row carries a reason and no numbers")
        try:
            dt.date.fromisoformat(self.session_date)
        except ValueError:
            raise ValueError("session_date is not a calendar date") from None
        return self

    def digest(self) -> str:
        return "sha256:" + sha256_digest(SHADOW_BODY_DOMAIN, self.model_dump())


def shadow_record_id(judgment_id: str, session_date: str) -> str:
    return "sha256:" + sha256_digest(SHADOW_RESULT_DOMAIN, {"judgment_id": judgment_id,
                                                           "session_date": session_date})


def _reply(status: str, record_id: Optional[str] = None, code: Optional[str] = None,
           detail: Optional[str] = None, retryable: bool = False) -> dict:
    return {"status": status, "record_id": record_id, "code": code, "detail": detail, "retryable": retryable}


class ShadowIngest:
    """``judgments`` is Plan 1's ``BacktestJudgments`` (``get`` raises ``JudgmentRefused`` on a tampered row) and
    ``versions`` Plan 2's ``AiDeploymentVersionStore``; both errors reach the caller as loud RPC errors."""

    def __init__(self, *, store: ScoreboardStore, judgments: Any, versions: Any, config: Any,
                 now: Callable[[], dt.datetime]):
        self._store, self._judgments, self._versions, self._config, self._now = store, judgments, versions, config, now

    def record(self, request: RecordShadowResultRequest, caller: Any) -> dict:
        if caller.principal != RESEARCH:
            return _reply("REFUSED", code="PRINCIPAL_FORBIDDEN", detail="only research records shadow rows")
        judgment = self._judgments.get(request.judgment_id)
        if judgment is None:
            return _reply("REFUSED", code="JUDGMENT_UNKNOWN", detail=request.judgment_id, retryable=True)
        if judgment.case_digest != request.case_digest or judgment.verdict != request.verdict:
            return _reply("REFUSED", code="SHADOW_JUDGMENT_MISMATCH", detail="case or verdict differs")
        if request.bar_size != judgment.binding["bar_size"]:
            return _reply("REFUSED", code="SHADOW_BAR_SIZE_MISMATCH",
                          detail=f"the judged case uses {judgment.binding['bar_size']!r}")
        first, last = shadow_window(judgment.recorded_at, judgment.verdict,
                                    deploy_expiry_sessions=self._config.deploy_expiry_sessions,
                                    cooldown_until_session=judgment.cooldown_until_session)
        session = dt.date.fromisoformat(request.session_date)
        if not first <= session <= last or not is_xnys_session(session):
            return _reply("REFUSED", code="SHADOW_SESSION_OUTSIDE_WINDOW", detail=f"window {first}..{last}")
        if session_close_utc(session) > self._now():
            return _reply("REFUSED", code="SHADOW_SESSION_NOT_CLOSED", detail=f"{session} has not closed",
                          retryable=True)
        record_id = shadow_record_id(request.judgment_id, request.session_date)
        row = {**request.model_dump(), "record_id": record_id, "session_date": session,
               "deployment_version": self._deployment_version(request), "body_digest": request.digest(),
               "recorded_at": self._now()}
        try:
            return _reply(self._store.ingest_sealed_many([("shadow_results", row)]), record_id)
        except ScoreboardConflict as conflict:
            return _reply("REFUSED", record_id, "CONFLICTING_DUPLICATE", str(conflict))

    def _deployment_version(self, request: RecordShadowResultRequest) -> Optional[str]:
        """Set by the first insert of a DEPLOY row; a repeat is a no-op, so a later registration changes nothing."""
        if request.verdict != "DEPLOY" or self._versions is None:
            return None
        return self._versions.version_for_judgment(request.judgment_id)


def owed_shadow_sessions(judgments: Any, *, deploy_expiry_sessions: int, now: dt.datetime) -> dict[str, dict]:
    """judgment_id -> its verdict and every session of its window that has closed: the rows a shadow book needs
    before it may read COMPLETE. Same window and close rule as ``ShadowIngest.record``."""
    owed: dict[str, dict] = {}
    for judgment in judgments.tracked():
        first, last = shadow_window(judgment.recorded_at, judgment.verdict,
                                    deploy_expiry_sessions=deploy_expiry_sessions,
                                    cooldown_until_session=judgment.cooldown_until_session)
        owed[judgment.judgment_id] = {
            "verdict": judgment.verdict,
            "sessions": tuple(day for day in xnys_sessions(first, last) if session_close_utc(day) <= now)}
    return owed


def _body_of(row: Mapping[str, Any]) -> RecordShadowResultRequest:
    return RecordShadowResultRequest(
        judgment_id=row["judgment_id"], case_digest=row["case_digest"], verdict=row["verdict"],
        session_date=row["session_date"].isoformat(), status=row["status"], reason=row["reason"],
        pnl_usd=row["pnl_usd"], fees_usd=row["fees_usd"], trades=row["trades"],
        end_equity_usd=row["end_equity_usd"], bar_size=row["bar_size"])


def _is_intact(row: Mapping[str, Any], sealed_digest: Optional[str]) -> bool:
    """The body digest, the record id and the stored full-row seal (every column) must all hold."""
    try:
        body = _body_of(row)
    except (ValidationError, TypeError, KeyError, AttributeError):
        return False
    return (body.digest() == row["body_digest"]
            and row["record_id"] == shadow_record_id(row["judgment_id"], body.session_date)
            and sealed_digest is not None and row_digest(row) == sealed_digest)


def verified_shadow_rows(store: ScoreboardStore) -> tuple[list[dict], list[dict]]:
    """Every shadow row, re-checked on read against its body digest and its sealed row digest. An edited row
    stays visible as INCOMPLETE (``ROW_TAMPERED``), never as a number a book could add up; the second list
    names those rows."""
    sealed = store.sealed_digests("shadow_results")
    checked: list[dict] = []
    tampered: list[dict] = []
    for row in store.fetch("shadow_results", {}):
        if _is_intact(row, sealed.get(row_key(row, ("record_id",)))):
            checked.append(dict(row))
            continue
        tampered.append({"record_id": row["record_id"], "judgment_id": row["judgment_id"]})
        checked.append({**row, "status": "INCOMPLETE", "reason": ROW_TAMPERED, "pnl_usd": None, "fees_usd": None,
                        "trades": None, "end_equity_usd": None})
    return checked, tampered
