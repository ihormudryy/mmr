"""Paper automation activation — Phase 1 (restart) and Phase 2 (hot-arm)."""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from trader.automation.paper_hot_arm import PaperHotArmPorts
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
    phase: str | None = None


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
    """Activate/deactivate paper automation (YAML + optional hot-arm ports)."""

    def __init__(
        self,
        trader_yaml_path: Path,
        strategy_yaml_path: Path,
        config_dir: Path,
        share_dir: Path,
        account_mode: str,
        command_authority_enabled: bool,
        now: Callable[[], dt.datetime],
        hot_arm: PaperHotArmPorts | None = None,
        *,
        fail_after: str | None = None,
    ) -> None:
        self._trader_yaml_path = Path(trader_yaml_path).expanduser()
        self._strategy_yaml_path = Path(strategy_yaml_path).expanduser()
        self._config_dir = Path(config_dir).expanduser()
        self._share_dir = Path(share_dir).expanduser()
        self._account_mode = str(account_mode).lower()
        self._command_authority_enabled = bool(command_authority_enabled)
        self._now = now
        self._hot_arm = hot_arm
        self._fail_after = fail_after
        self._last_activated_at: str | None = None
        self._last_error: str | None = None
        self._phase: str | None = None
        self._lifecycle_override: str | None = None  # failed
        self._memory_armed = False
        self._memory_strategy: str | None = None
        self._memory_artifact_id: str | None = None
        self._memory_bundle_path: str | None = None
        self._memory_key_ring: str | None = None

    def mark_runtime_armed(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        """Called at trader startup when YAML already armed the stack."""
        self._memory_armed = True
        self._memory_strategy = strategy_name
        self._memory_artifact_id = artifact_id
        self._memory_bundle_path = artifact_bundle_path
        self._memory_key_ring = public_key_ring_path
        self._lifecycle_override = None
        self._last_error = None

    def status(self) -> PaperAutomationStatus:
        trader_data = _load_yaml_mapping(self._trader_yaml_path)
        automation = trader_data.get("automation") or {}
        enabled = bool(automation.get("enabled", False))
        strategy_name = (
            self._memory_strategy
            or automation.get("strategy_name")
            or None
        )
        artifact_id = (
            self._memory_artifact_id
            or automation.get("expected_artifact_id")
            or None
        )
        public_key_ring_path = (
            self._memory_key_ring
            or automation.get("public_key_ring_path")
            or None
        )
        artifact_bundle_path = (
            self._memory_bundle_path
            or automation.get("artifact_bundle_path")
            or None
        )

        lifecycle = "disabled"
        restart_required = False
        armed_unpersisted = False
        last_error = self._last_error

        if self._lifecycle_override == "failed":
            lifecycle = "failed"
        elif self._memory_armed and not enabled:
            lifecycle = "armed_unpersisted"
            armed_unpersisted = True
        elif self._memory_armed and enabled:
            missing_error = self._missing_material_error(
                artifact_bundle_path=artifact_bundle_path,
                public_key_ring_path=public_key_ring_path,
            )
            if missing_error is not None:
                lifecycle = "degraded"
                last_error = missing_error
            else:
                lifecycle = "armed"
        elif enabled:
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
            armed_unpersisted=armed_unpersisted,
            last_error=last_error,
            command_authority_ready=self._command_authority_enabled,
            account_mode=self._account_mode,
            last_activated_at=self._last_activated_at,
            phase=self._phase,
        )

    def activate(self, *, strategy_name: str, reason: str) -> dict:
        del reason
        if self._hot_arm is not None:
            return self._activate_hot_arm(strategy_name=strategy_name)
        return self._activate_restart_required(strategy_name=strategy_name)

    def deactivate(self, *, reason: str) -> dict:
        del reason
        if self._hot_arm is not None:
            return self._deactivate_hot_arm()
        return self._deactivate_restart_required()

    def _activate_restart_required(self, *, strategy_name: str) -> dict:
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
        self._persist_enable(
            trader_data=trader_data,
            strategy_data=strategy_data,
            strategy=strategy,
            strategy_name=strategy_name,
            automation=automation,
            bundle_path=bundle_path,
            verify_dir=verify_dir,
            artifact_id=artifact_id,
        )
        self._last_activated_at = self._now().isoformat()
        self._last_error = None
        self._lifecycle_override = None
        return {
            "lifecycle": "restart_required",
            "strategy_name": strategy_name,
            "artifact_id": artifact_id,
            "artifact_bundle_path": str(bundle_path),
            "public_key_ring_path": str(verify_dir),
            "restart_required": True,
            "reused_existing_keys": reused,
        }

    def _activate_hot_arm(self, *, strategy_name: str) -> dict:
        assert self._hot_arm is not None
        # Idempotent: already armed for the same strategy.
        if (
            self._memory_armed
            and self._memory_strategy == strategy_name
            and self.status().lifecycle == "armed"
        ):
            return {
                "lifecycle": "armed",
                "strategy_name": strategy_name,
                "artifact_id": self._memory_artifact_id,
                "artifact_bundle_path": self._memory_bundle_path,
                "public_key_ring_path": self._memory_key_ring,
                "restart_required": False,
                "reused_existing_keys": True,
            }

        trader_data = _load_yaml_mapping(self._trader_yaml_path)
        strategy_data = _load_yaml_mapping(self._strategy_yaml_path)
        automation = dict(trader_data.get("automation") or {})
        strategy = self._validate_activation(
            strategy_name=strategy_name,
            automation=automation,
            strategy_data=strategy_data,
        )

        # armed_unpersisted retry: memory already matches — persist only.
        if (
            self._memory_armed
            and self._memory_strategy == strategy_name
            and self._memory_artifact_id
            and self._memory_bundle_path
            and self._memory_key_ring
        ):
            return self._persist_after_hot_arm(
                trader_data=trader_data,
                strategy_data=strategy_data,
                strategy=strategy,
                strategy_name=strategy_name,
                automation=automation,
                artifact_id=self._memory_artifact_id,
                bundle_path=Path(self._memory_bundle_path),
                verify_dir=Path(self._memory_key_ring),
                reused=True,
            )

        private_key_path, verify_dir, public_key_path = default_key_paths(
            self._config_dir
        )
        try:
            self._phase = "prepare_keys"
            self._inject_fail("prepare_keys")
            signer, reused = ensure_signing_keypair(
                private_key_path=private_key_path,
                public_key_path=public_key_path,
            )

            self._phase = "export_artifact"
            self._inject_fail("export_artifact")
            artifact_id = export_fixture_paper_eligible_bundle(
                signer=signer,
                artifacts_root=self._share_dir / "artifacts",
            )
            bundle_path = self._share_dir / "artifacts" / artifact_id

            self._phase = "trader_commit"
            self._hot_arm.trader_commit(
                strategy_name=strategy_name,
                artifact_id=artifact_id,
                artifact_bundle_path=str(bundle_path),
                public_key_ring_path=str(verify_dir),
            )
            trader_committed = True
            self._inject_fail("trader_commit")

            self._phase = "strategy_commit"
            self._hot_arm.strategy_commit(
                strategy_name=strategy_name,
                artifact_id=artifact_id,
                artifact_bundle_path=str(bundle_path),
                public_key_ring_path=str(verify_dir),
            )
            strategy_committed = True
            self._inject_fail("strategy_commit")

            self._phase = "verify"
            self._hot_arm.verify_ready(
                strategy_name=strategy_name,
                artifact_id=artifact_id,
            )
            self._inject_fail("verify")

            self._memory_armed = True
            self._memory_strategy = strategy_name
            self._memory_artifact_id = artifact_id
            self._memory_bundle_path = str(bundle_path)
            self._memory_key_ring = str(verify_dir)
            self._lifecycle_override = None

            return self._persist_after_hot_arm(
                trader_data=trader_data,
                strategy_data=strategy_data,
                strategy=strategy,
                strategy_name=strategy_name,
                automation=automation,
                artifact_id=artifact_id,
                bundle_path=bundle_path,
                verify_dir=verify_dir,
                reused=reused,
            )
        except Exception as exc:
            # Compensate both sides; ports must be idempotent.
            try:
                self._hot_arm.strategy_compensate()
            except Exception:
                logger.exception("strategy_compensate failed during activate")
            try:
                self._hot_arm.trader_compensate()
            except Exception:
                logger.exception("trader_compensate failed during activate")
            self._clear_memory_arm()
            self._phase = None
            self._lifecycle_override = "failed"
            self._last_error = str(exc)
            if isinstance(exc, (PaperAutomationActivationError, PaperMaterialsError)):
                raise
            # Errno 30 / EROFS: Compose used to mount artifacts :ro into trader.
            # Hot-arm Activate must write the fixture bundle there — surface a
            # actionable hint instead of a bare OSError.
            err = getattr(exc, "errno", None)
            if err in (30, getattr(__import__("errno"), "EROFS", 30)) or (
                isinstance(exc, OSError) and "Read-only file system" in str(exc)
            ):
                raise PaperAutomationActivationError(
                    "HOT_ARM_FAILED",
                    f"{exc} — trader's artifacts volume must be writable "
                    f"(remove :ro from the artifacts mount in docker-compose.yml "
                    f"for the trader service, recreate the container, retry)",
                ) from exc
            raise PaperAutomationActivationError("HOT_ARM_FAILED", str(exc)) from exc

    def _persist_after_hot_arm(
        self,
        *,
        trader_data: dict,
        strategy_data: dict,
        strategy: dict,
        strategy_name: str,
        automation: dict,
        artifact_id: str,
        bundle_path: Path,
        verify_dir: Path,
        reused: bool,
    ) -> dict:
        self._phase = "persist"
        try:
            self._inject_fail("persist")
            self._persist_enable(
                trader_data=trader_data,
                strategy_data=strategy_data,
                strategy=strategy,
                strategy_name=strategy_name,
                automation=automation,
                bundle_path=bundle_path,
                verify_dir=verify_dir,
                artifact_id=artifact_id,
            )
        except Exception as exc:
            self._phase = None
            self._last_error = str(exc)
            # Memory stays armed — durable enable failed.
            return {
                "lifecycle": "armed_unpersisted",
                "strategy_name": strategy_name,
                "artifact_id": artifact_id,
                "artifact_bundle_path": str(bundle_path),
                "public_key_ring_path": str(verify_dir),
                "restart_required": False,
                "reused_existing_keys": reused,
                "armed_unpersisted": True,
            }

        self._phase = None
        self._last_activated_at = self._now().isoformat()
        self._last_error = None
        self._lifecycle_override = None
        return {
            "lifecycle": "armed",
            "strategy_name": strategy_name,
            "artifact_id": artifact_id,
            "artifact_bundle_path": str(bundle_path),
            "public_key_ring_path": str(verify_dir),
            "restart_required": False,
            "reused_existing_keys": reused,
        }

    def _deactivate_restart_required(self) -> dict:
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

    def _deactivate_hot_arm(self) -> dict:
        assert self._hot_arm is not None
        try:
            self._hot_arm.strategy_compensate()
        except Exception:
            logger.exception("strategy_compensate failed during deactivate")
        try:
            self._hot_arm.trader_compensate()
        except Exception:
            logger.exception("trader_compensate failed during deactivate")
        self._clear_memory_arm()
        self._phase = None
        self._lifecycle_override = None

        yaml_error: str | None = None
        try:
            trader_data = _load_yaml_mapping(self._trader_yaml_path)
            automation = dict(trader_data.get("automation") or {})
            automation["enabled"] = False
            trader_data["automation"] = automation
            _atomic_write_yaml(self._trader_yaml_path, trader_data)
            logger.info("paper automation config updated: %s", {"enabled": False})
        except Exception as exc:
            yaml_error = str(exc)
            logger.exception("failed to persist automation.enabled=false")

        self._last_error = yaml_error
        return {
            "lifecycle": "disabled",
            "restart_required": False,
            "last_error": yaml_error,
        }

    def _persist_enable(
        self,
        *,
        trader_data: dict,
        strategy_data: dict,
        strategy: dict,
        strategy_name: str,
        automation: dict,
        bundle_path: Path,
        verify_dir: Path,
        artifact_id: str,
    ) -> None:
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

    def _clear_memory_arm(self) -> None:
        self._memory_armed = False
        self._memory_strategy = None
        self._memory_artifact_id = None
        self._memory_bundle_path = None
        self._memory_key_ring = None

    def _inject_fail(self, phase: str) -> None:
        if self._fail_after == phase:
            raise RuntimeError(f"injected failure after {phase}")

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
        # Allow retry for the same bound strategy (re-Activate / armed_unpersisted).
        if (
            bool(automation.get("enabled", False))
            and bound_strategy
            and bound_strategy != strategy_name
            and not (
                self._memory_armed and self._memory_strategy == strategy_name
            )
        ):
            raise PaperAutomationActivationError(
                "AUTOMATION_ALREADY_BOUND",
                f"paper automation is already bound to {bound_strategy!r}",
            )
        if (
            self._memory_armed
            and self._memory_strategy
            and self._memory_strategy != strategy_name
        ):
            raise PaperAutomationActivationError(
                "AUTOMATION_ALREADY_BOUND",
                f"paper automation is already bound to {self._memory_strategy!r}",
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
