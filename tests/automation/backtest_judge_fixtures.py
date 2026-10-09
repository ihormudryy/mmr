"""Shared helpers for SP2c Plan 1 tests: request bodies, signed cases, a mutable clock."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from pathlib import Path

from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.automation.backtest_judge_schema import apply_backtest_judge_migrations
from trader.automation.backtest_judge_wire import NARRATIVE_FIELDS, RecordBacktestJudgmentRequest
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.evaluation_claims import EvaluationClaims
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import canonical_json_bytes
from trader.research.evaluation_case import (
    CASE_DOMAIN, FULL_MENU, EvaluationCase, _signed_message, case_digest, case_path, write_evaluation_case,
)
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2026, 10, 8, 21, 30, tzinfo=dt.timezone.utc)      # Thursday 17:30 ET, after the close
KEY = "strategies/opening_range_breakout.py:OpeningRangeBreakout"
FILE_HASH = "sha256:" + "a" * 64
VERSION = "sha256:" + "b" * 64
HOLDOUT_BY_STAGE = {"COMPLETE": True, "HOLDOUT_FAILED": False}
ZERO = "sha256:" + "0" * 64


class Clock:
    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class ClockMovesWhileWaitingForTheLock:
    """A journal whose ``transaction`` first moves the clock: the caller waited for the lock until ``then``."""

    def __init__(self, db: DuckDBConnection, clock: Clock, then: dt.datetime):
        self._db = db
        self._clock = clock
        self._then = then

    def execute(self, *args, **kwargs):
        return self._db.execute(*args, **kwargs)

    def transaction(self, fn):
        self._clock.now = self._then
        return self._db.transaction(fn)


def request_body(**changes) -> EvaluationRequestBody:
    raw = {"strategy_key": KEY, "cohort": [{"RANGE_MINUTES": 15}, {"RANGE_MINUTES": 30}],
           "conids": [265598, 272093], "bar_size": "5 mins", "research_day": "2026-10-08"}
    raw.update(changes)
    return EvaluationRequestBody.model_validate(raw)


def passing_results() -> list[dict]:
    return [{"code": rule.code, "passed": True} for rule in PAPER_V1.rules]


def failed_results() -> list[dict]:
    results = passing_results()
    results[0]["passed"] = False
    return results


def holdout_evidence(stage: str) -> dict | None:
    """Plan 3's evidence.holdout: a result once the holdout ran, else null."""
    passed = HOLDOUT_BY_STAGE.get(stage)
    if passed is None:
        return None
    return {"start": "2025-01-02", "end": "2025-12-31", "passed": passed, "detail": "fixture holdout"}


def cohort_evidence(raw: dict) -> dict:
    """Plan 3's evidence.points and selected_index, agreeing with the header.

    The selected point carries the header's trial and, once the holdout opened, the final rule results.
    """
    points = [{"index": i, "params": dict(point), "trial_id": f"trial-point-{i}", "pre_holdout_passed": False,
               "rules": failed_results()} for i, point in enumerate(raw["cohort"])]
    selected = raw["selected_params"]
    if selected is None:
        return {"points": points, "selected_index": None}
    spelled = canonical_json_bytes(selected)
    index = next((i for i, point in enumerate(raw["cohort"]) if canonical_json_bytes(point) == spelled), 0)
    holdout_opened = raw["stage"] in HOLDOUT_BY_STAGE
    points[index].update(params=dict(selected), trial_id=raw["selected_trial_id"],
                         pre_holdout_passed=holdout_opened,
                         rules=[dict(rule) for rule in raw["final_rule_results"]] if holdout_opened
                         else failed_results())
    return {"points": points, "selected_index": index}


def case_body(body: EvaluationRequestBody, *, stage: str = "COMPLETE", **changes) -> dict:
    raw = {"schema_version": CASE_DOMAIN, "kind": "INITIAL", "request_id": evaluation_request_id(body),
           "claim_day": "2026-10-08", "strategy_key": body.strategy_key, "strategy_file_hash": FILE_HASH,
           "cohort": [dict(point) for point in body.cohort], "conids": list(body.conids),
           "bar_size": body.bar_size, "stage": stage, "selected_params": dict(body.cohort[0]),
           "family_id": "fam-1", "selected_trial_id": "trial-1", "artifact_id": "art-1",
           "eligibility_decision_digest": "d" * 64, "decision_state": "PAPER_ELIGIBLE",
           "ruleset_digest": PAPER_V1.digest, "holdout_passed": HOLDOUT_BY_STAGE.get(stage),
           "final_rule_results": passing_results(), "renewal": None,
           "created_at": NOW.isoformat()}
    if stage == "PRE_HOLDOUT_FAILED":
        raw.update(selected_params=None, selected_trial_id=None, artifact_id=None, eligibility_decision_digest=None,
                   decision_state=None, ruleset_digest=None, final_rule_results=[])
    raw.update(changes)
    if "evidence" not in changes:
        raw["evidence"] = {"note": "fixture", "holdout": holdout_evidence(stage), **cohort_evidence(raw)}
    return raw


def make_case(body: EvaluationRequestBody | None = None, **changes) -> EvaluationCase:
    return EvaluationCase.model_validate(case_body(body or request_body(), **changes))


def renewal_case_body(**changes) -> dict:
    raw = case_body(request_body(), kind="RENEWAL", stage="FORWARD_COMPLETE", request_id=None, claim_day=None,
                    cohort=[{"RANGE_MINUTES": 15}], selected_params={"RANGE_MINUTES": 15},
                    renewal={"prior_deployment_version": VERSION, "forward_sessions": 20, "incomplete_sessions": 0})
    raw.update(changes)
    return raw


@dataclass
class CaseKeys:
    signer: AttestationSigner
    verify_dir: Path
    cases_dir: Path


def case_keys(tmp_path: Path) -> CaseKeys:
    signer = AttestationSigner.generate()
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir()
    (verify_dir / "research.pem").write_bytes(signer.public_key_pem())
    return CaseKeys(signer, verify_dir, tmp_path / "artifacts" / "cases")


def write_signed_body(keys: CaseKeys, body: dict) -> str:
    """Sign and store a raw case body that the case model may refuse; return its digest."""
    digest = case_digest(body)
    path = case_path(keys.cases_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes({
        "case": body, "case_digest": digest, "public_key_id": keys.signer.public_key_id,
        "signature": keys.signer.sign_message(_signed_message(body))}))
    return digest


def journal(tmp_path: Path) -> DuckDBConnection:
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_backtest_judge_migrations(SchemaMigrator(db))
    return db


def judge_config(**changes) -> BacktestJudgeConfig:
    return replace(BacktestJudgeConfig(strategy_allowlist=(KEY,)), **changes)


def insert_reject(db: DuckDBConnection, strategy_key: str, until: dt.date, judgment_id: str = "jdg-fixture-1") -> None:
    """A bare REJECT row, so the claim tests do not depend on the judgment store."""
    db.execute(
        "INSERT INTO backtest_judgments (judgment_id, case_digest, request_id, kind, verdict, strategy_key, "
        "body_json, body_digest, binding_json, cooldown_until_session, recorded_at, record_digest) "
        "VALUES (?, ?, ?, 'INITIAL', 'REJECT', ?, '{}', 'x', '{}', ?, ?, 'x')",
        [judgment_id, "sha256:" + judgment_id.encode().hex().ljust(64, "0")[:64], "sha256:fixture",
         strategy_key, until, NOW])


def count_claims(db: DuckDBConnection) -> int:
    return db.execute("SELECT COUNT(*) FROM evaluation_claims", fetch="one")[0]


NARRATIVE = {name: f"{name} written by Jev" for name in NARRATIVE_FIELDS}


@dataclass
class World:
    db: DuckDBConnection
    clock: Clock
    claims: EvaluationClaims
    judgments: BacktestJudgments
    keys: CaseKeys


def world(tmp_path: Path, *, renewals=None, calendar=None, **config) -> World:
    db, clock, keys, cfg = journal(tmp_path), Clock(NOW), case_keys(tmp_path), judge_config(**config)
    judgments = BacktestJudgments(db, config=cfg, calendar=calendar or XNYSCalendarPolicy(),
                                  cases_dir=keys.cases_dir, verify_dir=keys.verify_dir, now=clock, renewals=renewals)
    return World(db, clock, EvaluationClaims(db, config=cfg, now=clock), judgments, keys)


def finished(w: World, body: EvaluationRequestBody | None = None, *, state: str = "DONE", signer=None,
             **case_changes) -> str:
    """Claim, finish and sign one evaluation; return its case digest."""
    body = body or request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")
    w.claims.update(evaluation_request_id(body), state)
    return write_evaluation_case(w.keys.cases_dir, make_case(body, **case_changes), signer or w.keys.signer)


def judgment(case_digest: str, verdict: str = "REJECT", menu=FULL_MENU, **changes) -> RecordBacktestJudgmentRequest:
    raw = {"judgment_id": "jdg-00000001", "case_digest": case_digest, "kind": "INITIAL", "renewal_of_version": None,
           "verdict": verdict, "menu": list(menu), "jev_model": "openrouter/jev-1", "jev_attempt_ref": "att-1",
           "decided_at": NOW.isoformat(), "narrative": dict(NARRATIVE) if verdict == "DEPLOY" else None}
    raw.update(changes)
    return RecordBacktestJudgmentRequest.model_validate(raw)


def count_judgments(db: DuckDBConnection) -> int:
    return db.execute("SELECT COUNT(*) FROM backtest_judgments", fetch="one")[0]
