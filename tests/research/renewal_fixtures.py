"""A trader's forward evidence for one version of the fixture TimeOfDay line (Plan 5 Tasks 5 and 6)."""
from __future__ import annotations

import hashlib

from tests.research.evaluation_fixtures import CONIDS, TIME_OF_DAY_STRATEGY

V1 = "sha256:" + "1" * 64
BASE = "sha256:" + "e" * 64
BUNDLE = "sha256:" + "b" * 64
KEY = "strategies/time_of_day.py:TimeOfDay"
PARAMS = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
SESSIONS = ("2024-04-01", "2024-04-02", "2024-04-03")
FILE_HASH = "sha256:" + hashlib.sha256(TIME_OF_DAY_STRATEGY.encode()).hexdigest()


def session(day: str, state: str = "COMPLETE") -> dict:
    complete = state == "COMPLETE"
    return {"session_date": day, "state": state,
            "reason": None if complete else ("BARS_MISSING: fixture" if state == "INCOMPLETE" else None),
            "pnl_usd": 5.0 if complete else None, "fees_usd": 1.0 if complete else None,
            "trades": 2 if complete else None, "end_equity_usd": 100_005.0 if complete else None}


def forward_view(*, states=("COMPLETE",) * 3, renewable=(True, None), trips=(), file_hash=FILE_HASH) -> dict:
    """Task 2's ForwardEvidenceView on the wire."""
    ok, code = renewable
    return {"version_digest": V1, "base_digest": BASE, "judgment_id": "jdg-00000001", "kind": "INITIAL",
            "prior_version_digest": None, "status": "EXPIRED", "first_session": SESSIONS[0],
            "expiry_session": SESSIONS[-1],
            "binding": {"strategy_key": KEY, "strategy_path": "strategies/time_of_day.py", "class_name": "TimeOfDay",
                        "strategy_file_hash": file_hash, "params": dict(PARAMS), "conids": list(CONIDS),
                        "bar_size": "15 mins", "order_notional": 1900.0, "bundle_digest": BUNDLE},
            "line": {"initial_judgment_id": "jdg-00000001", "family_id": "f" * 64, "selected_trial_id": "t0",
                     "artifact_id": "a" * 64, "eligibility_decision_digest": "d" * 64},
            "renewable": {"ok": ok, "code": code, "detail": "" if ok else code.lower()},
            "sessions": [session(day, state) for day, state in zip(SESSIONS, states)],
            "trips": list(trips), "as_of": "2024-04-04T21:00:00+00:00"}


def trip(round_trip_id="rt-1", net_pnl=7.5, status="CLOSED") -> dict:
    return {"round_trip_id": round_trip_id, "conid": CONIDS[0], "status": status, "opened_session": SESSIONS[0],
            "closed_session": SESSIONS[0] if status == "CLOSED" else None, "net_pnl_usd": net_pnl,
            "fees_complete": True}
