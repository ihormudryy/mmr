"""Journal tables of SP3 authored strategies (migrations 130 and 131; 132 is SP3 Plan 5's, 133-134 stay free)."""
from __future__ import annotations

from typing import Any

from trader.automation.authored_strategies import AuthoredState

AUTHORED_STRATEGIES_MIGRATION_VERSION = 130
ISOLATION_PROOFS_MIGRATION_VERSION = 131
_STATES = ", ".join(f"'{state.value}'" for state in AuthoredState)

_AUTHORED = f"""CREATE TABLE IF NOT EXISTS authored_strategies (
    digest VARCHAR PRIMARY KEY, class_name VARCHAR NOT NULL, bar_size VARCHAR NOT NULL,
    tunables_json VARCHAR NOT NULL, note VARCHAR NOT NULL, author_model VARCHAR NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ({_STATES})),
    reason VARCHAR, attempts INTEGER NOT NULL CHECK (attempts >= 0),
    evaluation_request_id VARCHAR, sandbox_image_digest VARCHAR, sandbox_image_seen_at TIMESTAMPTZ,
    verdict VARCHAR CHECK (verdict IS NULL OR verdict IN ('DEPLOY','SHADOW','REJECT','NO_VERDICT')),
    judgment_id VARCHAR, deployment_version VARCHAR,
    created_at TIMESTAMPTZ NOT NULL, state_changed_at TIMESTAMPTZ NOT NULL, history_json VARCHAR NOT NULL)"""

_PROOFS = """CREATE TABLE IF NOT EXISTS authored_isolation_proofs (
    proof_id VARCHAR PRIMARY KEY, image_digest VARCHAR NOT NULL, passed BOOLEAN NOT NULL,
    checks_json VARCHAR NOT NULL, ran_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
    principal VARCHAR NOT NULL)"""


def apply_authored_migrations(migrator: Any) -> list[int]:
    applied = []
    if migrator.apply(AUTHORED_STRATEGIES_MIGRATION_VERSION, "sp3_authored_strategies", (_AUTHORED,)):
        applied.append(AUTHORED_STRATEGIES_MIGRATION_VERSION)
    if migrator.apply(ISOLATION_PROOFS_MIGRATION_VERSION, "sp3_authored_isolation_proofs", (_PROOFS,)):
        applied.append(ISOLATION_PROOFS_MIGRATION_VERSION)
    return applied
