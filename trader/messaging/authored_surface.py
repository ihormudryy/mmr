"""SP3 authored-strategy methods on the trader (spec 5, 7.3, 7.9; Plan 3 rulings 13-14).

Direct handlers: no command ledger, no controller epoch. The allow-list is not the authority; each handler
checks its caller again. A business refusal is a reply body; an unusable store directory is an RPC error.
"""
from __future__ import annotations

from typing import Any, Callable

from trader.automation.authored_files import AuthoredStoreError
from trader.automation.authored_isolation import IsolationProofRefused
from trader.automation.authored_wire import (
    GetAuthoredStrategiesRequest, GetIsolationProofRequest, RecordIsolationProofRequest,
    ReportAuthoredEvaluationRequest, RetireAuthoredStrategyRequest, SubmitAuthoredStrategyRequest,
)
from trader.messaging.typed_rpc import RpcCaller, _DispatchProblem

AI_AUTHOR = frozenset({"ai_author"})
HUMAN_READERS = frozenset({"cli", "dashboard"})
RESEARCH = frozenset({"research"})
STRATEGY = frozenset({"strategy"})
OPERATOR = frozenset({"cli"})
PROOF_READERS = frozenset({"cli", "strategy"})
STORE_UNAVAILABLE = "AUTHORED_STORE_UNAVAILABLE"


def _require(caller: RpcCaller, allowed: frozenset[str], method: str) -> None:
    if caller.principal not in allowed:
        raise _DispatchProblem("PERMISSION_DENIED", f"principal {caller.principal!r} may not call {method!r}")


def _loudly(work: Callable[[], dict]) -> dict:
    """An unusable store is an RPC error, never a refusal (the store replies business refusals itself)."""
    try:
        return work()
    except AuthoredStoreError as exc:
        raise _DispatchProblem(STORE_UNAVAILABLE, str(exc)) from None


def register_authored_surface(registry: Any, *, authored: Any, proofs: Any) -> None:
    def submit_authored_strategy(parsed: SubmitAuthoredStrategyRequest, caller: RpcCaller) -> dict:
        _require(caller, AI_AUTHOR, "submit_authored_strategy")
        return _loudly(lambda: authored.submit(parsed))

    def get_authored_strategies(parsed: GetAuthoredStrategiesRequest, caller: RpcCaller) -> dict:
        _require(caller, AI_AUTHOR, "get_authored_strategies")
        return _loudly(authored.overview)

    def list_authored_strategies(parsed: GetAuthoredStrategiesRequest, caller: RpcCaller) -> dict:
        _require(caller, HUMAN_READERS, "list_authored_strategies")
        return _loudly(authored.overview)

    def report_authored_evaluation(parsed: ReportAuthoredEvaluationRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "report_authored_evaluation")
        return _loudly(lambda: authored.report(parsed))

    def retire_authored_strategy(parsed: RetireAuthoredStrategyRequest, caller: RpcCaller) -> dict:
        _require(caller, STRATEGY, "retire_authored_strategy")
        return _loudly(lambda: authored.retire(parsed))

    def record_isolation_proof(parsed: RecordIsolationProofRequest, caller: RpcCaller) -> dict:
        _require(caller, OPERATOR, "record_isolation_proof")
        try:
            return _loudly(lambda: proofs.record(parsed, principal=caller.principal))
        except IsolationProofRefused as refused:
            raise _DispatchProblem(refused.code, refused.detail) from None

    def get_isolation_proof(parsed: GetIsolationProofRequest, caller: RpcCaller) -> dict:
        _require(caller, PROOF_READERS, "get_isolation_proof")
        proof = proofs.latest(parsed.image_digest)
        return {"found": proof is not None, "proof": None if proof is None else proof.to_json()}

    methods = (
        ("command", "submit_authored_strategy", SubmitAuthoredStrategyRequest, submit_authored_strategy),
        ("query", "get_authored_strategies", GetAuthoredStrategiesRequest, get_authored_strategies),
        ("query", "list_authored_strategies", GetAuthoredStrategiesRequest, list_authored_strategies),
        ("command", "report_authored_evaluation", ReportAuthoredEvaluationRequest, report_authored_evaluation),
        ("command", "retire_authored_strategy", RetireAuthoredStrategyRequest, retire_authored_strategy),
        ("command", "record_isolation_proof", RecordIsolationProofRequest, record_isolation_proof),
        ("query", "get_isolation_proof", GetIsolationProofRequest, get_isolation_proof),
    )
    for role, method, model, handler in methods:
        registry.register(role, method, model, dict, handler, execution="thread", with_caller=True)
