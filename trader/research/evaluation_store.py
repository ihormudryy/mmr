"""Every `research evaluate` run, recorded (research migration 11).

Activate reads the latest row for a strategy to explain a refusal.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.research.strategy_paths import normalize_strategy_path

RESEARCH_MIGRATION_EVALUATIONS = 11
STAGE_PRE_HOLDOUT = "pre_holdout"
STAGE_HOLDOUT_FAILED = "holdout_failed"
STAGE_COMPLETE = "complete"

_COLUMNS = ("evaluation_id, spec_name, family_id, strategy_path, class_name, stage, state, "
            "artifact_id, decision_digest, failed_rules, missing_rules, report_path, created_at")
_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS research_evaluations (
        evaluation_id VARCHAR PRIMARY KEY,
        spec_name VARCHAR NOT NULL,
        family_id VARCHAR NOT NULL,
        strategy_path VARCHAR NOT NULL,
        class_name VARCHAR NOT NULL,
        stage VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        artifact_id VARCHAR,
        decision_digest VARCHAR,
        failed_rules VARCHAR NOT NULL,
        missing_rules VARCHAR NOT NULL,
        report_path VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
)
NO_EVALUATION = "no evaluation found; run `mmr research evaluate <spec.yaml>`"


def apply_evaluation_migrations(migrator: SchemaMigrator) -> None:
    migrator.apply(version=RESEARCH_MIGRATION_EVALUATIONS, name="research_evaluations",
                   statements=list(_STATEMENTS))


@dataclass(frozen=True)
class EvaluationRecord:
    spec_name: str
    family_id: str
    strategy_path: str
    class_name: str
    stage: str
    state: str
    artifact_id: Optional[str]
    decision_digest: Optional[str]
    failed_rules: tuple[str, ...]
    missing_rules: tuple[str, ...]
    report_path: str
    created_at: dt.datetime
    evaluation_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def summary(self) -> str:
        parts = [f"latest evaluation {self.spec_name!r}: {self.state} at stage {self.stage}"]
        if self.failed_rules:
            parts.append("failed " + ", ".join(self.failed_rules))
        if self.missing_rules:
            parts.append("missing " + ", ".join(self.missing_rules))
        parts.append(f"report {self.report_path}")
        return "; ".join(parts)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _row_to_record(r) -> EvaluationRecord:
    return EvaluationRecord(
        evaluation_id=r[0], spec_name=r[1], family_id=r[2], strategy_path=r[3],
        class_name=r[4], stage=r[5], state=r[6], artifact_id=r[7], decision_digest=r[8],
        failed_rules=tuple(json.loads(r[9])), missing_rules=tuple(json.loads(r[10])),
        report_path=r[11], created_at=_as_utc(r[12]))


class EvaluationRepository:
    def __init__(self, db: Any):
        self._db = db

    def record(self, rec: EvaluationRecord) -> str:
        def _tx(conn):
            conn.execute(
                f"INSERT INTO research_evaluations ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [rec.evaluation_id, rec.spec_name, rec.family_id,
                 normalize_strategy_path(rec.strategy_path), rec.class_name, rec.stage,
                 rec.state, rec.artifact_id, rec.decision_digest,
                 json.dumps(list(rec.failed_rules)), json.dumps(list(rec.missing_rules)),
                 rec.report_path, rec.created_at])
            return rec.evaluation_id

        return self._db.transaction(_tx)

    def list(self, limit: int = 50) -> list[EvaluationRecord]:
        def _tx(conn):
            return conn.execute(
                f"SELECT {_COLUMNS} FROM research_evaluations "
                "ORDER BY created_at DESC LIMIT ?", [limit]).fetchall()

        return [_row_to_record(r) for r in self._db.transaction(_tx)]

    def latest_for_strategy(self, strategy_path: str,
                            class_name: str) -> Optional[EvaluationRecord]:
        target = normalize_strategy_path(strategy_path)

        def _tx(conn):
            return conn.execute(
                f"SELECT {_COLUMNS} FROM research_evaluations WHERE class_name = ? "
                "ORDER BY created_at DESC", [class_name]).fetchall()

        for row in self._db.transaction(_tx):
            if normalize_strategy_path(row[3]) == target:
                return _row_to_record(row)
        return None


def _summary_path(summaries_dir: Path, strategy_path: str, class_name: str) -> Path:
    key = normalize_strategy_path(strategy_path).replace("/", "__")
    return Path(summaries_dir) / f"{key}__{class_name}.json"


def write_evaluation_summary(summaries_dir: Path, rec: EvaluationRecord) -> Path:
    """The latest evaluation for one strategy file + class, as a plain file.

    trader_service must never open the research DuckDB (trader/config.py), so
    Activate explains a refusal from this file instead.
    """
    path = _summary_path(summaries_dir, rec.strategy_path, rec.class_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"summary": rec.summary(), "family_id": rec.family_id,
                       "stage": rec.stage, "state": rec.state,
                       "created_at": rec.created_at.isoformat()}, indent=2)
    pending = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    pending.write_text(body)
    os.replace(pending, path)
    return path


def describe_latest_evaluation(summaries_dir: Path, strategy_path: str,
                               class_name: str) -> str:
    """One line for refusal messages. Best effort: it never raises."""
    try:
        path = _summary_path(summaries_dir, strategy_path, class_name)
        return str(json.loads(path.read_text())["summary"])
    except FileNotFoundError:
        return NO_EVALUATION
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"evaluation status unavailable ({type(exc).__name__}: {exc})"
