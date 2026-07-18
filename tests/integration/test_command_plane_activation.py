"""[P1 Task 8] Command-plane activation gate (in-process, synthetic).

Runs the failure-drill battery (``scripts/command_plane_drill.py``) and asserts
the P1 recovery invariants hold end to end, that the report carries the release
digests a sign-off needs, that Task 6/7 coverage gaps surface as ``pending``
(never silently omitted), and that the create -> approve -> correlated-terminal
flow the drills exercise rides the TYPED command path with no legacy dill-RPC
mutation method reachable on any socket role.

This is the SYNTHETIC gate. The real-Compose IB-paper session soak
(``scripts/run_paper_soak.py`` during market hours) is a separate, manual gate
that these fixtures deliberately do NOT replace.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package; put it on the path so the drill module imports.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import command_plane_drill as drill  # noqa: E402

# Legacy dill-RPC mutation methods that must never be reachable on the typed
# command path (mirrors tests/test_production_rpc_security.py).
LEGACY_MUTATIONS = (
    "place_order_simple",
    "place_expressive_order",
    "place_standalone_order",
    "cancel_all",
    "set_risk_limits",
)

RUNNABLE = {
    "happy_path",
    "duplicate_create_idempotent",
    "ambiguous_submit_reconciles",
    "restart_unresolved",
    "stale_quote_blocks_dispatch",
    "notional_cap_blocks_dispatch",
}
PENDING = {
    "liquidation_flat_only_from_broker_truth",
    "circuit_breaker_trips_and_persists",
    "semantic_readiness_gates_activation",
}


def test_drill_battery_passes_with_release_digests():
    report = drill.run_drills()
    assert report.passed is True
    statuses = {r["name"]: r["status"] for r in report.scenario_results}
    for name in RUNNABLE:
        assert statuses.get(name) == "passed", (name, statuses.get(name))
    # The release record needs a commit + config fingerprint on every report.
    assert report.commit_digest and report.commit_digest != "unknown"
    assert report.config_digest.startswith("sha256:")


def test_pending_scenarios_are_exactly_the_unlanded_features():
    # Coverage gaps (liquidation = Task 7, breaker/readiness = Task 6) must be
    # reported as pending so the gate never reads as "all covered" prematurely.
    report = drill.run_drills()
    assert set(report.pending) == PENDING


def test_unknown_scenario_name_fails_the_gate():
    report = drill.run_drills(["does_not_exist"])
    assert report.passed is False
    assert report.scenario_results[0]["status"] == "unknown"


@pytest.mark.parametrize("method", LEGACY_MUTATIONS)
def test_no_legacy_rpc_mutation_on_command_path(method, tmp_path):
    stack = drill.DrillStack(str(tmp_path / "authority.duckdb"))
    for role in ("command", "query", "feed"):
        assert stack.registry.resolve(role, method) is None
