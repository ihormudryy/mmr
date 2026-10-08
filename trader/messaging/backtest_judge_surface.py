"""SP2c backtest-judge methods on the trader (spec 5.1 table).

Direct handlers: no command ledger, no controller epoch. The allow-list is not the
authority; each handler checks its caller again. A business refusal is a reply
body; a tampered judgment is an RPC error.
"""
from __future__ import annotations

from typing import Any

from trader.automation.backtest_judge_wire import (
    ClaimEvaluationRequest, GetBacktestJudgmentRequest, GetDeploymentForwardEvidenceRequest,
    GetEvaluationClaimRequest, RecordBacktestJudgmentRequest, UpdateEvaluationClaimRequest,
)
from trader.automation.backtest_judgments import JudgmentRefused
from trader.automation.evaluation_claims import ClaimRefused
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.messaging.typed_rpc import RpcCaller, _DispatchProblem

RESEARCH = frozenset({"research"})
AI_RESEARCH = frozenset({"ai_research"})
JUDGMENT_READERS = frozenset({"research", "ai_research", "cli", "dashboard"})


def _require(caller: RpcCaller, allowed: frozenset[str], method: str) -> None:
    if caller.principal not in allowed:
        raise _DispatchProblem("PERMISSION_DENIED", f"principal {caller.principal!r} may not call {method!r}")


def register_backtest_judge_surface(registry: Any, *, claims: Any, judgments: Any, forward_evidence: Any) -> None:
    def claim_evaluation(parsed: ClaimEvaluationRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "claim_evaluation")
        try:
            return claims.claim(parsed.request_id, parsed.body, principal=caller.principal).reply()
        except ClaimRefused as refused:
            return refused.reply()

    def get_evaluation_claim(parsed: GetEvaluationClaimRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "get_evaluation_claim")
        claim = claims.get(parsed.request_id)
        return {"found": claim is not None, "claim": None if claim is None else claim.to_json()}

    def update_evaluation_claim(parsed: UpdateEvaluationClaimRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "update_evaluation_claim")
        try:
            return claims.update(parsed.request_id, parsed.state).reply()
        except ClaimRefused as refused:
            return refused.reply()

    def record_backtest_judgment(parsed: RecordBacktestJudgmentRequest, caller: RpcCaller) -> dict:
        _require(caller, AI_RESEARCH, "record_backtest_judgment")
        try:
            return judgments.record(parsed)
        except JudgmentRefused as refused:          # a tampered row fails loudly, never a reply body
            raise _DispatchProblem(refused.code, refused.detail) from None

    def get_backtest_judgment(parsed: GetBacktestJudgmentRequest, caller: RpcCaller) -> dict:
        _require(caller, JUDGMENT_READERS, "get_backtest_judgment")
        try:
            judgment = (judgments.get(parsed.judgment_id) if parsed.judgment_id is not None
                        else judgments.get_by_case(parsed.case_digest))
        except JudgmentRefused as refused:          # a tampered row fails loudly, never reads as missing
            raise _DispatchProblem(refused.code, refused.detail) from None
        return {"found": judgment is not None, "judgment": None if judgment is None else judgment.to_json()}

    def get_deployment_forward_evidence(parsed: GetDeploymentForwardEvidenceRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "get_deployment_forward_evidence")
        try:
            evidence = forward_evidence.read(parsed.deployment_version)
        except ForwardEvidenceRefused as refused:
            return {"status": "REFUSED", "code": refused.code, "detail": refused.detail, "evidence": None}
        return {"status": "FOUND", "code": None, "detail": None, "evidence": evidence}

    registry.register("command", "claim_evaluation", ClaimEvaluationRequest, dict, claim_evaluation,
                      execution="thread", with_caller=True)
    registry.register("query", "get_evaluation_claim", GetEvaluationClaimRequest, dict, get_evaluation_claim,
                      execution="thread", with_caller=True)
    registry.register("command", "update_evaluation_claim", UpdateEvaluationClaimRequest, dict,
                      update_evaluation_claim, execution="thread", with_caller=True)
    registry.register("command", "record_backtest_judgment", RecordBacktestJudgmentRequest, dict,
                      record_backtest_judgment, execution="thread", with_caller=True)
    registry.register("query", "get_backtest_judgment", GetBacktestJudgmentRequest, dict, get_backtest_judgment,
                      execution="thread", with_caller=True)
    registry.register("query", "get_deployment_forward_evidence", GetDeploymentForwardEvidenceRequest, dict,
                      get_deployment_forward_evidence, execution="thread", with_caller=True)
