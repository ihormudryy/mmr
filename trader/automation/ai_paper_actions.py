"""Coordinator actions and reads of the ai_paper family besides the decision (Plan 3 Task 9).

``publish_ai_risk_policy`` (ai_supervisor, cli), ``register_ai_deployment``
(ai_research) and ``register_discretionary_deployment`` (cli, paper only; SP2
Plan 3), plus the two reads. Each action checks its principal itself,
so a caller that bypasses the RPC allow-list is still refused.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Optional

from trader.automation.ai_deployments import DISCRETIONARY_KIND, AiDeployment, DeploymentRefused
from trader.automation.ai_risk_policy import PolicyRefused
from trader.automation.command_steps import CommandSteps
from trader.automation.discretionary_deployment import DiscretionaryDeployment
from trader.automation.risk_limits import RiskLimits, RiskLimitsError
from trader.domain.commands import CommandReceipt
from trader.trading.command_coordinator import CommandRequest, CommandValidationError

AI_SUPERVISOR = "ai_supervisor"
AI_RESEARCH = "ai_research"
OPERATOR = "cli"
# SP2 spec 6.7: the operator publishes the initial policy; ai_supervisor keeps the right for SP2d.
POLICY_PUBLISHERS = frozenset({AI_SUPERVISOR, "cli"})
PUBLISH_ACTION = "publish_ai_risk_policy"
REGISTER_ACTION = "register_ai_deployment"
REGISTER_DISCRETIONARY_ACTION = "register_discretionary_deployment"
DEPLOYMENT_COMMAND_PREFIX = "aidep-"


def deployment_command_id(digest: str) -> str:
    """A re-registration of the same content replays one command instead of conflicting."""
    return DEPLOYMENT_COMMAND_PREFIX + digest.split(":", 1)[1][:48]


def _limits_view(limits: Optional[RiskLimits]) -> Optional[dict]:
    return None if limits is None else limits.to_json()


class AiPaperActions:
    def __init__(self, *, policy: Any, deployments: Any, broker: Any, config: Any, account_id: str,
                 account_mode: str, ledger: Any, journal: Any, controls: Any, now: Callable[[], dt.datetime]):
        self._policy = policy
        self._deployments = deployments
        self._broker = broker
        self._config = config
        self._account_id = account_id
        self._account_mode = account_mode
        self._steps = CommandSteps(ledger=ledger, journal=journal, controls=controls,
                                   account_id=account_id, now=now)

    # -- publish_ai_risk_policy (its own receipts, so a broker outage is retryable) --

    def publish(self, cmd: CommandRequest) -> CommandReceipt:
        try:
            outcome = self._publish(cmd)
        except _Refused as refused:
            self._steps.transition(cmd, "RECEIVED", "REJECTED", error_code=refused.code,
                                   outcome={"message": refused.message})
            return CommandSteps.receipt(cmd.command_id, "REJECTED", refused.code, refused.retryable,
                                        outcome={"message": refused.message})
        self._steps.transition(cmd, "RECEIVED", "RESOLVED", outcome=outcome)
        return CommandSteps.receipt(cmd.command_id, "RESOLVED", None, False, outcome=outcome)

    def _publish(self, cmd: CommandRequest) -> dict:
        if cmd.principal not in POLICY_PUBLISHERS:
            raise _Refused("PRINCIPAL_FORBIDDEN", "only ai_supervisor or the cli operator publishes risk policies")
        if cmd.account_id != self._account_id:
            raise _Refused("ACCOUNT_MISMATCH", "command account is not the pinned account")
        try:
            limits = RiskLimits.from_json(cmd.body.get("limits"))
        except RiskLimitsError as ex:
            raise _Refused("POLICY_INVALID", str(ex)) from None
        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception:
            raise _Refused("BROKER_SNAPSHOT_UNAVAILABLE", "broker snapshot unavailable", retryable=True) from None
        try:
            result = self._policy.publish(limits, reason=cmd.body.get("reason"), principal=cmd.principal,
                                          command_id=cmd.command_id, broker=snapshot)
        except PolicyRefused as ex:
            raise _Refused(ex.code, ex.message) from None
        return {"revision": result.revision, "applied_now": list(result.applied_now),
                "queued": list(result.queued)}

    # -- register_ai_deployment (single step) ----------------------------------

    def register(self, cmd: CommandRequest) -> dict:
        if cmd.principal != AI_RESEARCH:
            raise CommandValidationError("PRINCIPAL_FORBIDDEN", "only ai_research registers deployments")
        try:
            deployment = AiDeployment.from_json(cmd.body)
        except DeploymentRefused as ex:
            raise CommandValidationError(ex.code, ex.message) from None
        if deployment.style not in self._config.styles:
            raise CommandValidationError("STYLE_NOT_ENABLED", f"style {deployment.style!r} is not enabled")
        digest, created = self._deployments.register(deployment, principal=cmd.principal,
                                                     command_id=cmd.command_id)
        return {"digest": digest, "created": created,
                "strategy_digest_provenance": self._deployments.provenance(digest)}

    # -- register_discretionary_deployment (single step; SP2 spec 6.6) ----------

    def register_discretionary(self, cmd: CommandRequest) -> dict:
        if cmd.principal != OPERATOR:
            raise CommandValidationError("PRINCIPAL_FORBIDDEN",
                                         "only the cli operator registers a discretionary deployment")
        if self._account_mode != "paper" or not str(self._account_id).startswith("DU"):
            raise CommandValidationError("ACCOUNT_NOT_PAPER", "discretionary deployments are paper only")
        try:
            deployment = DiscretionaryDeployment.from_json(cmd.body)
        except DeploymentRefused as ex:
            raise CommandValidationError(ex.code, ex.message) from None
        if deployment.style not in self._config.styles:
            raise CommandValidationError("STYLE_NOT_ENABLED", f"style {deployment.style!r} is not enabled")
        digest, created = self._deployments.register_discretionary(deployment, principal=cmd.principal,
                                                                   command_id=cmd.command_id)
        return {"digest": digest, "created": created, "kind": DISCRETIONARY_KIND}

    # -- reads -------------------------------------------------------------------

    def policy_view(self) -> dict:
        view = self._policy.current()
        latest = self._policy.latest_published()
        return {
            "latest_published_revision": None if latest is None else latest[0],
            "latest_published": None if latest is None else latest[1].to_json(),
            "owner_ceiling": self._policy.ceiling.to_json(),
            "ceiling": (self._policy.ceiling if view is None else view.ceiling).to_json(),
            "session_date": None if view is None else view.session_date.isoformat(),
            "anchor": None if view is None else view.anchor,
            "effective": None if view is None else _limits_view(view.effective),
            "effective_revision": None if view is None else view.effective_revision,
            "published_revision": None if view is None else view.published_revision,
            "queued": [] if view is None else list(view.queued),
            "latch_code": None if view is None else view.latch_code,
            "daily_loss_budget": None if view is None else view.daily_loss_budget,
        }

    def deployment_view(self, digest: str) -> dict:
        try:
            deployment = self._deployments.get_sealed_any(digest)
        except DeploymentRefused as ex:
            return {"digest": digest, "kind": None, "deployment": None, "strategy_digest_provenance": None,
                    "error_code": ex.code}
        return {"digest": digest, "kind": self._deployments.kind_of(digest), "deployment": deployment.to_json(),
                "strategy_digest_provenance": self._deployments.provenance(digest), "error_code": None}


class _Refused(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
