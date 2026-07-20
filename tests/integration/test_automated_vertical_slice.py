"""[P3 Task 9] One-strategy paper automation vertical slice (synthetic gate).

Runs ``scripts/automation_paper_drill.py`` and asserts the P3 recovery
invariants hold end to end on the typed path: duplicate bar → identical
intent → one coordinator command → protected paper order → broker events →
attribution → time exit/flatten → sealed replay; plus failure injections
(stale quote, rejected stop, ambiguous submit, disconnect/duplicate,
crash/restart, missed deadline) with breaker / no duplicate exposure.

This is the SYNTHETIC gate. The real IB-paper session soak remains a separate,
manual, non-fungible P3 release gate — these fixtures deliberately do NOT
replace it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import automation_paper_drill as drill  # noqa: E402

from trader.config import AutomationConfig, MMRConfig
from trader.strategy.intent_emitter import IntentEmitter, IntentEmitterContext

RUNNABLE = {
    "happy_path",
    "stale_quote",
    "rejected_stop",
    "ambiguous_submission",
    "disconnect_duplicate_event",
    "crash_restart",
    "missed_deadline",
    "emitter_no_journal_mutation",
}

LEGACY_MUTATIONS = (
    "place_order_simple",
    "place_expressive_order",
    "place_standalone_order",
    "cancel_all",
)


def test_automation_config_defaults_are_disabled_and_empty():
    cfg = AutomationConfig()
    assert cfg.enabled is False
    assert cfg.live_enabled is False
    assert cfg.artifact_bundle_path == ""
    assert cfg.public_key_ring_path == ""
    assert cfg.expected_artifact_id == ""
    assert cfg.strategy_name == ""

    mmr = MMRConfig()
    assert mmr.automation.enabled is False
    assert mmr.automation.strategy_name == ""


def test_drill_battery_passes_with_release_digests():
    report = drill.run_drills()
    assert report.passed is True
    statuses = {r["name"]: r["status"] for r in report.scenario_results}
    for name in RUNNABLE:
        assert statuses.get(name) == "passed", (name, statuses.get(name))
    assert report.commit_digest and report.commit_digest != "unknown"
    assert report.config_digest.startswith("sha256:")


def test_unknown_scenario_fails_the_gate():
    report = drill.run_drills(["does_not_exist"])
    assert report.passed is False
    assert report.scenario_results[0]["status"] == "unknown"


def test_happy_path_is_single_command_idempotent(tmp_path):
    detail = drill.scn_happy_path(str(tmp_path / "happy.duckdb"))
    assert detail["orders_submitted"] == 1
    assert detail["typed_calls"] >= 1
    assert detail["attribution_resolved"] is True
    assert detail["replay_digest"]


def test_intent_emitter_builds_identical_ids_for_duplicate_bar(tmp_path):
    stack = drill.build_stack(str(tmp_path / "ids.duckdb"))
    bar_ts = drill.NOW - __import__("datetime").timedelta(minutes=1)
    r1 = stack.emit_from_bar(bar_ts=bar_ts)
    r2 = stack.emit_from_bar(bar_ts=bar_ts)
    assert r1 is not None and r2 is not None
    assert r1.command_id == r2.command_id
    assert stack.emitter.last_intent is not None
    assert stack.emitter.last_intent.intent_id.startswith("intent-")
    assert stack.emitter.last_intent.command_id.startswith("auto-")


def test_strategy_runtime_wires_intent_emitter_not_ib_orders():
    """Automation path must not construct IB Order objects or use legacy RPC."""
    import inspect

    from trader.strategy import intent_emitter as mod
    from trader.strategy import strategy_runtime as rt

    source = inspect.getsource(mod) + inspect.getsource(rt.StrategyRuntime._dispatch_signal)
    assert "execute_automated_intent" in inspect.getsource(mod)
    assert "IntentEmitter" in inspect.getsource(rt)
    assert "ib_async.order.Order" not in source
    assert "place_expressive_order" not in source
    assert "place_order_simple" not in source


def test_strategy_service_api_exposes_automation_status():
    from trader.messaging.strategy_service_api import StrategyServiceApi

    class _Runtime:
        automation_enabled = False
        automation_live_enabled = False
        automation_strategy_name = ""
        automation_expected_artifact_id = ""
        automation_artifact_bundle_path = ""

    api = StrategyServiceApi(_Runtime())  # type: ignore[arg-type]
    status = api.get_automation_status()
    assert status["enabled"] is False
    assert status["live_enabled"] is False
    assert status["strategy_name"] == ""
    assert status["expected_artifact_id"] == ""


@pytest.mark.parametrize("method", LEGACY_MUTATIONS)
def test_emitter_never_registers_legacy_mutations(method, tmp_path):
    stack = drill.build_stack(str(tmp_path / "legacy.duckdb"))
    # Typed client only records execute_automated_intent; legacy list stays empty.
    stack.emit_from_bar(bar_ts=drill.NOW - __import__("datetime").timedelta(minutes=1))
    assert method not in stack.typed_client.legacy_rpc_calls
    assert all(c["method"] == "execute_automated_intent" for c in stack.typed_client.calls)
