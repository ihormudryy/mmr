"""Hot-arm ports for Phase 2 paper automation activation.

The activation service owns the phase machine; these ports commit/compensate
trader registry state and strategy IntentEmitter state without process restart.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Protocol


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


class ProductionPaperHotArmPorts:
    """Production hot-arm: late-register trader execute path + strategy arm RPC.

    ``attach_registry`` must be called after ``build_production_registry`` so
    late-register can mutate the live allowlist. Strategy clients are created
    lazily against ``strategy_typed_*`` config on the trader.
    """

    def __init__(
        self,
        *,
        trader: Any,
        stack: Any,
        account_id: str,
        account_mode: str,
        now: Callable[[], dt.datetime],
        build_intent_service: Callable[..., Any],
    ) -> None:
        self._trader = trader
        self._stack = stack
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._build_intent_service = build_intent_service
        self._registry: Any = None
        self._late_registered = False
        self._binding: dict[str, str] | None = None
        self._command_client: Any = None
        self._query_client: Any = None

    def attach_registry(self, registry: Any) -> None:
        self._registry = registry

    def _ensure_strategy_clients(self) -> tuple[Any, Any]:
        if self._command_client is not None and self._query_client is not None:
            return self._command_client, self._query_client
        from trader.messaging.typed_rpc import (
            HmacServiceAuthenticator,
            TypedRpcClient,
            load_service_hmac_key,
        )

        key_file = getattr(self._trader, "service_hmac_key_file", "") or ""
        auth = HmacServiceAuthenticator(load_service_hmac_key(key_file))
        address = (
            getattr(self._trader, "strategy_typed_address", None)
            or "tcp://127.0.0.1"
        )
        if not address:
            address = "tcp://127.0.0.1"
        cmd_port = int(getattr(self._trader, "strategy_typed_command_port", 42104))
        qry_port = int(getattr(self._trader, "strategy_typed_query_port", 42105))
        self._command_client = TypedRpcClient(
            "command", auth, address=address, port=cmd_port, timeout=30.0,
        )
        self._query_client = TypedRpcClient(
            "query", auth, address=address, port=qry_port, timeout=30.0,
        )
        self._command_client.connect()
        self._query_client.connect()
        return self._command_client, self._query_client

    def trader_commit(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        from trader.messaging.production_api import (
            ExecuteAutomatedIntentRequest,
            _execute_automated_intent_rpc_handler,
        )

        if self._registry is None:
            raise RuntimeError("typed registry not attached for paper hot-arm")

        # Temporarily stamp trader attrs so the shared builder can verify paths.
        prev = {
            "automation_enabled": getattr(self._trader, "automation_enabled", False),
            "automation_live_enabled": getattr(
                self._trader, "automation_live_enabled", False
            ),
            "automation_artifact_bundle_path": getattr(
                self._trader, "automation_artifact_bundle_path", ""
            ),
            "automation_public_key_ring_path": getattr(
                self._trader, "automation_public_key_ring_path", ""
            ),
            "automation_expected_artifact_id": getattr(
                self._trader, "automation_expected_artifact_id", ""
            ),
        }
        self._trader.automation_enabled = True
        self._trader.automation_live_enabled = False
        self._trader.automation_artifact_bundle_path = artifact_bundle_path
        self._trader.automation_public_key_ring_path = public_key_ring_path
        self._trader.automation_expected_artifact_id = artifact_id
        try:
            service = self._build_intent_service(self._trader)
        finally:
            for key, value in prev.items():
                setattr(self._trader, key, value)

        if service is None:
            raise RuntimeError("failed to build AutomatedIntentCommandService")

        coordinator = self._stack.coordinator
        coordinator.register_action(
            "execute_automated_intent",
            service.execute,
            requires_preflight=False,
            saga=True,
        )
        if not self._registry.contains("command", "execute_automated_intent"):
            self._registry.register(
                "command",
                "execute_automated_intent",
                ExecuteAutomatedIntentRequest,
                dict,
                _execute_automated_intent_rpc_handler(coordinator, self._account_id),
            )
            self._late_registered = True
        self._stack.automated_intent_service = service
        self._binding = {
            "strategy_name": strategy_name,
            "artifact_id": artifact_id,
            "artifact_bundle_path": artifact_bundle_path,
            "public_key_ring_path": public_key_ring_path,
        }
        # Keep live attrs aligned for any downstream readers.
        self._trader.automation_enabled = True
        self._trader.automation_live_enabled = False
        self._trader.automation_artifact_bundle_path = artifact_bundle_path
        self._trader.automation_public_key_ring_path = public_key_ring_path
        self._trader.automation_expected_artifact_id = artifact_id

    def trader_compensate(self) -> None:
        from trader.trading.command_coordinator import (
            CommandRequest,
            CommandValidationError,
        )

        coordinator = self._stack.coordinator
        registry = self._registry

        def _refuse(_command: CommandRequest) -> dict:
            raise CommandValidationError(
                "AUTOMATION_DISARMED",
                "paper automation is not armed",
            )

        if self._late_registered and registry is not None:
            registry.unregister("command", "execute_automated_intent")
            coordinator.unregister_action("execute_automated_intent")
            self._late_registered = False
        elif "execute_automated_intent" in getattr(coordinator, "_actions", {}):
            coordinator.register_action(
                "execute_automated_intent",
                _refuse,
                requires_preflight=False,
                saga=True,
            )

        self._stack.automated_intent_service = None
        self._binding = None
        self._trader.automation_enabled = False
        self._trader.automation_expected_artifact_id = ""
        self._trader.automation_strategy_name = ""

    def strategy_commit(
        self,
        *,
        strategy_name: str,
        artifact_id: str,
        artifact_bundle_path: str,
        public_key_ring_path: str,
    ) -> None:
        command_client, _query = self._ensure_strategy_clients()
        result = command_client.call(
            "arm_paper_automation",
            {
                "strategy_name": strategy_name,
                "artifact_bundle_path": artifact_bundle_path,
                "public_key_ring_path": public_key_ring_path,
                "expected_artifact_id": artifact_id,
            },
            dict,
        )
        if not result.get("armed"):
            raise RuntimeError(f"strategy arm refused: {result!r}")

    def strategy_compensate(self) -> None:
        try:
            command_client, _query = self._ensure_strategy_clients()
            command_client.call("disarm_paper_automation", {}, dict)
        except Exception:
            # Best-effort during compensate / deactivate.
            raise

    def verify_ready(self, *, strategy_name: str, artifact_id: str) -> None:
        if self._registry is None or not self._registry.contains(
            "command", "execute_automated_intent"
        ):
            raise RuntimeError("trader execute_automated_intent is not registered")
        if self._binding is None:
            raise RuntimeError("trader hot-arm binding missing")
        if (
            self._binding["strategy_name"] != strategy_name
            or self._binding["artifact_id"] != artifact_id
        ):
            raise RuntimeError("trader binding mismatch")
        _cmd, query_client = self._ensure_strategy_clients()
        arm = query_client.call("get_paper_automation_arm", {}, dict)
        if not arm.get("armed"):
            raise RuntimeError("strategy IntentEmitter is not armed")
        if (
            arm.get("strategy_name") != strategy_name
            or arm.get("artifact_id") != artifact_id
        ):
            raise RuntimeError("strategy arm binding mismatch")
