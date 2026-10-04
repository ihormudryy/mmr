"""Tests for restart-required paper automation activation."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from trader.automation.paper_activation import (
    PaperAutomationActivationError,
    PaperAutomationActivationService,
    _redacted_automation_diff,
    _redacted_strategy_params_diff,
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


def _configured_service(tmp_path, **bundle_kwargs):
    # Synthetic TEST data representing a pre-existing offline research export.
    # Production activation must only read it, never mint or alter evidence.
    from .paper_evidence_helpers import research_bundle

    bundle_path, key_ring = research_bundle(tmp_path, **bundle_kwargs)
    service = _service(
        tmp_path,
        automation={
            "enabled": False, "live_enabled": False,
            "artifact_bundle_path": str(bundle_path),
            "public_key_ring_path": str(key_ring),
            "expected_artifact_id": bundle_path.name, "strategy_name": "orb_gld",
        },
        strategies=[{
            "name": "orb_gld", "module": "strategies/orb.py",
            "class_name": "OpeningRangeBreakout", "params": {"minutes": 30},
        }],
    )
    return service, bundle_path, key_ring


@pytest.mark.parametrize("hot_arm", [False, True])
def test_activation_consumes_existing_evidence_without_signing_key(tmp_path, hot_arm):
    from trader.automation.paper_hot_arm import RecordingHotArmPorts

    service, bundle_path, key_ring = _configured_service(tmp_path)
    if hot_arm:
        service._hot_arm = RecordingHotArmPorts()
    before = {p: p.read_bytes() for p in bundle_path.iterdir()}

    result = service.activate(strategy_name="orb_gld", reason="reviewed offline")

    assert result["artifact_bundle_path"] == str(bundle_path)
    assert result["artifact_id"] == bundle_path.name
    assert result["public_key_ring_path"] == str(key_ring)
    assert result["lifecycle"] == ("armed" if hot_arm else "restart_required")
    assert {p: p.read_bytes() for p in before} == before
    assert not (tmp_path / "config" / "keys").exists()
    assert not (tmp_path / "share").exists()


@pytest.mark.parametrize("hot_arm", [False, True])
def test_activation_without_research_evidence_has_no_side_effects(tmp_path, hot_arm):
    from trader.automation.paper_hot_arm import RecordingHotArmPorts

    service = _service(tmp_path)
    ports = RecordingHotArmPorts()
    if hot_arm:
        service._hot_arm = ports
    before = {p: p.read_bytes() for p in (tmp_path / "config").glob("*.yaml")}

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="not research evidence")

    assert exc.value.code == "RESEARCH_EVIDENCE_REQUIRED"
    assert {p: p.read_bytes() for p in before} == before
    assert not (tmp_path / "config" / "keys").exists()
    assert not (tmp_path / "share").exists()
    assert ports.calls == []


@pytest.mark.parametrize("change", [
    {"module": "strategies/other.py"},
    {"class_name": "OtherStrategy"},
    {"params": {"minutes": 15}},
    {"params": {"minutes": 30, "unresearched_override": True}},
])
def test_activation_rejects_strategy_not_bound_to_research(tmp_path, change):
    service, _, _ = _configured_service(tmp_path)
    path = tmp_path / "config" / "strategy_runtime.yaml"
    data = yaml.safe_load(path.read_text())
    data["strategies"][0].update(change)
    _write_yaml(path, data)

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="wrong strategy")

    assert exc.value.code == "RESEARCH_EVIDENCE_INVALID"
    assert yaml.safe_load((tmp_path / "config" / "trader.yaml").read_text())["automation"]["enabled"] is False


def test_activate_refuses_without_command_authority(tmp_path: Path) -> None:
    service = _service(tmp_path, command_authority_enabled=False)

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="operator approved")

    assert exc.value.code == "COMMAND_AUTHORITY_REQUIRED"


@pytest.mark.parametrize("failure", ["tampered", "untrusted", "expired", "malformed_key", "legacy_fixture"])
@pytest.mark.parametrize("hot_arm", [False, True])
def test_invalid_research_never_persists_or_commits(tmp_path, failure, hot_arm):
    from trader.automation.paper_hot_arm import RecordingHotArmPorts
    from trader.research.signing import AttestationSigner

    service, bundle, keys = _configured_service(
        tmp_path, **({"reviewer": "bootstrap"} if failure == "legacy_fixture" else {}),
    )
    ports = RecordingHotArmPorts()
    if hot_arm:
        service._hot_arm = ports
    if failure == "tampered":
        target = bundle / "trials.json"
        target.chmod(0o644)
        target.write_text("[]")
        target.chmod(0o444)
    elif failure == "untrusted":
        (keys / "research.pem").write_bytes(AttestationSigner.generate().public_key_pem())
    elif failure == "malformed_key":
        (keys / "research.pem").write_text("not a public key")
    elif failure == "expired":
        service._now = lambda: NOW + dt.timedelta(days=100)
    before = {p: p.read_bytes() for p in (tmp_path / "config").glob("*.yaml")}

    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb_gld", reason="invalid evidence")

    assert exc.value.code == "RESEARCH_EVIDENCE_INVALID"
    assert ports.calls == []
    assert {p: p.read_bytes() for p in before} == before
    assert not (tmp_path / "config" / "keys").exists()


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


def test_redacted_automation_diff_reports_only_changed_keys() -> None:
    before = {"enabled": False, "strategy_name": ""}
    after = {
        "enabled": True,
        "live_enabled": False,
        "strategy_name": "orb_gld",
        "artifact_bundle_path": "/tmp/bundle",
    }

    diff = _redacted_automation_diff(before, after)

    assert diff == {
        "enabled": {"old": False, "new": True},
        "strategy_name": {"old": "", "new": "orb_gld"},
        "artifact_bundle_path": {"old": None, "new": "/tmp/bundle"},
        "live_enabled": {"old": None, "new": False},
    }
    assert "private_key_path" not in diff


def test_redacted_strategy_params_diff_never_includes_private_keys() -> None:
    before = {"RANGE_MINUTES": 45}
    after = {"RANGE_MINUTES": 45, "artifact_bundle_path": "/tmp/bundle"}

    diff = _redacted_strategy_params_diff(before, after)

    assert diff == {
        "artifact_bundle_path": {"old": None, "new": "/tmp/bundle"},
    }
    assert "RANGE_MINUTES" not in diff


def test_activate_logs_redacted_diff_shape(tmp_path: Path, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    service, _, _ = _configured_service(tmp_path)
    service.activate(strategy_name="orb_gld", reason="operator approved")

    assert any("diff=" in record.message for record in caplog.records)
    log_text = " ".join(record.message for record in caplog.records)
    assert "private_key" not in log_text
    assert "BEGIN" not in log_text


def test_activate_writes_atomic_yaml_and_returns_restart_required(
    tmp_path: Path,
) -> None:
    service, bundle_path, key_ring = _configured_service(tmp_path)
    result = service.activate(
        strategy_name="orb_gld", reason="operator approved paper activation",
    )

    config_dir = tmp_path / "config"
    assert result == {
        "lifecycle": "restart_required",
        "strategy_name": "orb_gld",
        "artifact_id": bundle_path.name,
        "artifact_bundle_path": str(bundle_path),
        "public_key_ring_path": str(key_ring),
        "restart_required": True,
        "reused_existing_keys": True,
    }

    trader_data = yaml.safe_load((config_dir / "trader.yaml").read_text())
    assert trader_data["unrelated"] == {"preserved": True}
    assert trader_data["automation"] == {
        "enabled": True,
        "live_enabled": False,
        "artifact_bundle_path": str(bundle_path),
        "public_key_ring_path": str(key_ring),
        "expected_artifact_id": bundle_path.name,
        "strategy_name": "orb_gld",
    }
    strategy_data = yaml.safe_load(
        (config_dir / "strategy_runtime.yaml").read_text()
    )
    strategy = strategy_data["strategies"][0]
    assert strategy["params"]["minutes"] == 30
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


def test_activate_reuses_existing_public_keys(tmp_path: Path) -> None:
    service, _, key_ring = _configured_service(tmp_path)
    before = (key_ring / "research.pem").read_bytes()
    first = service.activate(strategy_name="orb_gld", reason="first")
    second = service.activate(strategy_name="orb_gld", reason="retry")

    assert first["reused_existing_keys"] is True
    assert second["reused_existing_keys"] is True
    assert (key_ring / "research.pem").read_bytes() == before


def test_activate_deactivate_activate_reuses_materials(tmp_path: Path) -> None:
    service, bundle_path, _ = _configured_service(tmp_path)

    first = service.activate(strategy_name="orb_gld", reason="first activation")
    service.deactivate(reason="operator paused")
    second = service.activate(strategy_name="orb_gld", reason="second activation")

    assert first["artifact_id"] == second["artifact_id"]
    assert first["artifact_bundle_path"] == second["artifact_bundle_path"]
    assert second["reused_existing_keys"] is True
    assert len(list(bundle_path.parent.iterdir())) == 1


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
