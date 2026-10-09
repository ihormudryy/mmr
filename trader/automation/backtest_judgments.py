"""Durable Jev judgments and the strategy-key cooldown (SP2c spec 5.2 items 2-3, 6.1).

``ai_research`` records one judgment per evaluation case. The trader is the
authority: it verifies the signed case itself, offers DEPLOY only when the case
qualifies (rules first), keeps one judgment per case and per evaluation, and
starts the strategy key's cooldown on REJECT. Rows are sealed: every read
recomputes the record digest. File reads, signature checks and the renewal
port run before the write transaction. The clock that sets ``recorded_at`` and
the REJECT cooldown is read inside it, so a write that waited for the lock across
New York midnight counts from the day it committed.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from exchange_calendars.errors import DateOutOfBounds

from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.automation.backtest_judge_wire import RecordBacktestJudgmentRequest
from trader.automation.evaluation_claims import claim_row_in_tx, ny_day, utc
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.evaluation_case import (
    CaseRefused, EvaluationCase, initial_deploy_allowed, load_case_verify_keys, load_verified_case, offered_menu,
    renewal_forward_complete,
)
from trader.research.shadow_window import TRACKED_VERDICTS
from trader.research.strategy_key import split_strategy_key

JUDGMENT_DOMAIN = "mmr.backtest-judgment.v1"
JUDGMENT_BODY_DOMAIN = "mmr.backtest-judgment-body.v1"
MAX_DECIDED_AHEAD = dt.timedelta(minutes=5)
JUDGMENT_CONFLICT = "JUDGMENT_CONFLICT"
JUDGMENT_TAMPERED = "JUDGMENT_TAMPERED"
COOLDOWN_CALENDAR_UNAVAILABLE = "COOLDOWN_CALENDAR_UNAVAILABLE"
_COLUMNS = ("judgment_id, case_digest, request_id, kind, verdict, strategy_key, body_json, body_digest, "
            "binding_json, cooldown_until_session, recorded_at, record_digest")


class JudgmentRefused(Exception):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def reply(self, judgment_id: str) -> dict:
        return {"status": "REFUSED", "judgment_id": judgment_id, "code": self.code, "detail": self.detail,
                "retryable": self.retryable, "verdict": None, "cooldown_until_session": None}


@dataclass(frozen=True)
class RenewalStatus:
    refusal_code: Optional[str] = None       # refuse the whole judgment (unknown version, other binding)
    deploy_block_code: Optional[str] = None  # refuse only a DEPLOY (bundle expired, line ended)
    detail: str = ""


class RenewalChecks(Protocol):
    def status(self, case: EvaluationCase, *, now: dt.datetime) -> RenewalStatus: ...


class NoRenewalVersions:
    """Plans 1-4 judge no renewal; SP2c Plan 5 (Renewal) supplies the real checks."""

    def status(self, case: EvaluationCase, *, now: dt.datetime) -> RenewalStatus:
        return RenewalStatus(refusal_code="RENEWAL_NOT_SUPPORTED", detail="renewal arrives with SP2c Plan 5")


def nth_session_after(calendar: Any, day: dt.date, sessions: int) -> dt.date:
    """The ``sessions``-th XNYS session strictly after ``day``."""
    if sessions < 1:
        raise ValueError("sessions must be >= 1")
    try:
        found = calendar.sessions_in_range(day + dt.timedelta(days=1), day + dt.timedelta(days=2 * sessions + 14))
    except DateOutOfBounds as exc:
        raise JudgmentRefused(COOLDOWN_CALENDAR_UNAVAILABLE, str(exc)) from None
    if len(found) < sessions:
        raise JudgmentRefused(COOLDOWN_CALENDAR_UNAVAILABLE,
                              f"the calendar has fewer than {sessions} sessions after {day.isoformat()}")
    return found[sessions - 1]


@dataclass(frozen=True)
class BacktestJudgment:
    judgment_id: str
    case_digest: str
    request_id: Optional[str]
    kind: str
    verdict: str
    strategy_key: str
    body: dict
    binding: dict
    cooldown_until_session: Optional[dt.date]
    recorded_at: dt.datetime

    def to_json(self) -> dict:
        return {"judgment_id": self.judgment_id, "case_digest": self.case_digest, "request_id": self.request_id,
                "kind": self.kind, "verdict": self.verdict, "strategy_key": self.strategy_key, "body": self.body,
                "binding": self.binding, "cooldown_until_session": _iso_or_none(self.cooldown_until_session),
                "recorded_at": utc(self.recorded_at).isoformat()}


def _iso_or_none(day: Optional[dt.date]) -> Optional[str]:
    return None if day is None else day.isoformat()


def _text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def record_digest(judgment: BacktestJudgment, body_digest: str) -> str:
    sealed = {**judgment.to_json(), "body_digest": body_digest}
    return "sha256:" + sha256_digest(JUDGMENT_DOMAIN, sealed)


def _binding(case: EvaluationCase, digest: str) -> dict:
    path, class_name = split_strategy_key(case.strategy_key)
    return {"case_digest": digest, "kind": case.kind, "stage": case.stage, "request_id": case.request_id,
            "strategy_key": case.strategy_key, "strategy_path": path, "class_name": class_name,
            "strategy_file_hash": case.strategy_file_hash, "params": case.selected_params,
            "conids": list(case.conids), "bar_size": case.bar_size, "family_id": case.family_id,
            "selected_trial_id": case.selected_trial_id, "artifact_id": case.artifact_id,
            "eligibility_decision_digest": case.eligibility_decision_digest,
            "prior_deployment_version": None if case.renewal is None else case.renewal.prior_deployment_version}


def _sealed_judgment(row: Any) -> BacktestJudgment:
    """Rebuild a judgment from a row in ``_COLUMNS`` order; refuse a row whose seal does not match."""
    judgment = BacktestJudgment(row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6]),
                                json.loads(row[8]), row[9], utc(row[10]))
    if not hmac.compare_digest(record_digest(judgment, row[7]), row[11]):
        raise JudgmentRefused(JUDGMENT_TAMPERED, f"judgment {row[0]} does not match its record digest")
    return judgment


def _existing_receipt(row: Any, body_digest: str) -> dict:
    stored = _sealed_judgment(row)
    if not hmac.compare_digest(row[7], body_digest):
        raise JudgmentRefused(JUDGMENT_CONFLICT, "this judgment id already holds another body")
    return _receipt("EXISTING", stored.judgment_id, stored.verdict, stored.cooldown_until_session)


def _receipt(status: str, judgment_id: str, verdict: str, cooldown: Optional[dt.date]) -> dict:
    return {"status": status, "judgment_id": judgment_id, "code": None, "detail": None, "retryable": False,
            "verdict": verdict, "cooldown_until_session": _iso_or_none(cooldown)}


class BacktestJudgments:
    def __init__(self, db: Any, *, config: BacktestJudgeConfig, calendar: Any, cases_dir: Path, verify_dir: Path,
                 now: Callable[[], dt.datetime], renewals: Optional[RenewalChecks] = None):
        self._db = db
        self._config = config
        self._calendar = calendar
        self._cases_dir = Path(cases_dir)
        self._verify_dir = Path(verify_dir)
        self._now = now
        self._renewals = renewals if renewals is not None else NoRenewalVersions()

    def record(self, request: RecordBacktestJudgmentRequest) -> dict:
        body = request.digest_body()
        body_digest = sha256_digest(JUDGMENT_BODY_DOMAIN, body)
        try:
            prior = self._existing_reply(request.judgment_id, body_digest)
            if prior is not None:
                return prior
            case = self._verified_case(request.case_digest)
            self._check_against_case(request, case, utc(self._now()))
            return self._db.transaction(lambda conn: self._insert_in_tx(conn, request, case, body, body_digest))
        except JudgmentRefused as refused:
            if refused.code == JUDGMENT_TAMPERED:
                raise                       # a broken seal is an error, never a reply
            return refused.reply(request.judgment_id)

    def get(self, judgment_id: str) -> Optional[BacktestJudgment]:
        row = self._db.execute(f"SELECT {_COLUMNS} FROM backtest_judgments WHERE judgment_id = ?",
                               [judgment_id], fetch="one")
        return None if row is None else _sealed_judgment(row)

    def get_by_case(self, case_digest: str) -> Optional[BacktestJudgment]:
        """One judgment per case (UNIQUE case_digest), so the case names at most one row."""
        row = self._db.execute("SELECT judgment_id FROM backtest_judgments WHERE case_digest = ?",
                               [case_digest], fetch="one")
        return None if row is None else self.get(row[0])

    def tracked(self) -> list[BacktestJudgment]:
        """Every judgment with a shadow window (DEPLOY, SHADOW, REJECT); a broken seal raises JudgmentRefused."""
        marks = ", ".join("?" for _ in TRACKED_VERDICTS)
        rows = self._db.execute(f"SELECT {_COLUMNS} FROM backtest_judgments WHERE verdict IN ({marks}) "
                                "ORDER BY recorded_at, judgment_id", list(TRACKED_VERDICTS), fetch="all")
        return [_sealed_judgment(row) for row in rows or []]

    def _existing_reply(self, judgment_id: str, body_digest: str) -> Optional[dict]:
        """A retry returns the first receipt without re-reading the case file."""
        row = self._db.execute(f"SELECT {_COLUMNS} FROM backtest_judgments WHERE judgment_id = ?",
                               [judgment_id], fetch="one")
        return None if row is None else _existing_receipt(row, body_digest)

    def _verified_case(self, digest: str) -> EvaluationCase:
        try:
            return load_verified_case(self._cases_dir, digest, load_case_verify_keys(self._verify_dir))
        except CaseRefused as refused:
            raise JudgmentRefused(refused.code, refused.detail) from None

    def _check_against_case(self, request: RecordBacktestJudgmentRequest, case: EvaluationCase,
                            now: dt.datetime) -> None:
        if request.kind != case.kind:
            raise JudgmentRefused("JUDGMENT_KIND_MISMATCH", f"a {request.kind} judgment of a {case.kind} case")
        if request.decided_at_utc() > now + MAX_DECIDED_AHEAD:
            raise JudgmentRefused("DECIDED_IN_FUTURE", "decided_at is ahead of the trader clock")
        if tuple(request.menu) != offered_menu(case):
            raise JudgmentRefused("JUDGMENT_MENU_MISMATCH", f"the case offers {list(offered_menu(case))}")
        if case.kind == "RENEWAL":
            self._check_renewal(request, case, now)
        elif request.verdict == "DEPLOY" and not initial_deploy_allowed(case):
            raise JudgmentRefused("DEPLOY_NOT_ALLOWED", "only a complete case with every paper-v1 rule passed")

    def _check_renewal(self, request: RecordBacktestJudgmentRequest, case: EvaluationCase,
                       now: dt.datetime) -> None:
        if request.renewal_of_version != case.renewal.prior_deployment_version:
            raise JudgmentRefused("RENEWAL_VERSION_MISMATCH", "the judgment and the case name different versions")
        status = self._renewals.status(case, now=now)
        if status.refusal_code is not None:
            raise JudgmentRefused(status.refusal_code, status.detail)
        if request.verdict == "DEPLOY" and status.deploy_block_code is not None:
            raise JudgmentRefused(status.deploy_block_code, status.detail)
        if request.verdict == "DEPLOY" and not renewal_forward_complete(case):
            raise JudgmentRefused("DEPLOY_NOT_ALLOWED", "the forward evidence is incomplete")

    def _cooldown_until(self, verdict: str, now: dt.datetime) -> Optional[dt.date]:
        if verdict != "REJECT":
            return None
        return nth_session_after(self._calendar, ny_day(now), self._config.family_cooldown_sessions)

    def _insert_in_tx(self, conn: Any, request: RecordBacktestJudgmentRequest, case: EvaluationCase, body: dict,
                      body_digest: str) -> dict:
        recorded_at = utc(self._now())
        row = conn.execute(f"SELECT {_COLUMNS} FROM backtest_judgments WHERE judgment_id = ?",
                           [request.judgment_id]).fetchone()
        if row is not None:                         # a concurrent retry won the race
            return _existing_receipt(row, body_digest)
        other = conn.execute("SELECT judgment_id FROM backtest_judgments WHERE case_digest = ?",
                             [request.case_digest]).fetchone()
        if other is not None:
            raise JudgmentRefused(JUDGMENT_CONFLICT, f"the case is already judged by {other[0]}")
        if case.kind == "INITIAL":
            other = conn.execute("SELECT judgment_id FROM backtest_judgments WHERE request_id = ?",
                                 [case.request_id]).fetchone()
            if other is not None:
                raise JudgmentRefused(JUDGMENT_CONFLICT, f"the evaluation is already judged by {other[0]}")
            self._check_claim_in_tx(conn, case, request.decided_at_utc())
        judgment = BacktestJudgment(request.judgment_id, request.case_digest, case.request_id, case.kind,
                                    request.verdict, case.strategy_key, body, _binding(case, request.case_digest),
                                    self._cooldown_until(request.verdict, recorded_at), recorded_at)
        conn.execute(f"INSERT INTO backtest_judgments ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [judgment.judgment_id, judgment.case_digest, judgment.request_id, judgment.kind,
                      judgment.verdict, judgment.strategy_key, _text(judgment.body), body_digest,
                      _text(judgment.binding), judgment.cooldown_until_session, judgment.recorded_at,
                      record_digest(judgment, body_digest)])
        return _receipt("RECORDED", judgment.judgment_id, judgment.verdict, judgment.cooldown_until_session)

    @staticmethod
    def _check_claim_in_tx(conn: Any, case: EvaluationCase, decided_at: dt.datetime) -> None:
        claim = claim_row_in_tx(conn, case.request_id)
        if claim is None:
            raise JudgmentRefused("CASE_CLAIM_UNKNOWN", "the trader holds no claim for this evaluation")
        if claim.state not in ("DONE", "FAILED"):
            raise JudgmentRefused("CASE_CLAIM_NOT_FINISHED", f"the evaluation is {claim.state}", retryable=True)
        finished_as = "FAILED" if case.stage == "FAILED" else "DONE"
        if claim.state != finished_as:
            raise JudgmentRefused("CASE_CLAIM_MISMATCH", f"a {case.stage} case needs a {finished_as} claim, "
                                                         f"the claim is {claim.state}")
        claimed = claim.body()
        same = (claim.strategy_key == case.strategy_key and claim.ny_day.isoformat() == case.claim_day
                and _text([claimed.cohort, claimed.conids, claimed.bar_size])
                == _text([case.cohort, case.conids, case.bar_size]))
        if not same:
            raise JudgmentRefused("CASE_CLAIM_MISMATCH", "the case is not the claimed evaluation")
        if decided_at < claim.claimed_at:
            raise JudgmentRefused("DECIDED_BEFORE_CLAIM", "decided_at is before the evaluation was claimed")
