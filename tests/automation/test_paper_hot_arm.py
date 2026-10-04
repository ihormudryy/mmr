"""Phase 2 hot-arm activation tests (ports + chaos injection)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from trader.automation.bundle_finder import EligibleBundle
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


def _with_eligible_bundle(tmp_path: Path):
    """Stand in for the bundle finder: plumbing tests do not need a real bundle.

    Creates the bundle directory and the key ring directory, which a real find
    leaves behind, so status() sees complete material.
    """
    bundle_path = tmp_path / "share" / "artifacts" / f"sha256_{ARTIFACT_ID}"

    def _stand_in(self, strategy_name, strategy, trader_data):
        bundle_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "keys" / "verify").mkdir(parents=True, exist_ok=True)
        return EligibleBundle(bundle_path, ARTIFACT_ID, NOW)

    return patch.object(PaperAutomationActivationService, "_eligible_bundle", _stand_in)


def test_hot_arm_activate_ends_armed(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    with _with_eligible_bundle(tmp_path):
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
    with _with_eligible_bundle(tmp_path):
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
    with _with_eligible_bundle(tmp_path):
        result = service.activate(strategy_name="orb_gld", reason="go")

    assert result["lifecycle"] == "armed_unpersisted"
    assert ports.trader_bound is not None
    assert service.status().lifecycle == "armed_unpersisted"
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is False

    # Retry re-checks the bundle, then completes persist when memory is already armed.
    service._fail_after = None
    with _with_eligible_bundle(tmp_path):
        retry = service.activate(strategy_name="orb_gld", reason="retry")
    assert retry["lifecycle"] == "armed"
    assert retry["reused_existing_keys"] is True
    trader = yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())
    assert trader["automation"]["enabled"] is True


def test_deactivate_tears_down_memory(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    with _with_eligible_bundle(tmp_path):
        service.activate(strategy_name="orb_gld", reason="go")

    result = service.deactivate(reason="stop")
    assert result["lifecycle"] == "disabled"
    assert result["restart_required"] is False
    assert ports.trader_bound is None
    assert ports.strategy_bound is None
    assert service.status().lifecycle == "disabled"


def test_hot_arm_refusal_keeps_its_code(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="go")

    assert exc.value.code == "NO_ELIGIBLE_BUNDLE"
    assert ports.calls == []
    assert service.status().lifecycle != "armed"
    assert service.status().phase is None


def _finder_returning(tmp_path: Path, bundles: list[EligibleBundle], calls: list[str]):
    """A bundle finder that returns the next bundle per call (the last one repeats)
    and refuses, as the real finder does, once the clock passes the bundle's expiry."""

    def _stand_in(self, strategy_name, strategy, trader_data):
        calls.append(strategy_name)
        bundle = bundles[min(len(calls), len(bundles)) - 1]
        if self._now() >= bundle.expires_at:
            raise self._no_eligible_bundle(f"{strategy_name}: attestation expired")
        bundle.path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "keys" / "verify").mkdir(parents=True, exist_ok=True)
        return bundle

    return patch.object(PaperAutomationActivationService, "_eligible_bundle", _stand_in)


def test_hot_arm_retry_refuses_changed_material_binding(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    artifacts = tmp_path / "share" / "artifacts"
    later = NOW + dt.timedelta(days=90)
    first = EligibleBundle(artifacts / f"sha256_{ARTIFACT_ID}", ARTIFACT_ID, later)
    newer = EligibleBundle(artifacts / f"sha256_{'b' * 64}", "b" * 64, later)
    calls: list[str] = []

    with _finder_returning(tmp_path, [first, newer], calls):
        service.activate(strategy_name="orb_gld", reason="first")
        before = list(ports.calls)
        with pytest.raises(PaperAutomationActivationError) as exc:
            service.activate(strategy_name="orb_gld", reason="a newer bundle exists")

    assert exc.value.code == "AUTOMATION_ALREADY_BOUND"
    assert ports.calls == before
    assert len(calls) == 2


def test_hot_arm_retry_rechecks_expired_evidence(tmp_path: Path) -> None:
    ports = RecordingHotArmPorts()
    service = _service(tmp_path, hot_arm=ports)
    bundle = EligibleBundle(
        tmp_path / "share" / "artifacts" / f"sha256_{ARTIFACT_ID}", ARTIFACT_ID,
        NOW + dt.timedelta(days=90))
    calls: list[str] = []

    with _finder_returning(tmp_path, [bundle], calls):
        service.activate(strategy_name="orb_gld", reason="first")
        before = list(ports.calls)
        service._now = lambda: NOW + dt.timedelta(days=100)
        with pytest.raises(PaperAutomationActivationError) as exc:
            service.activate(strategy_name="orb_gld", reason="retry")

    assert exc.value.code == "NO_ELIGIBLE_BUNDLE"
    assert len(calls) == 2  # the idempotent shortcut did not skip the check
    assert ports.calls == before
