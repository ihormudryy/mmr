"""SP3 Plan 3 Task 10: mmr ai-author list | prove-isolation [--in-container] (spec 7.9; D3; rulings 14-15)."""
from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

import pytest

import trader.authored.isolation_host as host
import trader.authored.prove_isolation as runner
import trader.messaging.keys_cli as keys_cli
import trader.mmr_cli as mmr_cli
from trader.mmr_cli import _LOCAL_ONLY_COMMANDS, _handle_ai_author, build_parser

PROOF = {"image_digest": "a1b2c3d-20261011T220000Z", "passed": True, "ran_at": "2026-10-11T22:00:00+00:00",
         "checks": [{"name": "network_none", "passed": True, "detail": "interfaces=['lo']"}]}


def args(action, in_container=False, **extra):
    return argparse.Namespace(ai_author_action=action, compose="docker compose", in_container=in_container,
                              **extra)


def test_the_parser_knows_both_actions_and_the_in_container_step():
    parser = build_parser()
    assert parser.parse_args(["ai-author", "list"]).ai_author_action == "list"
    parsed = parser.parse_args(["ai-author", "prove-isolation", "--compose", "podman compose"])
    assert (parsed.ai_author_action, parsed.compose, parsed.in_container) == (
        "prove-isolation", "podman compose", False)
    assert parser.parse_args(["ai-author", "prove-isolation", "--in-container"]).in_container is True
    assert "ai-author" in _LOCAL_ONLY_COMMANDS                    # typed RPC only, no legacy connect


def test_list_prints_the_records(capsys):
    mmr = SimpleNamespace(authored_strategies=lambda: {"enabled": False, "counted": 0, "records": []})
    _handle_ai_author(mmr, args("list"))
    assert "counted" in capsys.readouterr().out


def test_the_host_step_refuses_inside_a_container(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: True)
    with pytest.raises(SystemExit) as exit_:
        _handle_ai_author(SimpleNamespace(), args("prove-isolation"))
    assert exit_.value.code == 2


def test_the_in_container_step_refuses_on_the_host(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: False)
    with pytest.raises(SystemExit) as exit_:
        mmr_cli._prove_isolation_in_container()
    assert exit_.value.code == 2


def test_the_in_container_step_builds_no_sdk_and_needs_no_key(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: True)
    monkeypatch.setattr(runner, "run_in_container", lambda: 0)
    monkeypatch.setattr(mmr_cli, "MMR", lambda: pytest.fail("the in-container step must not build an SDK"))
    monkeypatch.setattr(sys, "argv", ["mmr", "ai-author", "prove-isolation", "--in-container"])
    with pytest.raises(SystemExit) as exit_:
        mmr_cli.main()
    assert exit_.value.code == 0


def test_prove_isolation_records_the_proof_as_the_operator(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: False)
    monkeypatch.setattr(host, "prove_isolation", lambda compose: PROOF)
    recorded = []
    mmr = SimpleNamespace(record_isolation_proof=lambda body: recorded.append(body) or {"status": "RECORDED",
                                                                                          "proof": body})
    _handle_ai_author(mmr, args("prove-isolation"))
    assert recorded == [PROOF]


def test_a_failing_proof_is_recorded_and_exits_1(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: False)
    failing = {**PROOF, "passed": False, "checks": [{**PROOF["checks"][0], "passed": False}]}
    monkeypatch.setattr(host, "prove_isolation", lambda compose: failing)
    recorded = []
    mmr = SimpleNamespace(record_isolation_proof=lambda body: recorded.append(body) or {"status": "RECORDED"})
    with pytest.raises(SystemExit) as exit_:
        _handle_ai_author(mmr, args("prove-isolation"))
    assert exit_.value.code == 1 and recorded == [failing]
