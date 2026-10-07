"""SP2 Plan 3 Task 4: ``mmr ai-deployment register-discretionary | show``."""
from __future__ import annotations

import pytest

from trader import mmr_cli
from trader.automation.discretionary_deployment import DEFAULT_SCOPE_RULE
from trader.common.reactivex import SuccessFail
from trader.mmr_cli import build_parser

DIGEST = "sha256:" + "d" * 64


class FakeSdk:
    def __init__(self, kind="discretionary"):
        self.calls = []
        self.kind = kind

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
