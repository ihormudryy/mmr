"""Code-owned identities (SP2 spec 5.3, 8; Plan 5 Rulings 11-13). Models never name an id."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ACTION_KEY = re.compile(r"^[a-z][a-z_]{0,15}:[0-9]{1,12}(:[a-z0-9_]{1,24})?$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_decision_id(source_id: str, action_key: str) -> str:
    """Cycle id or signal source-event id plus the action identity: one id per logical decision."""
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    if not isinstance(action_key, str) or not ACTION_KEY.fullmatch(action_key):
        raise ValueError("action_key must look like 'enter:265598'")
    return "dec-" + _sha(f"{source_id}|{action_key}")[:32]


def command_id_for(decision_id: str) -> str:
    """The trader's rule (trader.automation.ai_paper_decision.command_id_for); a test pins they agree."""
    return f"aip-{decision_id}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def cost_record_id(event_id: str) -> str:
    return "cost-" + _sha(event_id)[:40]


def attempt_ref(attempt_key: str) -> str:
    return "att-" + _sha(attempt_key)[:40]


def simulated_record_id(experiment_id: str, baseline_id: str, opportunity_id: str) -> str:
    return "sim-" + _sha(f"{experiment_id}|{baseline_id}|{opportunity_id}")[:40]
