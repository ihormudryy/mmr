"""record_shadow_result over typed RPC (SP2c Plan 3). research only; the handler re-checks.

A business refusal is a reply body. A tampered judgment or deployment version is an RPC error, never a
reply body and never a missing row.
"""
from __future__ import annotations

from typing import Any

from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.backtest_judgments import JudgmentRefused
from trader.messaging.typed_rpc import RpcCaller, _DispatchProblem
from trader.scoreboard.shadow_ingest import RESEARCH, RecordShadowResultRequest


def register_shadow_surface(registry: Any, ingest: Any) -> None:
    if ingest is None:
        return

    def record_shadow_result(parsed: RecordShadowResultRequest, caller: RpcCaller) -> dict:
        if caller.principal != RESEARCH:
            raise _DispatchProblem("PERMISSION_DENIED",
                                   f"principal {caller.principal!r} may not call 'record_shadow_result'")
        try:
            return ingest.record(parsed, caller)
        except JudgmentRefused as refused:
            raise _DispatchProblem(refused.code, refused.detail) from None
        except DeploymentRefused as refused:
            raise _DispatchProblem(refused.code, refused.message) from None

    registry.register("command", "record_shadow_result", RecordShadowResultRequest, dict, record_shadow_result,
                      execution="thread", with_caller=True)
