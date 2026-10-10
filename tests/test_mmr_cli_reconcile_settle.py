"""Issue #121: ``mmr reconcile unknown`` and ``mmr reconcile settle``; bare ``mmr reconcile`` is unchanged."""
from __future__ import annotations

import json

import pytest

from trader import mmr_cli
from trader.common.reactivex import SuccessFail
from trader.mmr_cli import build_parser

ROW = {"command_id": "pause-1", "action": "pause_trading", "state": "OUTCOME_UNKNOWN",
       "error_code": "RECEIVED_AT_RESTART", "target_type": "account", "target_id": "DU1",
       "created_at": "2026-10-10T14:00:00+00:00", "updated_at": "2026-10-10T14:00:01+00:00",
       "operator_settleable": True}


class FakeSdk:
    def __init__(self, settle=None):
        self.calls = []
        self.settle_result = settle or SuccessFail.success(
            obj={"command_id": "pause-1", "action": "pause_trading", "state": "REJECTED",
                 "error_code": "OPERATOR_SETTLED", "settled_by": "cli", "reason": "checked"})

    def reconcile(self):
        self.calls.append("reconcile")
        return {"findings": []}

    def unresolved_commands(self):
        self.calls.append("unresolved")
        return {"account_id": "DU1", "commands": [ROW]}

    def settle_unknown_command(self, target_command_id, outcome, reason):
        self.calls.append(("settle", target_command_id, outcome, reason))
        return self.settle_result


def run(sdk, *argv):
    mmr_cli._handle_reconcile(sdk, build_parser().parse_args(["reconcile", *argv]))


def test_bare_reconcile_still_runs_the_broker_report(monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk)
    assert sdk.calls == ["reconcile"]


def test_settle_sends_the_target_outcome_and_reason_and_prints_the_receipt(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "settle", "pause-1", "--outcome", "rejected", "--reason", "checked")
    assert sdk.calls == [("settle", "pause-1", "rejected", "checked")]
    assert json.loads(capsys.readouterr().out)["data"]["error_code"] == "OPERATOR_SETTLED"


def test_settle_refusal_is_printed_as_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk(settle=SuccessFail.fail(error="settle_unknown_command rejected: SETTLE_ACTION_FORBIDDEN"))
    run(sdk, "settle", "appr-1", "--outcome", "resolved", "--reason", "x")
    printed = json.loads(capsys.readouterr().out)
    assert printed["success"] is False and "SETTLE_ACTION_FORBIDDEN" in printed["message"]


def test_settle_needs_an_outcome_and_a_reason():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["reconcile", "settle", "pause-1", "--outcome", "maybe", "--reason", "x"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["reconcile", "settle", "pause-1", "--outcome", "resolved"])


def test_unknown_lists_the_rows(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    run(FakeSdk(), "unknown")
    assert json.loads(capsys.readouterr().out)["data"]["commands"] == [ROW]
