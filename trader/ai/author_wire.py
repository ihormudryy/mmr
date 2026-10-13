"""Reply shapes of the author's trader calls: submit_authored_strategy and get_authored_strategies (SP3 Plan 3).

The only file to adapt when those replies change. It mirrors trader/automation/authored_strategies.py
(AuthoredRecord.to_json, AuthoredStrategyStore.overview and the store's submit replies) key for key, without
importing the trader store. extra="forbid" and no defaults: a new key, a missing key or a wrong type raises
WireError, never read as "nothing happened".

Not here on purpose: AUTHORED_STORE_UNAVAILABLE is an RPC error (RpcRefused), not a reply, and
AUTHORED_WITHDRAWAL_FAILED belongs to retire_authored_strategy, which the author never calls.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from trader.ai.research_wire import DIGEST, WireError, parse_reply

AUTHORED_KEY = r"^authored:sha256:[0-9a-f]{64}:[A-Za-z_][A-Za-z0-9_]{0,63}$"
AuthoredStateName = Literal["STORED", "EVALUATING", "SHADOW", "DEPLOYED", "REJECTED", "RETIRED", "EXPIRED", "FAILED"]
Number = Union[StrictInt, StrictFloat]


class _AuthorWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TunableRange(_AuthorWire):
    low: Number
    high: Number
    default: Number          # the class constant, stored on the record (Plan 3 ruling 5)


class AuthoredRecord(_AuthorWire):
    """AuthoredRecord.to_json() of the trader store."""
    digest: StrictStr = Field(pattern=DIGEST)
    key: StrictStr = Field(pattern=AUTHORED_KEY)
    class_name: StrictStr
    bar_size: StrictStr
    tunables: dict[StrictStr, TunableRange]
    note: StrictStr
    author_model: StrictStr
    state: AuthoredStateName
    reason: Optional[StrictStr]
    attempts: StrictInt = Field(ge=0)
    evaluation_request_id: Optional[StrictStr]
    sandbox_image_digest: Optional[StrictStr]
    verdict: Optional[StrictStr]
    judgment_id: Optional[StrictStr]
    deployment_version: Optional[StrictStr]
    created_at: StrictStr
    state_changed_at: StrictStr
    history: list[dict[StrictStr, Any]]


class AuthoredStrategiesReply(_AuthorWire):
    enabled: StrictBool
    max_active: StrictInt = Field(ge=0)
    counted: StrictInt = Field(ge=0)
    room: StrictInt = Field(ge=0)
    judged_count: StrictInt = Field(ge=0)
    bar_sizes: list[StrictStr]
    records: list[AuthoredRecord]

    @property
    def cap_has_room(self) -> bool:
        return self.room > 0


class SubmitAuthoredReply(_AuthorWire):
    status: Literal["STORED", "EXISTING", "REFUSED"]
    code: Optional[StrictStr]
    detail: Optional[StrictStr]
    retryable: StrictBool
    record: Optional[AuthoredRecord]


def parse_authored_strategies(reply: Any) -> AuthoredStrategiesReply:
    return parse_reply(AuthoredStrategiesReply, "get_authored_strategies", reply)


def parse_submit_reply(reply: Any) -> SubmitAuthoredReply:
    parsed = parse_reply(SubmitAuthoredReply, "submit_authored_strategy", reply)
    if parsed.status != "REFUSED" and parsed.record is None:
        raise WireError(f"submit_authored_strategy: {parsed.status} without a record")
    return parsed
