"""Strict wire models of the SP2c deployment-version methods (spec 5.1 table).

The strategy service imports this module, never ``production_api``. Every model is
``extra="forbid", strict=True``: a bool is never an int and no unknown key passes.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trader.automation.ai_deployments import MAX_CONIDS, _CLASS, _PATH
from trader.research.strategy_key import AUTHORED_PATH

_STRICT = ConfigDict(extra="forbid", strict=True)

# A catalogue file or an authored file (SP3 Plan 3 ruling 1, decision D4). The trader validates its own reply with
# this model, so a row this pattern refuses would fail the reply for every deployment.
ACTIVE_STRATEGY_PATH = rf"^(?:{_PATH.pattern[1:-1]}|{AUTHORED_PATH.pattern[1:-1]})$"

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class WithdrawAiDeploymentRequest(BaseModel):
    model_config = _STRICT

    version_digest: Digest
    reason: Annotated[str, Field(min_length=1, max_length=200)]


class GetAiDeploymentVersionRequest(BaseModel):
    model_config = _STRICT

    version_digest: Digest


class GetActiveAiDeploymentsRequest(BaseModel):
    model_config = _STRICT


class ActiveAiDeployment(BaseModel):
    model_config = _STRICT

    version_digest: Digest
    base_digest: Digest
    strategy_path: Annotated[str, Field(pattern=ACTIVE_STRATEGY_PATH)]
    strategy_digest: Digest
    class_name: Annotated[str, Field(pattern=_CLASS.pattern)]
    params: dict[str, Any]
    conids: Annotated[list[Annotated[int, Field(gt=0)]], Field(min_length=1, max_length=MAX_CONIDS)]
    bar_size: str
    expiry_session: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class GetActiveAiDeploymentsResponse(BaseModel):
    model_config = _STRICT

    account_mode: Literal["paper"]
    deployments: list[ActiveAiDeployment]
