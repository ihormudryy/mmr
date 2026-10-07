"""Offline replay of one Jev decision (SP2 spec 11; Plan 6 Rulings 2, 19). No model calls, no trader reads."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from trader.ai.decision_engine import JudgeSettings, judge_entry
from trader.ai.replay import INCOMPLETE, ExternalAdapterCounter, ReplayEvidence, ReplayResult, ReplaySession
from trader.ai.tools import ReplayTools, code_version


async def replay_decision(store: Any, decision_id: str, *, config: Any,
                          counter: Optional[ExternalAdapterCounter] = None) -> ReplayResult:
    passes = (await store.aquery("SELECT COUNT(*) FROM ai_replay_evidence WHERE decision_key = ? "
                                 "AND name = 'given:source'", [decision_id], fetch="one"))[0]
    if passes > 1:
        return ReplayResult(INCOMPLETE, missing=("rejudged_unit",))           # Ruling 19
    evidence = await asyncio.to_thread(ReplayEvidence.load, store, decision_id)
    mismatch = manifest_mismatch(evidence.manifest, config)
    if mismatch:
        return ReplayResult(INCOMPLETE, missing=mismatch)                   # PR #86 thread 4211394935
    session = ReplaySession(evidence, counter or ExternalAdapterCounter())
    settings = JudgeSettings.from_config(config, health=None)

    async def work(replay: ReplaySession) -> dict:
        judgment = await judge_entry(ReplayTools(replay, decision_id), None, settings)
        return judgment.summary()
    result = await session.arun(work)
    session.assert_no_external_calls()
    return result


def manifest_mismatch(manifest: Optional[dict], config: Any) -> tuple[str, ...]:
    """A replay reproduces a decision only under the config and code it was made with. A missing manifest is
    left to the session, which reports it as missing."""
    if manifest is None:
        return ()
    mismatch = ()
    if manifest.get("config_digest") != config.digest():
        mismatch += ("config_mismatch",)
    if manifest.get("code_version") != code_version():
        mismatch += ("code_mismatch",)
    return mismatch


def recorded_judgment(store: Any, decision_id: str) -> Optional[dict]:
    """The first live jev ruling of this unit, in the shape of Judgment.summary()."""
    row = store.db.execute(
        "SELECT outcome, code, quantity, ceiling, evidence_digest FROM ai_rulings WHERE unit_key = ? AND step = 'jev' "
        "ORDER BY recorded_at, ruling_id LIMIT 1", [decision_id], fetch="one")
    if row is None:
        return None
    return {"outcome": row[0], "code": row[1], "quantity": row[2], "ceiling": row[3], "evidence_digest": row[4]}
