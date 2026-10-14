"""Issue #181: ``mmr breaker status`` and ``mmr breaker reset --reason``."""
from __future__ import annotations

import json

import pytest

from trader import mmr_cli
from trader.common.reactivex import SuccessFail
from trader.mmr_cli import build_parser

TRIPPED = {"account_id": "DU1", "state": "TRIPPED", "reason_code": "LIQUIDATION_FAILED",
           "reason": "account owner claimed", "revision": 2, "tripped_at": "2026-10-07T19:45:04+00:00",
           "reset_at": None, "reset_command_id": None}


class FakeSdk:
    def __init__(self, reset=None):
        self.calls = []
        self.reset_result = reset or SuccessFail.success(obj={**TRIPPED, "state": "CLEAR",
                                                              "reset_command_id": "sdk-1"})

    def circuit_breaker(self):
        self.calls.append("status")
        return TRIPPED

    def reset_circuit_breaker(self, reason):
        self.calls.append(("reset", reason))
        return self.reset_result


def run(sdk, *argv):
    mmr_cli._handle_breaker(sdk, build_parser().parse_args(["breaker", *argv]))


def test_status_prints_the_state_and_the_trip_reason(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "status")
    assert sdk.calls == ["status"]
    assert json.loads(capsys.readouterr().out)["data"]["reason_code"] == "LIQUIDATION_FAILED"


def test_reset_sends_the_reason_and_prints_the_cleared_state(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "reset", "--reason", "oct 7 flatten reached FLAT")
    assert sdk.calls == [("reset", "oct 7 flatten reached FLAT")]
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "CLEAR"


def test_a_refused_reset_is_printed_as_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk(reset=SuccessFail.fail(error="reset_circuit_breaker rejected: BREAKER_RESET_REFUSED"))
    run(sdk, "reset", "--reason", "x")
    printed = json.loads(capsys.readouterr().out)
    assert printed["success"] is False and "BREAKER_RESET_REFUSED" in printed["message"]


def test_reset_needs_a_reason():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["breaker", "reset"])
