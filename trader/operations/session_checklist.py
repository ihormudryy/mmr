"""P4 Task 7 — pre/post session automation checklists."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.data.domain_journal import DomainJournal
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainMutation

SESSION_CHECKLIST_MIGRATION_45 = 45
SESSION_CHECKLIST_MIGRATION_VERSIONS = (SESSION_CHECKLIST_MIGRATION_45,)
SESSION_CHECKLIST_MIGRATION_45_NAME = "p4_session_checklist_results"

CHECK_PHASE_PRE = "pre"
CHECK_PHASE_POST = "post"


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def checklist_digest(
    *,
    session_key: str,
    strategy_id: str,
    artifact_digest: str,
    config_digest: str,
    phase: str,
) -> str:
    body = json.dumps(
        {
            "session_key": session_key,
            "strategy_id": strategy_id,
            "artifact_digest": artifact_digest,
            "config_digest": config_digest,
            "phase": phase,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def apply_session_checklist_migration(migrator: SchemaMigrator) -> bool:
    phases = f"'{CHECK_PHASE_PRE}', '{CHECK_PHASE_POST}'"
    return migrator.apply(
        SESSION_CHECKLIST_MIGRATION_45,
        SESSION_CHECKLIST_MIGRATION_45_NAME,
        (
            f"""CREATE TABLE IF NOT EXISTS session_checklist_results (
                result_digest VARCHAR PRIMARY KEY,
                session_key VARCHAR NOT NULL,
                strategy_id VARCHAR NOT NULL,
                artifact_digest VARCHAR NOT NULL,
                config_digest VARCHAR NOT NULL,
                phase VARCHAR NOT NULL CHECK (phase IN ({phases})),
                passed BOOLEAN NOT NULL,
                checks_json VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_session_checklist_session
                ON session_checklist_results(session_key, strategy_id)""",
        ),
    )


@dataclass(frozen=True)
class SessionChecklistContext:
    session_key: str
    strategy_id: str
    artifact_digest: str
    config_digest: str
    account_id: str
    account_mode: str
    gross_allocation: float
    xnys_schedule_version: str
    broker_generation: int
    broker: Optional[BrokerRiskSnapshot] = None
    prior_session_flat: bool = True
    prior_session_reconciled: bool = True
    eligibility_valid: bool = True
    breaker_clear: bool = True
    quotes_ready: bool = True
    risk_evidence_current: bool = True
    replay_sealed: bool = False
    replay_passed: bool = False
    replay_divergence_acknowledged: bool = False
    evidence_updated: bool = False
    attribution_complete: bool = False


@dataclass(frozen=True)
class SessionChecklistResult:
    phase: str
    passed: bool
    checks: dict[str, bool]
    result_digest: str
    recorded_at: dt.datetime
    session_key: str
    strategy_id: str
    artifact_digest: str
    config_digest: str
    account_id: str = ""
    idempotent_replay: bool = False

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.checks.items() if not ok)


class SessionChecklistStore:
    def __init__(self, journal: DomainJournal):
        self.journal = journal

    def get(self, result_digest: str) -> Optional[SessionChecklistResult]:
        row = self.journal.connect().execute(
            "SELECT result_digest, session_key, strategy_id, artifact_digest, config_digest, "
            "phase, passed, checks_json, recorded_at FROM session_checklist_results "
            "WHERE result_digest = ?",
            [result_digest],
        ).fetchone()
        if row is None:
            return None
        checks = json.loads(row[7])
        return SessionChecklistResult(
            phase=row[5],
            passed=bool(row[6]),
            checks=checks,
            result_digest=row[0],
            recorded_at=row[8],
            session_key=row[1],
            strategy_id=row[2],
            artifact_digest=row[3],
            config_digest=row[4],
            idempotent_replay=True,
        )

    def save(self, result: SessionChecklistResult) -> SessionChecklistResult:
        existing = self.get(result.result_digest)
        if existing is not None:
            return existing

        def work(conn, append):
            conn.execute(
                "INSERT INTO session_checklist_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    result.result_digest,
                    result.session_key,
                    result.strategy_id,
                    result.artifact_digest,
                    result.config_digest,
                    result.phase,
                    result.passed,
                    json.dumps(result.checks, sort_keys=True),
                    result.recorded_at,
                ],
            )
            mutation = DomainMutation(
                event_type="operations.session_checklist",
                entity_type="session_checklist",
                entity_id=result.result_digest,
                operation="upsert",
                account_id=result.account_id,
                source="trader_service",
                source_timestamp=result.recorded_at,
                correlation_id=result.result_digest,
                payload={
                    "phase": result.phase,
                    "passed": result.passed,
                    "failed": list(result.failed),
                },
            )
            append(mutation, lambda _c, _r: None, result.result_digest)
            return result

        self.journal.mutate_batch_work(self.journal.connect(), work)
        return result


class SessionChecklist:
    """Durable, idempotent pre/post session gates for automation."""

    def __init__(self, store: SessionChecklistStore):
        self.store = store

    def run_pre(self, ctx: SessionChecklistContext, *, now: dt.datetime) -> SessionChecklistResult:
        now = _as_utc(now)
        checks = {
            "account_mode_live_or_paper": ctx.account_mode in ("paper", "live"),
            "artifact_digest_present": bool(ctx.artifact_digest),
            "allocation_positive": ctx.gross_allocation > 0,
            "xnys_schedule_version": bool(ctx.xnys_schedule_version),
            "broker_generation_current": ctx.broker_generation > 0,
            "quotes_ready": ctx.quotes_ready,
            "risk_evidence_current": ctx.risk_evidence_current,
            "prior_session_flat": ctx.prior_session_flat,
            "prior_session_reconciled": ctx.prior_session_reconciled,
            "eligibility_valid": ctx.eligibility_valid,
            "breaker_clear": ctx.breaker_clear,
        }
        if ctx.broker is not None:
            checks["account_id_matches"] = ctx.broker.account_id == ctx.account_id
            checks["account_mode_matches"] = ctx.broker.account_mode == ctx.account_mode
        passed = all(checks.values())
        digest = checklist_digest(
            session_key=ctx.session_key,
            strategy_id=ctx.strategy_id,
            artifact_digest=ctx.artifact_digest,
            config_digest=ctx.config_digest,
            phase=CHECK_PHASE_PRE,
        )
        result = SessionChecklistResult(
            phase=CHECK_PHASE_PRE,
            passed=passed,
            checks=checks,
            result_digest=digest,
            recorded_at=now,
            session_key=ctx.session_key,
            strategy_id=ctx.strategy_id,
            artifact_digest=ctx.artifact_digest,
            config_digest=ctx.config_digest,
            account_id=ctx.account_id,
        )
        return self.store.save(result)

    def run_post(self, ctx: SessionChecklistContext, *, now: dt.datetime) -> SessionChecklistResult:
        now = _as_utc(now)
        flat = True
        no_orders = True
        if ctx.broker is not None:
            flat = all(abs(p.quantity) < 1e-9 for p in ctx.broker.positions)
            no_orders = ctx.broker.open_order_count == 0
        checks = {
            "broker_flat": flat,
            "no_working_orders": no_orders,
            "replay_sealed": ctx.replay_sealed,
            "replay_passed": ctx.replay_passed,
            "attribution_complete": ctx.attribution_complete,
            "evidence_updated": ctx.evidence_updated,
            "divergence_acknowledged_or_none": ctx.replay_divergence_acknowledged or ctx.replay_passed,
        }
        passed = all(checks.values())
        digest = checklist_digest(
            session_key=ctx.session_key,
            strategy_id=ctx.strategy_id,
            artifact_digest=ctx.artifact_digest,
            config_digest=ctx.config_digest,
            phase=CHECK_PHASE_POST,
        )
        result = SessionChecklistResult(
            phase=CHECK_PHASE_POST,
            passed=passed,
            checks=checks,
            result_digest=digest,
            recorded_at=now,
            session_key=ctx.session_key,
            strategy_id=ctx.strategy_id,
            artifact_digest=ctx.artifact_digest,
            config_digest=ctx.config_digest,
            account_id=ctx.account_id,
        )
        return self.store.save(result)


def pre_session_checklist_passed(
    checklist: SessionChecklist,
    ctx_factory: Callable[[], SessionChecklistContext],
    *,
    now: Callable[[], dt.datetime],
) -> bool:
    """Hook for ``SemanticReadiness``: missing/failed pre-check keeps automation paused."""
    try:
        ctx = ctx_factory()
        result = checklist.run_pre(ctx, now=now())
        return result.passed
    except Exception:
        return False
