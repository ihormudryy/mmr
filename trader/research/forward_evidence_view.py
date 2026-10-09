"""The forward evidence of one deployment version (SP2c spec 5.2 item 7, 6.2 "Renewal after expiry").

The trader builds it for get_deployment_forward_evidence; the research service parses it with this same
strict model and signs a renewal case from it. Every value is code-computed."""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

DIGEST = r"^sha256:[0-9a-f]{64}$"
DAY = r"^\d{4}-\d{2}-\d{2}$"
Money = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Scalar = Union[StrictBool, StrictInt, Money, StrictStr]
VERSION_STATUSES = ("ACTIVE", "NOT_STARTED", "EXPIRED", "WITHDRAWN", "SUPERSEDED", "JUDGMENT_ENDED", "OVER_CAP")


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class VersionBinding(_View):
    strategy_key: StrictStr
    strategy_path: StrictStr
    class_name: StrictStr
    strategy_file_hash: StrictStr = Field(pattern=DIGEST)
    params: dict[StrictStr, Scalar]
    conids: list[StrictInt] = Field(min_length=1, max_length=20)
    bar_size: StrictStr
    order_notional: Money = Field(gt=0)
    bundle_digest: StrictStr = Field(pattern=DIGEST)


class LineFacts(_View):
    """The INITIAL judgment of the line: the evaluation the bundle attests."""
    initial_judgment_id: StrictStr
    family_id: Optional[StrictStr]
    selected_trial_id: Optional[StrictStr]
    artifact_id: StrictStr
    eligibility_decision_digest: Optional[StrictStr]


class Renewability(_View):
    ok: StrictBool
    code: Optional[StrictStr]
    detail: StrictStr


class ForwardSession(_View):
    session_date: StrictStr = Field(pattern=DAY)
    state: Literal["COMPLETE", "INCOMPLETE", "MISSING", "NOT_REPLAYED"]
    reason: Optional[StrictStr]
    pnl_usd: Optional[Money]
    fees_usd: Optional[Money]
    trades: Optional[StrictInt]
    end_equity_usd: Optional[Money]


class PaperTrip(_View):
    round_trip_id: StrictStr
    conid: StrictInt
    status: Literal["OPEN", "CLOSED"]
    opened_session: StrictStr = Field(pattern=DAY)
    closed_session: Optional[StrictStr]
    net_pnl_usd: Optional[Money]
    fees_complete: StrictBool


class ForwardEvidenceView(_View):
    version_digest: StrictStr = Field(pattern=DIGEST)
    base_digest: StrictStr = Field(pattern=DIGEST)
    judgment_id: StrictStr
    kind: Literal["INITIAL", "RENEWAL"]
    prior_version_digest: Optional[StrictStr]
    status: Literal[VERSION_STATUSES]
    first_session: StrictStr = Field(pattern=DAY)
    expiry_session: StrictStr = Field(pattern=DAY)
    binding: VersionBinding
    line: LineFacts
    renewable: Renewability
    sessions: list[ForwardSession]
    trips: list[PaperTrip]
    as_of: StrictStr

    def to_wire(self) -> dict:
        return self.model_dump(mode="json")
