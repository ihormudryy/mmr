"""SP2 Plan 3 Task 4 and SP2c Plan 2: ``mmr ai-deployment register-discretionary | show | withdraw | version``."""
from __future__ import annotations

import pytest

from trader import mmr_cli
from trader.automation.discretionary_deployment import DEFAULT_SCOPE_RULE
from trader.common.reactivex import SuccessFail
from trader.mmr_cli import build_parser

DIGEST = "sha256:" + "d" * 64


VERSION = "sha256:" + "e" * 64
VERSION_VIEW = {"found": True, "version": {
    "version_digest": VERSION, "base_digest": DIGEST, "judgment_id": "jdg-1", "kind": "INITIAL",
    "prior_version_digest": None, "first_session": "2026-10-12", "expiry_session": "2026-11-06", "state": "ACTIVE"}}


class FakeSdk:
    def __init__(self, kind="discretionary", withdraw=None, version_view=None):
        self.calls = []
        self.kind = kind
        self.withdraw_result = withdraw or SuccessFail.success(
            obj={"version_digest": VERSION, "withdrawn": True, "already_withdrawn": False})
        self.version_view = version_view or VERSION_VIEW

    def withdraw_ai_deployment(self, version_digest, reason):
        self.calls.append({"withdraw": version_digest, "reason": reason})
        return self.withdraw_result

    def ai_deployment_version(self, version_digest):
        self.calls.append({"version": version_digest})
        return self.version_view

    def register_discretionary_deployment(self, *, operator, statement, rule=None, style="intraday_long"):
        self.calls.append({"operator": operator, "statement": statement, "rule": rule, "style": style})
        return SuccessFail.success(obj={"digest": DIGEST, "created": True, "kind": "discretionary"})

    def ai_deployment(self, digest):
        return {"digest": digest, "kind": self.kind, "deployment": {}, "strategy_digest_provenance": None,
                "error_code": None}


def run(sdk, *argv):
    mmr_cli._handle_ai_deployment(sdk, build_parser().parse_args(["ai-deployment", *argv]))


def test_register_sends_only_the_narrowed_parts(monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "register-discretionary", "--operator", "owner", "--statement", "x", "--stock-types", "ETF")
    assert sdk.calls == [{"operator": "owner", "statement": "x", "rule": {"stock_types": ["ETF"]},
                          "style": "intraday_long"}]


def test_register_parses_every_flag(monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "register-discretionary", "--operator", "owner", "--statement", "x", "--exchanges", "NYSE,NASDAQ",
        "--min-price", "10", "--min-dollar-volume", "30000000", "--max-order-share", "0.005")
    assert sdk.calls[0]["rule"] == {"primary_exchanges": ["NYSE", "NASDAQ"], "min_price": 10.0,
                                    "min_median_dollar_volume": 30_000_000.0,
                                    "max_order_share_of_dollar_volume": 0.005}


@pytest.mark.parametrize("missing", [["--operator", "owner"], ["--statement", "x"]])
def test_operator_and_statement_are_required(missing):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ai-deployment", "register-discretionary", *missing])


def test_show_prints_the_discretionary_label(capsys, monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", False)
    run(FakeSdk(), "show", DIGEST)
    assert "DISCRETIONARY (operator attested, no backtest evidence)" in capsys.readouterr().out


def test_show_of_a_strategy_deployment_has_no_label(capsys, monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", False)
    run(FakeSdk(kind="strategy"), "show", DIGEST)
    assert "DISCRETIONARY" not in capsys.readouterr().out


def test_sdk_builds_the_body_from_the_default_rule(monkeypatch):
    from types import SimpleNamespace
    from trader.domain.commands import CommandReceipt
    from trader.sdk import MMR

    calls = []
    client = SimpleNamespace(call=lambda method, body, kind: calls.append((method, body)) or CommandReceipt(
        "aidep-x", "aidep-x", "RESOLVED", {"digest": DIGEST, "created": True, "kind": "discretionary"}, None, False))
    monkeypatch.setattr(MMR, "_typed_command", property(lambda self: client))
    result = MMR.__new__(MMR).register_discretionary_deployment(operator="owner", statement="x",
                                                                rule={"stock_types": ["ETF"]})
    assert result.is_success() and result.obj["kind"] == "discretionary"
    method, body = calls[0]
    assert method == "register_discretionary_deployment"
    deployment = body["deployment"]
    assert deployment["scope_rule"] == {**DEFAULT_SCOPE_RULE.to_json(), "stock_types": ["ETF"]}
    assert (deployment["kind"], deployment["style"], deployment["attestation"]["operator"]) == (
        "discretionary", "intraday_long", "owner")


def test_withdraw_sends_the_version_and_reason(capsys, monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "withdraw", VERSION, "--reason", "bad fills")
    assert sdk.calls == [{"withdraw": VERSION, "reason": "bad fills"}]
    assert '"withdrawn": true' in capsys.readouterr().out


def test_withdraw_needs_a_reason():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ai-deployment", "withdraw", VERSION])


def test_a_refused_withdraw_names_the_code_and_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", False)
    refused = SuccessFail.fail(error="withdraw_ai_deployment rejected: DEPLOYMENT_VERSION_UNKNOWN")
    with pytest.raises(SystemExit) as exited:
        run(FakeSdk(withdraw=refused), "withdraw", VERSION, "--reason", "x")
    assert exited.value.code == 1
    assert "DEPLOYMENT_VERSION_UNKNOWN" in capsys.readouterr().out


def test_version_prints_the_state(capsys, monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    sdk = FakeSdk()
    run(sdk, "version", VERSION)
    assert sdk.calls == [{"version": VERSION}]
    assert '"state": "ACTIVE"' in capsys.readouterr().out


def test_an_unknown_version_exits_1(capsys, monkeypatch):
    monkeypatch.setattr(mmr_cli, "_json_mode", False)
    with pytest.raises(SystemExit) as exited:
        run(FakeSdk(version_view={"found": False, "version": None}), "version", VERSION)
    assert exited.value.code == 1
    assert "no sealed deployment version" in capsys.readouterr().out


def _sdk_with_command(monkeypatch, reply):
    from types import SimpleNamespace
    from trader.sdk import MMR

    calls = []

    def call(method, body, kind):
        calls.append((method, body))
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(MMR, "_typed_command", property(lambda self: SimpleNamespace(call=call)))
    return MMR.__new__(MMR), calls


def test_sdk_withdraw_sends_the_body_and_returns_the_outcome(monkeypatch):
    from trader.domain.commands import CommandReceipt
    outcome = {"version_digest": VERSION, "withdrawn": True, "already_withdrawn": False}
    sdk, calls = _sdk_with_command(monkeypatch, CommandReceipt("aidw-x", "aidw-x", "RESOLVED", outcome, None, False))
    result = sdk.withdraw_ai_deployment(VERSION, "bad fills")
    assert result.is_success() and result.obj == outcome
    assert calls == [("withdraw_ai_deployment", {"version_digest": VERSION, "reason": "bad fills"})]


def test_sdk_withdraw_reports_a_refusal_code(monkeypatch):
    from trader.domain.commands import CommandReceipt
    from trader.messaging.typed_rpc import TypedRpcRemoteError
    sdk, _ = _sdk_with_command(monkeypatch, CommandReceipt("aidw-x", "aidw-x", "REJECTED",
                                                           {"message": "no sealed version"},
                                                           "DEPLOYMENT_VERSION_UNKNOWN", False))
    result = sdk.withdraw_ai_deployment(VERSION, "x")
    assert not result.is_success() and "DEPLOYMENT_VERSION_UNKNOWN: no sealed version" in str(result.error)
    sdk, _ = _sdk_with_command(monkeypatch, TypedRpcRemoteError("PERMISSION_DENIED", "no"))
    assert "PERMISSION_DENIED" in str(sdk.withdraw_ai_deployment(VERSION, "x").error)
