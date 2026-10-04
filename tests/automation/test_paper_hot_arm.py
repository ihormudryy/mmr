"""Phase 2 hot-arm activation tests (ports + chaos injection)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from trader.automation.paper_activation import (
    PaperAutomationActivationError,
    PaperAutomationActivationService,
)
from trader.automation.paper_hot_arm import RecordingHotArmPorts
from .test_paper_activation import _configured_service


NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _service(
    tmp_path: Path,
    *,
    hot_arm: RecordingHotArmPorts | None = None,
    fail_after: str | None = None,
) -> PaperAutomationActivationService:
    service, _, _ = _configured_service(tmp_path)
    service._hot_arm = hot_arm
    service._fail_after = fail_after
    return service


def test_hot_arm_activate_ends_armed(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    result = service.activate(strategy_name="orb_gld", reason="go")

    assert result["lifecycle"] == "armed"
    assert result["restart_required"] is False
    assert ports.trader_bound["strategy_name"] == "orb_gld"
    assert ports.strategy_bound["artifact_id"] == result["artifact_id"]
    assert service.status().lifecycle == "armed"
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is True


def test_strategy_commit_failure_compensates(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts(fail_after="strategy_commit")
    service = _service(tmp_path, hot_arm=ports)
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
    service.activate(strategy_name="orb_gld", reason="go")

    result = service.deactivate(reason="stop")
    assert result["lifecycle"] == "disabled"
    assert result["restart_required"] is False
    assert ports.trader_bound is None
    assert ports.strategy_bound is None
    assert service.status().lifecycle == "disabled"


def test_hot_arm_retry_refuses_changed_material_binding(tmp_path):
    import shutil

    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    result = service.activate(strategy_name="orb_gld", reason="first")
    other_bundle = tmp_path / "different-bundle-path"
    shutil.copytree(result["artifact_bundle_path"], other_bundle)
    trader_path = tmp_path / "config" / "trader.yaml"
    data = yaml.safe_load(trader_path.read_text())
    data["automation"]["artifact_bundle_path"] = str(other_bundle)
    _write_yaml(trader_path, data)
    before = list(ports.calls)

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="changed binding")

    assert exc.value.code == "AUTOMATION_ALREADY_BOUND"
    assert ports.calls == before


def test_hot_arm_retry_rechecks_expired_evidence(tmp_path):
    service = _service(tmp_path, hot_arm=RecordingHotArmPorts())
    service.activate(strategy_name="orb_gld", reason="first")
    service._now = lambda: NOW + dt.timedelta(days=100)
    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="retry")
    assert exc.value.code == "RESEARCH_EVIDENCE_INVALID"
