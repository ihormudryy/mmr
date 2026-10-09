"""SP2 Plan 1 Task 8: the operator publishes the initial AI risk policy (spec amendment 6.7)."""
from __future__ import annotations

import json

import pytest

from tests.test_ai_paper_rpc import command, query, served  # noqa: F401 (fixture)
from trader import mmr_cli
from trader.automation.ai_policy_file import PolicyFileError, load_policy_file
from trader.automation.risk_limits import PAPER_LIMITS
from trader.messaging.principals import TRADER_ACL
from trader.mmr_cli import build_parser

SP2_PLAN1_ROWS = {
    ("command", "grant_ai_controller_epoch"): {"ai_supervisor"},
    ("query", "read_ai_signals"): {"ai_supervisor"},
    ("query", "get_ai_paper_decision"): {"ai_supervisor"},
    ("command", "publish_ai_risk_policy"): {"ai_supervisor", "cli"},
}


def rights(principal):
    return {key for key, allowed in TRADER_ACL.items() if principal in allowed}


def test_plan1_rows_are_exact_and_research_gets_none():
    assert {key: set(TRADER_ACL[key]) for key in SP2_PLAN1_ROWS} == SP2_PLAN1_ROWS
    assert not set(SP2_PLAN1_ROWS) & rights("ai_research")
    assert not set(SP2_PLAN1_ROWS) & rights("dashboard")


def test_ai_research_rights_are_exactly_the_sp1_set():
    from trader.messaging.principals import _TRADER_MARKET_READS
    expected = {("query", m) for m in _TRADER_MARKET_READS} | {
        ("query", "resolve_instrument"), ("query", "discover_instrument"),
        ("command", "register_ai_deployment"), ("query", "get_ai_deployment"),
        ("command", "record_backtest_judgment"), ("query", "get_backtest_judgment")}     # SP2c Plan 1
    assert rights("ai_research") == expected


def test_cli_gains_only_the_publish():
    new_for_cli = {key for key in SP2_PLAN1_ROWS if "cli" in SP2_PLAN1_ROWS[key]}
    assert new_for_cli == {("command", "publish_ai_risk_policy")}
    assert ("command", "submit_ai_paper_decision") not in rights("cli")


def test_operator_publishes_through_signed_rpc(served):
    out = command(served, "cli").call(
        "publish_ai_risk_policy", {"command_id": "cli-pol-1", "limits": PAPER_LIMITS.to_json(), "reason": "init"},
        dict)
    assert (out["state"], out["outcome"]["revision"]) == ("RESOLVED", 1)
    view = query(served, "cli").call("get_ai_risk_policy", {}, dict)
    assert view["latest_published_revision"] == 1


def write(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return path


def test_policy_file_loads_exact_limits(tmp_path):
    lines = "\n".join(f"  {k}: {v}" for k, v in PAPER_LIMITS.to_json().items())
    assert load_policy_file(write(tmp_path, f"limits:\n{lines}\n")) == PAPER_LIMITS.to_json()


@pytest.mark.parametrize("text", ["", "- 1\n", "limits: 3\n", "limits: {max_positions: 3}\n",
                                  "limits: {}\nextra: 1\n", "!!python/object:os.system {}\n"])
def test_bad_policy_files_fail_loudly(tmp_path, text):
    with pytest.raises(PolicyFileError):
        load_policy_file(write(tmp_path, text))


class FakeSdk:
    def __init__(self):
        self.calls = []

    def ai_policy_publish(self, limits, reason, command_id=None):
        from trader.common.reactivex import SuccessFail
        self.calls.append((limits, reason, command_id))
        return SuccessFail.success(obj={"revision": 1, "applied_now": [], "queued": []})

    def ai_policy_view(self):
        return {"latest_published_revision": 1}


def test_cli_publish_reads_the_file_and_sends_the_reason(tmp_path, capsys, monkeypatch):
    lines = "\n".join(f"  {k}: {v}" for k, v in PAPER_LIMITS.to_json().items())
    path = write(tmp_path, f"limits:\n{lines}\n")
    sdk = FakeSdk()
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    mmr_cli._handle_ai_policy(sdk, build_parser().parse_args(
        ["ai-policy", "publish", str(path), "--reason", "initial", "--command-id", "cli-pol-7"]))
    assert sdk.calls == [(PAPER_LIMITS.to_json(), "initial", "cli-pol-7")]
    assert json.loads(capsys.readouterr().out)["data"]["revision"] == 1


def test_sdk_publish_uses_a_colon_free_cli_command_id(monkeypatch):
    from types import SimpleNamespace
    from trader.domain.commands import CommandReceipt
    from trader.sdk import MMR

    calls = []
    client = SimpleNamespace(call=lambda method, body, kind: calls.append((method, body)) or CommandReceipt(
        body["command_id"], body["command_id"], "RESOLVED", {"revision": 2}, None, False))
    mmr = MMR.__new__(MMR)
    monkeypatch.setattr(MMR, "_typed_command", property(lambda self: client))
    assert mmr.ai_policy_publish(PAPER_LIMITS.to_json(), "init").is_success()
    method, body = calls[0]
    assert method == "publish_ai_risk_policy" and body["command_id"].startswith("cli-pol-")
    assert ":" not in body["command_id"] and body["reason"] == "init"
