"""Phase 2 hot-arm activation tests (ports + chaos injection)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from trader.automation.paper_activation import (
    PaperAutomationActivationError,
    PaperAutomationActivationService,
)
from trader.automation.paper_hot_arm import RecordingHotArmPorts


NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
ARTIFACT_ID = "a" * 64


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _service(
    tmp_path: Path,
    *,
    hot_arm: RecordingHotArmPorts | None = None,
    fail_after: str | None = None,
) -> PaperAutomationActivationService:
    trader_yaml = tmp_path / "config" / "trader.yaml"
    strategy_yaml = tmp_path / "config" / "strategy_runtime.yaml"
    _write_yaml(
        trader_yaml,
        {
            "automation": {
                "enabled": False,
                "live_enabled": False,
                "artifact_bundle_path": "",
                "public_key_ring_path": "",
                "expected_artifact_id": "",
                "strategy_name": "",
            },
        },
    )
    _write_yaml(
        strategy_yaml,
        {
            "strategies": [
                {
                    "name": "orb_gld",
                    "module": "strategies/opening_range_breakout.py",
                    "params": {"RANGE_MINUTES": 45},
                }
            ],
        },
    )
    return PaperAutomationActivationService(
        trader_yaml_path=trader_yaml,
        strategy_yaml_path=strategy_yaml,
        config_dir=tmp_path / "config",
        share_dir=tmp_path / "share",
        account_mode="paper",
        command_authority_enabled=True,
        now=lambda: NOW,
        hot_arm=hot_arm,
        fail_after=fail_after,
    )


def _fake_export(*, signer, artifacts_root: Path) -> str:
    del signer
    (artifacts_root / ARTIFACT_ID).mkdir(parents=True, exist_ok=True)
    return ARTIFACT_ID


def test_hot_arm_activate_ends_armed(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        result = service.activate(strategy_name="orb_gld", reason="go")

    assert result["lifecycle"] == "armed"
    assert result["restart_required"] is False
    assert ports.trader_bound["strategy_name"] == "orb_gld"
    assert ports.strategy_bound["artifact_id"] == ARTIFACT_ID
    assert service.status().lifecycle == "armed"
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is True


def test_strategy_commit_failure_compensates(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts(fail_after="strategy_commit")
    service = _service(tmp_path, hot_arm=ports)
    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        with pytest.raises(PaperAutomationActivationError) as exc:
            service.activate(strategy_name="orb_gld", reason="go")
    assert exc.value.code == "HOT_ARM_FAILED"
    assert "strategy_compensate" in ports.calls
    assert "trader_compensate" in ports.calls
    assert ports.trader_bound is None
    assert service.status().lifecycle == "failed"
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is False


def test_persist_failure_leaves_armed_unpersisted(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports, fail_after="persist")
    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        result = service.activate(strategy_name="orb_gld", reason="go")

    assert result["lifecycle"] == "armed_unpersisted"
    assert ports.trader_bound is not None
    assert service.status().lifecycle == "armed_unpersisted"
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is False

    # Retry completes persist without re-export when memory already armed.
    service._fail_after = None
    retry = service.activate(strategy_name="orb_gld", reason="retry")
    assert retry["lifecycle"] == "armed"
    assert retry["reused_existing_keys"] is True
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is True


def test_deactivate_tears_down_memory(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        service.activate(strategy_name="orb_gld", reason="go")

    result = service.deactivate(reason="stop")
    assert result["lifecycle"] == "disabled"
    assert result["restart_required"] is False
    assert ports.trader_bound is None
    assert ports.strategy_bound is None
    assert service.status().lifecycle == "disabled"
