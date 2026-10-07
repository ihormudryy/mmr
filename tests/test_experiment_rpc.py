"""SP1 Plan 4 Task 7: the experiment methods over typed RPC, on the real command stack and registry."""
from __future__ import annotations

import pytest
import yaml

from tests.test_ai_paper_rpc import _served, command, query
from trader.automation.ai_paper_config import load_ai_paper_config
from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.trading.command_coordinator import CommandRequest

ACCOUNT = "DU111111"


def _config(pct, enabled=True):
    raw = {"enabled": enabled}
    if pct is not None:
        raw["experiment_kill_drawdown_pct"] = pct
    return load_ai_paper_config(raw, trading_mode="paper")


def _write_yaml(tmp_path, pct):
    section = {"enabled": True}
    if pct is not None:
        section["experiment_kill_drawdown_pct"] = pct
    (tmp_path / "trader.yaml").write_text(yaml.safe_dump({"ai_paper": section}))


@pytest.fixture
def served(tmp_path, monkeypatch):
    stack = _served(tmp_path, monkeypatch, _config(20.0))
    _write_yaml(tmp_path, 20.0)
    stack.stack.experiments.monitor.recover()      # trader_service recovers it before readiness
    yield stack
    stack.close()


@pytest.fixture
def served_disabled(tmp_path, monkeypatch):
    stack = _served(tmp_path, monkeypatch, _config(None, enabled=False))
    yield stack
    stack.close()


def start(served, command_id="s1", principal="cli"):
    return command(served, principal).call("start_experiment", {"command_id": command_id, "reason": "go"}, dict)


def experiment_id(served):
    return served.stack.experiments.store.active().experiment_id


def body(method, served, command_id="c1"):
    if method == "get_experiment":
        return {}
    if method == "start_experiment":
        return {"command_id": command_id, "reason": "go"}
    return {"command_id": command_id, "experiment_id": "exp-" + "a" * 20, "reason": "r"}


def test_cli_starts_and_dashboard_reads(served):
    out = start(served)
    assert out["state"] == "RESOLVED" and out["outcome"]["state"] == "ARMED", out
    view = query(served, "dashboard").call("get_experiment", {}, dict)
    assert view["experiment"]["state"] == "ARMED"
    assert view["experiment"]["start_net_liquidation"] == 1_000_000.0
    assert view["kill_line"]["detection"] == "IB account updates, about 3 minutes; paper only"
    assert view["mode_conflict"] is None and [t["to_state"] for t in view["transitions"]] == ["ARMED"]


def test_get_experiment_without_an_experiment(served):
    view = query(served, "ai_supervisor").call("get_experiment", {}, dict)
    assert view["experiment"] is None and view["entry_block"] is None
    assert view["kill_line"]["active"] == {"pct": 20.0, "basis": "start"}


def test_edited_yaml_is_never_shown_as_active(tmp_path, monkeypatch):            # K9
    served = _served(tmp_path, monkeypatch, _config(20.0))
    _write_yaml(tmp_path, 20.0)
    try:
        start(served)
        _write_yaml(tmp_path, 10.0)                                              # edit on disk, no restart
        line = query(served, "cli").call("get_experiment", {}, dict)["kill_line"]
        assert (line["active"]["pct"], line["configured"]["pct"], line["pending_restart"]) == (20.0, 10.0, True)
    finally:
        served.close()
    restarted = _served(tmp_path, monkeypatch, _config(10.0))                   # the trader reads trader.yaml again
    _write_yaml(tmp_path, 10.0)
    try:
        line = query(restarted, "cli").call("get_experiment", {}, dict)["kill_line"]
        assert (line["active"]["pct"], line["pending_restart"]) == (10.0, False)  # the post-restart verification
    finally:
        restarted.close()


def test_a_looser_edit_is_never_active_for_a_running_experiment(served, tmp_path):
    start(served)
    _write_yaml(tmp_path, 30.0)
    line = query(served, "cli").call("get_experiment", {}, dict)["kill_line"]
    assert line["active"]["pct"] == 20.0 and line["configured"]["pct"] == 20.0 and line["pending_restart"] is False


def test_an_unreadable_yaml_is_reported_not_applied(served, tmp_path):
    start(served)
    (tmp_path / "trader.yaml").write_text(yaml.safe_dump({"ai_paper": {"experiment_kill_drawdown_pct": True}}))
    line = query(served, "cli").call("get_experiment", {}, dict)["kill_line"]
    assert (line["active"]["pct"], line["configured"], line["pending_restart"]) == (20.0, "UNREADABLE", False)


@pytest.mark.parametrize("principal,method", [
    ("ai_supervisor", "start_experiment"), ("ai_supervisor", "resume_experiment"),
    ("ai_supervisor", "stop_experiment"), ("ai_research", "pause_experiment"),
    ("ai_research", "get_experiment"), ("strategy", "start_experiment"), ("strategy", "get_experiment")])
def test_wrong_principal_is_denied(served, principal, method):
    role = "query" if method == "get_experiment" else "command"
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client(principal, "trader", role).call(method, body(method, served), dict)
    assert exc.value.code == "PERMISSION_DENIED"


def test_ai_supervisor_may_pause(served):
    start(served)
    out = command(served, "ai_supervisor").call(
        "pause_experiment", {"command_id": "p1", "experiment_id": experiment_id(served), "reason": "risk"}, dict)
    assert out["outcome"]["state"] == "PAUSED"
    out = command(served, "cli").call(
        "resume_experiment", {"command_id": "r1", "experiment_id": experiment_id(served), "reason": "ok"}, dict)
    assert out["outcome"]["state"] == "ARMED"


def test_service_refuses_a_wrong_principal_that_bypasses_the_acl(served):
    start(served)
    receipt = served.coordinator.execute(CommandRequest(
        command_id="r-bypass", action="resume_experiment", account_id=ACCOUNT, target_type="experiment",
        target_id=experiment_id(served), expected_version=None,
        body={"experiment_id": experiment_id(served), "reason": "r"}, source="ai_supervisor",
        principal="ai_supervisor"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"


@pytest.mark.parametrize("patch", [{"reason": True}, {"reason": 1}, {"reason": ""}, {"reason": "x" * 201},
    {"experiment_id": 7}, {"experiment_id": True}, {"experiment_id": "exp-1"}, {"command_id": "a:b"},
    {"extra": 1}])
def test_wire_is_strict(served, patch):
    start(served)
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, "cli").call(
            "pause_experiment",
            {"command_id": "p1", "experiment_id": experiment_id(served), "reason": "r", **patch}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("patch", [{"reason": True}, {"extra": 1}, {"command_id": 5}])
def test_start_wire_is_strict(served, patch):
    with pytest.raises(TypedRpcRemoteError) as exc:
        command(served, "cli").call("start_experiment", {"command_id": "s1", "reason": "go", **patch}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_get_experiment_takes_no_arguments(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "cli").call("get_experiment", {"experiment_id": "x"}, dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_same_command_id_replays_the_receipt(served):
    first = start(served, command_id="s1")
    assert start(served, command_id="s1") == first


def test_stop_needs_a_flat_account_then_a_new_start_is_a_new_experiment(served):
    start(served)
    first = experiment_id(served)
    out = command(served, "cli").call(
        "stop_experiment", {"command_id": "x1", "experiment_id": first, "reason": "done"}, dict)
    assert out["outcome"]["state"] == "STOPPED"
    again = start(served, command_id="s2")
    assert again["outcome"]["state"] == "ARMED" and again["outcome"]["experiment_id"] != first


def test_start_refused_with_ai_paper_disabled(served_disabled):
    out = start(served_disabled)
    assert (out["state"], out["error_code"]) == ("REJECTED", "AI_PAPER_DISABLED")
    assert query(served_disabled, "cli").call("get_experiment", {}, dict)["experiment"] is None


def test_identity_check_attached_after_the_registry(served):
    assert served.stack.experiments.service.identity_problem() is None


def test_no_experiment_method_writes_the_kill_line_or_ceiling():
    assert not [m for _, m in TRADER_ACL if "kill" in m or "ceiling" in m]
