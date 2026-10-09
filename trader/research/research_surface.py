"""The research server's typed RPC methods (SP2c spec 5.1).

The registry's allow-list is the first gate. The services behind it check the caller again, so a handler
never relies on the transport alone. A refusal is a reply body, never an RPC error.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from trader.automation.backtest_judge_wire import JUDGMENT_ID
from trader.messaging.principals import RESEARCH_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry

Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]
INITIAL_FIELDS = ("strategy_key", "cohort", "conids", "bar_size")


class SubmitEvaluationRequest(BaseModel):
    """An INITIAL candidate, or a RENEWAL of one deployment version (SP2c Plan 5)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["INITIAL", "RENEWAL"]
    strategy_key: Optional[str] = Field(default=None, max_length=200)
    cohort: Optional[list[dict[str, Scalar]]] = Field(default=None, min_length=1, max_length=10)
    conids: Optional[list[StrictInt]] = Field(default=None, min_length=1, max_length=50)
    bar_size: Optional[str] = Field(default=None, max_length=16)
    prior_version_digest: Optional[str] = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _one_shape(self) -> "SubmitEvaluationRequest":
        initial = [getattr(self, name) is not None for name in INITIAL_FIELDS]
        if self.kind == "INITIAL" and (not all(initial) or self.prior_version_digest is not None):
            raise ValueError("an INITIAL request names strategy_key, cohort, conids and bar_size only")
        if self.kind == "RENEWAL" and (any(initial) or self.prior_version_digest is None):
            raise ValueError("a RENEWAL request names prior_version_digest only")
        return self

    def to_service(self) -> dict:
        """The service drops ``kind`` itself; it is the one place that does."""
        return self.model_dump(exclude_none=True)


class GetEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AttestFromJudgmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    judgment_id: str = Field(pattern=JUDGMENT_ID.pattern)


def build_research_registry(*, evaluations: Any, attest: Any) -> TypedRpcRegistry:
    """One registry for both sockets: each method is registered on the one role its ACL row names."""
    registry = TypedRpcRegistry(acl=RESEARCH_ACL)
    registry.register("command", "submit_evaluation", SubmitEvaluationRequest, dict,
                      lambda request, caller: evaluations.submit(request.to_service(), caller),
                      execution="thread", with_caller=True)
    registry.register("query", "get_evaluation", GetEvaluationRequest, dict,
                      lambda request, caller: evaluations.get(request.request_id, caller),
                      execution="thread", with_caller=True)
    registry.register("command", "attest_from_judgment", AttestFromJudgmentRequest, dict,
                      lambda request, caller: attest.attest(request.model_dump(), caller),
                      execution="thread", with_caller=True)
    return registry
