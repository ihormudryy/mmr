"""Hot-arm ports for Phase 2 paper automation activation.

The activation service owns the phase machine; these ports commit/compensate
trader registry state and strategy IntentEmitter state without process restart.
"""
from __future__ import annotations

from typing import Protocol


class PaperHotArmPorts(Protocol):
    """Cross-process commit surface used by ``PaperAutomationActivationService``."""

    def trader_commit(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        """Build + late-register ``execute_automated_intent`` for the binding."""

    def trader_compensate(self) -> None:
        """Unregister or refuse-wrap automated intent; clear trader binding."""

    def strategy_commit(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        """Arm strategy_service IntentEmitter for the binding."""

    def strategy_compensate(self) -> None:
        """Disarm strategy IntentEmitter + clear in-memory automation flags."""

    def verify_ready(self, *, strategy_name: str, artifact_id: str) -> None:
        """Fail closed unless trader + strategy agree on strategy/artifact."""


class RecordingHotArmPorts:
    """In-memory ports for unit tests (optional fail-after injection)."""

    def __init__(self, *, fail_after: str | None = None) -> None:
        self.fail_after = fail_after
        self.calls: list[str] = []
        self.trader_bound: dict | None = None
        self.strategy_bound: dict | None = None
        self._trader_ready = False
        self._strategy_ready = False

    def _maybe_fail(self, phase: str) -> None:
        self.calls.append(phase)
        if self.fail_after == phase:
            raise RuntimeError(f"injected failure after {phase}")

    def trader_commit(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        self.trader_bound = {
            "strategy_name": strategy_name,
            "artifact_id": artifact_id,
            "artifact_bundle_path": artifact_bundle_path,
            "public_key_ring_path": public_key_ring_path,
        }
        self._trader_ready = True
        self._maybe_fail("trader_commit")

    def trader_compensate(self) -> None:
        self.calls.append("trader_compensate")
        self.trader_bound = None
        self._trader_ready = False

    def strategy_commit(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        self.strategy_bound = {
            "strategy_name": strategy_name,
            "artifact_id": artifact_id,
            "artifact_bundle_path": artifact_bundle_path,
            "public_key_ring_path": public_key_ring_path,
        }
        self._strategy_ready = True
        self._maybe_fail("strategy_commit")

    def strategy_compensate(self) -> None:
        self.calls.append("strategy_compensate")
        self.strategy_bound = None
        self._strategy_ready = False

    def verify_ready(self, *, strategy_name: str, artifact_id: str) -> None:
        if not self._trader_ready or not self._strategy_ready:
            raise RuntimeError("verify_ready: not both sides ready")
        if self.trader_bound is None or self.strategy_bound is None:
            raise RuntimeError("verify_ready: missing binding")
        if (
            self.trader_bound["strategy_name"] != strategy_name
            or self.strategy_bound["strategy_name"] != strategy_name
            or self.trader_bound["artifact_id"] != artifact_id
            or self.strategy_bound["artifact_id"] != artifact_id
        ):
            raise RuntimeError("verify_ready: strategy/artifact mismatch")
        self._maybe_fail("verify")
