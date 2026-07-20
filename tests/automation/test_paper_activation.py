"""Tests for restart-required paper automation activation."""
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


NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
ARTIFACT_ID = "a" * 64


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _service(
    tmp_path: Path,
    *,
    account_mode: str = "paper",
    command_authority_enabled: bool = True,
    automation: dict | None = None,
    strategies: list[dict] | None = None,
) -> PaperAutomationActivationService:
    trader_yaml = tmp_path / "config" / "trader.yaml"
    strategy_yaml = tmp_path / "config" / "strategy_runtime.yaml"
    _write_yaml(
        trader_yaml,
        {
            "unrelated": {"preserved": True},
            "automation": automation
            or {
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
            "env": "TRADER_CHECK=False",
            "strategies": strategies
            or [
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
        account_mode=account_mode,
        command_authority_enabled=command_authority_enabled,
        now=lambda: NOW,
    )


def _fake_export(*, signer, artifacts_root: Path) -> str:
    del signer
    (artifacts_root / ARTIFACT_ID).mkdir(parents=True, exist_ok=True)
    return ARTIFACT_ID


def test_activate_refuses_without_command_authority(tmp_path: Path) -> None:
    service = _service(tmp_path, command_authority_enabled=False)

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="operator approved")

    assert exc.value.code == "COMMAND_AUTHORITY_REQUIRED"


def test_activate_refuses_strategy_with_propose_mode(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        strategies=[
            {
                "name": "orb_gld",
                "module": "strategies/opening_range_breakout.py",
                "auto_execute": "propose",
            }
        ],
    )

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="operator approved")

    assert exc.value.code == "STRATEGY_HAS_PROPOSE"


@pytest.mark.parametrize(
    ("service_kwargs", "strategy_name", "expected_code"),
    [
        ({"account_mode": "live"}, "orb_gld", "NOT_PAPER"),
        (
            {
                "automation": {
                    "enabled": False,
                    "live_enabled": True,
                    "strategy_name": "",
                }
            },
            "orb_gld",
            "LIVE_AUTOMATION_REFUSED",
        ),
        ({}, "missing", "STRATEGY_NOT_FOUND"),
        (
            {
                "automation": {
                    "enabled": True,
                    "live_enabled": False,
                    "strategy_name": "other_strategy",
                }
            },
            "orb_gld",
            "AUTOMATION_ALREADY_BOUND",
        ),
    ],
)
def test_activate_uses_documented_gate_error_codes(
    tmp_path: Path,
    service_kwargs: dict,
    strategy_name: str,
    expected_code: str,
) -> None:
    service = _service(tmp_path, **service_kwargs)

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name=strategy_name, reason="operator approved")

    assert exc.value.code == expected_code


def test_activate_writes_atomic_yaml_and_returns_restart_required(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        result = service.activate(
            strategy_name="orb_gld",
            reason="operator approved paper activation",
        )

    config_dir = tmp_path / "config"
    bundle_path = tmp_path / "share" / "artifacts" / ARTIFACT_ID
    assert result == {
        "lifecycle": "restart_required",
        "strategy_name": "orb_gld",
        "artifact_id": ARTIFACT_ID,
        "artifact_bundle_path": str(bundle_path),
        "public_key_ring_path": str(config_dir / "keys" / "verify"),
        "restart_required": True,
        "reused_existing_keys": False,
    }

    trader_data = yaml.safe_load((config_dir / "trader.yaml").read_text())
    assert trader_data["unrelated"] == {"preserved": True}
    assert trader_data["automation"] == {
        "enabled": True,
        "live_enabled": False,
        "artifact_bundle_path": str(bundle_path),
        "public_key_ring_path": str(config_dir / "keys" / "verify"),
        "expected_artifact_id": ARTIFACT_ID,
        "strategy_name": "orb_gld",
    }
    strategy_data = yaml.safe_load(
        (config_dir / "strategy_runtime.yaml").read_text()
    )
    strategy = strategy_data["strategies"][0]
    assert strategy["params"]["RANGE_MINUTES"] == 45
    assert strategy["params"]["artifact_bundle_path"] == str(bundle_path)
    assert "auto_execute" not in strategy
    assert not (config_dir / "trader.yaml.tmp").exists()
    assert not (config_dir / "strategy_runtime.yaml.tmp").exists()

    status = service.status()
    assert status.lifecycle == "restart_required"
    assert status.restart_required is True
    assert status.last_activated_at == NOW.isoformat()
    assert status.phase is None
    assert status.armed_unpersisted is False


def test_activate_reuses_existing_signing_keys(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        first = service.activate(strategy_name="orb_gld", reason="first")

    service = _service(tmp_path)
    with patch(
        "trader.automation.paper_activation.export_fixture_paper_eligible_bundle",
        _fake_export,
    ):
        second = service.activate(strategy_name="orb_gld", reason="retry")

    assert first["reused_existing_keys"] is False
    assert second["reused_existing_keys"] is True


def test_deactivate_clears_enabled_and_requires_restart(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        automation={
            "enabled": True,
            "live_enabled": False,
            "artifact_bundle_path": "/tmp/artifact",
            "public_key_ring_path": "/tmp/keys",
            "expected_artifact_id": ARTIFACT_ID,
            "strategy_name": "orb_gld",
        },
    )

    result = service.deactivate(reason="operator stopped automation")

    assert result == {
        "lifecycle": "restart_required",
        "restart_required": True,
    }
    trader_data = yaml.safe_load(
        (tmp_path / "config" / "trader.yaml").read_text()
    )
    assert trader_data["automation"]["enabled"] is False
    assert trader_data["automation"]["strategy_name"] == "orb_gld"


def test_status_reports_restart_required_for_complete_enabled_config(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "share" / "artifacts" / ARTIFACT_ID
    bundle_path.mkdir(parents=True)
    (tmp_path / "config" / "keys" / "verify").mkdir(parents=True)
    service = _service(
        tmp_path,
        automation={
            "enabled": True,
            "live_enabled": False,
            "artifact_bundle_path": str(bundle_path),
            "public_key_ring_path": str(tmp_path / "config" / "keys" / "verify"),
            "expected_artifact_id": ARTIFACT_ID,
            "strategy_name": "orb_gld",
        },
    )

    status = service.status()

    assert status.lifecycle == "restart_required"
    assert status.artifact_bundle_path == str(bundle_path)
    assert status.last_error is None


def test_status_reports_degraded_when_enabled_artifact_is_missing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "share" / "artifacts" / ARTIFACT_ID
    (tmp_path / "config" / "keys" / "verify").mkdir(parents=True)
    service = _service(
        tmp_path,
        automation={
            "enabled": True,
            "live_enabled": False,
            "artifact_bundle_path": str(missing),
            "public_key_ring_path": str(tmp_path / "config" / "keys" / "verify"),
            "expected_artifact_id": ARTIFACT_ID,
            "strategy_name": "orb_gld",
        },
    )

    status = service.status()

    assert status.lifecycle == "degraded"
    assert status.restart_required is False
    assert status.last_error == f"artifact bundle missing: {missing}"
