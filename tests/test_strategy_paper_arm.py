"""Tests for strategy-service paper automation hot-arm / disarm."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.strategy.strategy_runtime import PaperAutomationArmError, StrategyRuntime
from trader.strategy.intent_emitter import IntentEmitter
import datetime as dt


NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
ARTIFACT_ID = "a" * 64


def _make_runtime(tmp_path, **overrides) -> StrategyRuntime:
    kwargs = dict(
        ib_server_address="127.0.0.1",
        ib_server_port=4002,
        strategy_runtime_ib_client_id=99,
        duckdb_path=str(tmp_path / "x.duckdb"),
        universe_library="u",
        zmq_pubsub_server_address="tcp://127.0.0.1",
        zmq_pubsub_server_port=1,
        zmq_rpc_server_address="tcp://127.0.0.1",
        zmq_rpc_server_port=2,
        zmq_strategy_rpc_server_address="tcp://127.0.0.1",
        zmq_strategy_rpc_server_port=3,
        zmq_messagebus_server_address="tcp://127.0.0.1",
        zmq_messagebus_server_port=4,
        strategies_directory=str(tmp_path),
        strategy_config_file=str(tmp_path / "strategy_runtime.yaml"),
        paper_trading=True,
    )
    kwargs.update(overrides)
    return StrategyRuntime(**kwargs)


def _verified() -> VerifiedArtifact:
    return VerifiedArtifact(
        artifact_id=ARTIFACT_ID,
        manifest_digest="sha256:manifest",
        dataset_manifest_digest="sha256:dataset",
        parameters={},
        allowlist=(),
        max_gross_allocation=0.06,
        expires_at=NOW + dt.timedelta(days=30),
        public_key_id="key-1",
        verification_reason_codes=("OK",),
    )


def test_arm_requires_trader_client(tmp_path):
    rt = _make_runtime(tmp_path)
    with pytest.raises(PaperAutomationArmError) as exc:
        rt.arm_paper_automation(
            strategy_name="orb_gld",
            artifact_bundle_path=str(tmp_path / "bundle"),
            public_key_ring_path=str(tmp_path / "keys"),
            expected_artifact_id=ARTIFACT_ID,
        )
    assert exc.value.code == "TRADER_CLIENT_MISSING"


def test_arm_builds_emitter_and_disarm_clears(tmp_path, monkeypatch):
    rt = _make_runtime(tmp_path)
    rt._trader_command_client = MagicMock()

    def _fake_verify(strategy_name, bundle_path):
        rt._verified_artifact = _verified()
        rt._verified_artifact_strategy = strategy_name
        rt._maybe_build_intent_emitter()

    monkeypatch.setattr(rt, "_verify_artifact_at_load", _fake_verify)

    result = rt.arm_paper_automation(
        strategy_name="orb_gld",
        artifact_bundle_path=str(tmp_path / "artifacts" / ARTIFACT_ID),
        public_key_ring_path=str(tmp_path / "keys" / "verify"),
        expected_artifact_id=ARTIFACT_ID,
    )
    assert result["armed"] is True
    assert result["strategy_name"] == "orb_gld"
    assert isinstance(rt.intent_emitter, IntentEmitter)
    assert rt.get_paper_automation_arm() == {
        "armed": True,
        "strategy_name": "orb_gld",
        "artifact_id": ARTIFACT_ID,
    }

    cleared = rt.disarm_paper_automation()
    assert cleared["armed"] is False
    assert rt.intent_emitter is None
    assert rt.automation_enabled is False
    assert rt.get_paper_automation_arm()["armed"] is False


def test_arm_refuses_live_trading(tmp_path):
    rt = _make_runtime(tmp_path, paper_trading=False)
    rt._trader_command_client = MagicMock()
    with pytest.raises(PaperAutomationArmError) as exc:
        rt.arm_paper_automation(
            strategy_name="orb_gld",
            artifact_bundle_path="/x",
            public_key_ring_path="/y",
            expected_artifact_id=ARTIFACT_ID,
        )
    assert exc.value.code == "NOT_PAPER"
