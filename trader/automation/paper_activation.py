"""Restart-required activation for Phase 1 paper automation."""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from trader.automation.paper_materials import (
    PaperMaterialsError,
    default_key_paths,
    ensure_signing_keypair,
    export_fixture_paper_eligible_bundle,
)

logger = logging.getLogger(__name__)


class PaperAutomationActivationError(Exception):
    """A safe, coded refusal to change paper automation activation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PaperAutomationStatus:
    lifecycle: str
    strategy_name: str | None
    artifact_id: str | None
    public_key_ring_path: str | None
    artifact_bundle_path: str | None
    restart_required: bool
    armed_unpersisted: bool
    last_error: str | None
    command_authority_ready: bool
    account_mode: str
    last_activated_at: str | None
    phase: None = None


def _atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _load_yaml_mapping(path: Path) -> dict:
    with open(path) as handle:
        value = yaml.safe_load(handle)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected YAML mapping in {path}")
    return value


def _redacted_automation_diff(before: dict, after: dict) -> dict:
    keys = (
        "enabled",
        "live_enabled",
        "artifact_bundle_path",
        "public_key_ring_path",
        "expected_artifact_id",
        "strategy_name",
    )
    return {
        k: {"old": before.get(k), "new": after.get(k)}
        for k in keys
        if before.get(k) != after.get(k)
    }


def _redacted_strategy_params_diff(before: dict, after: dict) -> dict:
    keys = ("artifact_bundle_path",)
    return {
        k: {"old": before.get(k), "new": after.get(k)}
        for k in keys
        if before.get(k) != after.get(k)
    }


class PaperAutomationActivationService:
    """Prepare durable paper automation config without hot-arming services."""

    def __init__(
        self,
        trader_yaml_path: Path,
        strategy_yaml_path: Path,
        config_dir: Path,
        share_dir: Path,
        account_mode: str,
        command_authority_enabled: bool,
        now: Callable[[], dt.datetime],
    ) -> None:
        self._trader_yaml_path = Path(trader_yaml_path).expanduser()
        self._strategy_yaml_path = Path(strategy_yaml_path).expanduser()
        self._config_dir = Path(config_dir).expanduser()
        self._share_dir = Path(share_dir).expanduser()
        self._account_mode = str(account_mode).lower()
        self._command_authority_enabled = bool(command_authority_enabled)
        self._now = now
        self._last_activated_at: str | None = None
        self._last_error: str | None = None

    def status(self) -> PaperAutomationStatus:
        trader_data = _load_yaml_mapping(self._trader_yaml_path)
        automation = trader_data.get("automation") or {}
        enabled = bool(automation.get("enabled", False))
        strategy_name = automation.get("strategy_name") or None
        artifact_id = automation.get("expected_artifact_id") or None
        public_key_ring_path = automation.get("public_key_ring_path") or None
        artifact_bundle_path = automation.get("artifact_bundle_path") or None

        lifecycle = "disabled"
        restart_required = False
        last_error = self._last_error
        if enabled:
            missing_error = self._missing_material_error(
                artifact_bundle_path=artifact_bundle_path,
                public_key_ring_path=public_key_ring_path,
            )
            if missing_error is not None:
                lifecycle = "degraded"
                last_error = missing_error
            else:
                lifecycle = "restart_required"
                restart_required = True

        return PaperAutomationStatus(
            lifecycle=lifecycle,
            strategy_name=strategy_name,
            artifact_id=artifact_id,
            public_key_ring_path=public_key_ring_path,
            artifact_bundle_path=artifact_bundle_path,
            restart_required=restart_required,
            armed_unpersisted=False,
            last_error=last_error,
            command_authority_ready=self._command_authority_enabled,
            account_mode=self._account_mode,
            last_activated_at=self._last_activated_at,
            phase=None,
        )

    def activate(self, *, strategy_name: str, reason: str) -> dict:
        del reason
        trader_data = _load_yaml_mapping(self._trader_yaml_path)
        strategy_data = _load_yaml_mapping(self._strategy_yaml_path)
        automation = dict(trader_data.get("automation") or {})
        strategy = self._validate_activation(
            strategy_name=strategy_name,
            automation=automation,
            strategy_data=strategy_data,
        )

        private_key_path, verify_dir, public_key_path = default_key_paths(
            self._config_dir
        )
        try:
            signer, reused = ensure_signing_keypair(
                private_key_path=private_key_path,
                public_key_path=public_key_path,
            )
            artifact_id = export_fixture_paper_eligible_bundle(
                signer=signer,
                artifacts_root=self._share_dir / "artifacts",
            )
        except PaperMaterialsError as exc:
            self._last_error = str(exc)
            raise

        bundle_path = self._share_dir / "artifacts" / artifact_id
        before_automation = dict(automation)
        params = strategy.get("params")
        if params is None:
            params = {}
            strategy["params"] = params
        if not isinstance(params, dict):
            raise ValueError(f"strategy {strategy_name!r} params must be a mapping")
        before_params = dict(params)
        params["artifact_bundle_path"] = str(bundle_path)

        automation.update(
            {
                "enabled": True,
                "live_enabled": False,
                "artifact_bundle_path": str(bundle_path),
                "public_key_ring_path": str(verify_dir),
                "expected_artifact_id": artifact_id,
                "strategy_name": strategy_name,
            }
        )
        trader_data["automation"] = automation

        # Persist the strategy first; trader.yaml remains the sole enablement source.
        _atomic_write_yaml(self._strategy_yaml_path, strategy_data)
        _atomic_write_yaml(self._trader_yaml_path, trader_data)
        logger.info(
            "paper automation config updated: diff=%s",
            {
                "automation": _redacted_automation_diff(before_automation, automation),
                "strategy_params": _redacted_strategy_params_diff(
                    before_params, params
                ),
            },
        )
        self._last_activated_at = self._now().isoformat()
        self._last_error = None
        return {
            "lifecycle": "restart_required",
            "strategy_name": strategy_name,
            "artifact_id": artifact_id,
            "artifact_bundle_path": str(bundle_path),
            "public_key_ring_path": str(verify_dir),
            "restart_required": True,
            "reused_existing_keys": reused,
        }

    def deactivate(self, *, reason: str) -> dict:
        del reason
        trader_data = _load_yaml_mapping(self._trader_yaml_path)
        automation = dict(trader_data.get("automation") or {})
        automation["enabled"] = False
        trader_data["automation"] = automation
        _atomic_write_yaml(self._trader_yaml_path, trader_data)
        logger.info("paper automation config updated: %s", {"enabled": False})
        self._last_error = None
        return {
            "lifecycle": "restart_required",
            "restart_required": True,
        }

    def _validate_activation(
        self,
        *,
        strategy_name: str,
        automation: dict,
        strategy_data: dict,
    ) -> dict:
        if self._account_mode != "paper":
            raise PaperAutomationActivationError(
                "NOT_PAPER", "paper automation requires a paper account"
            )
        if not self._command_authority_enabled:
            raise PaperAutomationActivationError(
                "COMMAND_AUTHORITY_REQUIRED",
                "command authority must be enabled before paper automation",
            )
        if bool(automation.get("live_enabled", False)):
            raise PaperAutomationActivationError(
                "LIVE_AUTOMATION_REFUSED",
                "live automation cannot be activated by the paper service",
            )

        strategies = strategy_data.get("strategies") or []
        strategy = next(
            (
                item
                for item in strategies
                if isinstance(item, dict) and item.get("name") == strategy_name
            ),
            None,
        )
        if strategy is None:
            raise PaperAutomationActivationError(
                "STRATEGY_NOT_FOUND", f"strategy {strategy_name!r} was not found"
            )
        if strategy.get("auto_execute") == "propose":
            raise PaperAutomationActivationError(
                "STRATEGY_HAS_PROPOSE",
                f"strategy {strategy_name!r} already uses auto_execute: propose",
            )

        bound_strategy = automation.get("strategy_name")
        if (
            bool(automation.get("enabled", False))
            and bound_strategy
            and bound_strategy != strategy_name
        ):
            raise PaperAutomationActivationError(
                "AUTOMATION_ALREADY_BOUND",
                f"paper automation is already bound to {bound_strategy!r}",
            )
        return strategy

    @staticmethod
    def _missing_material_error(
        *,
        artifact_bundle_path: str | None,
        public_key_ring_path: str | None,
    ) -> str | None:
        if not artifact_bundle_path or not Path(artifact_bundle_path).is_dir():
            return f"artifact bundle missing: {artifact_bundle_path or '<unset>'}"
        if not public_key_ring_path or not Path(public_key_ring_path).is_dir():
            return f"public key ring missing: {public_key_ring_path or '<unset>'}"
        return None
