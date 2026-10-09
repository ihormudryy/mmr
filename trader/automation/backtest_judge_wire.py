"""Typed RPC bodies of the SP2c backtest-judge methods (spec 5.1 table; 5.2 items 1-3, 7)."""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from trader.research.evaluation_case import FULL_MENU, NO_DEPLOY_MENU
from trader.research.evaluation_request import REQUEST_ID, EvaluationRequestBody
from trader.research.review import MAX_NARRATIVE_CHARS

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
JUDGMENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
JEV_MODEL = re.compile(r"^[A-Za-z0-9_./:@+-]{1,128}$")
ATTEMPT_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# The eight narrative fields of trader.research.review.OperatorReview (design §8.5).
NARRATIVE_FIELDS = ("economic_rationale", "edge_survives_costs", "known_failure_regimes",
                    "data_and_survivorship_limits", "parameter_sensitivity", "operational_dependencies",
                    "capacity_and_decay", "episode_dominance")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _matching(pattern: re.Pattern, name: str, value: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} must match {pattern.pattern}")
    return value


def parse_aware(text: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not an ISO-8601 time") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("times must carry an offset")
    try:
        return moment.astimezone(dt.timezone.utc)
    except OverflowError:
        raise ValueError(f"{text!r} is out of range in UTC") from None


class DeployNarrative(_Strict):
    economic_rationale: str
    edge_survives_costs: str
    known_failure_regimes: str
    data_and_survivorship_limits: str
    parameter_sensitivity: str
    operational_dependencies: str
    capacity_and_decay: str
    episode_dominance: str

    @model_validator(mode="after")
    def _filled(self) -> "DeployNarrative":
        for name in NARRATIVE_FIELDS:
            text = getattr(self, name)
            if not text.strip() or len(text) > MAX_NARRATIVE_CHARS:
                raise ValueError(f"{name} must be 1-{MAX_NARRATIVE_CHARS} characters and not blank")
        return self


class RecordBacktestJudgmentRequest(_Strict):
    judgment_id: str
    case_digest: str
    kind: Literal["INITIAL", "RENEWAL"]
    renewal_of_version: Optional[str]
    verdict: Literal["DEPLOY", "SHADOW", "REJECT", "NO_VERDICT"]
    menu: list[Literal["DEPLOY", "SHADOW", "REJECT"]]
    jev_model: str
    jev_attempt_ref: Optional[str]
    decided_at: str
    narrative: Optional[DeployNarrative]

    @model_validator(mode="after")
    def _shape(self) -> "RecordBacktestJudgmentRequest":
        _matching(JUDGMENT_ID, "judgment_id", self.judgment_id)
        _matching(DIGEST, "case_digest", self.case_digest)
        _matching(JEV_MODEL, "jev_model", self.jev_model)
        if self.jev_attempt_ref is not None:
            _matching(ATTEMPT_REF, "jev_attempt_ref", self.jev_attempt_ref)
        elif self.verdict != "NO_VERDICT":
            raise ValueError("only a NO_VERDICT may lack a model attempt (no call was sent)")
        parse_aware(self.decided_at)
        if tuple(self.menu) not in (FULL_MENU, NO_DEPLOY_MENU):
            raise ValueError(f"menu must be {list(FULL_MENU)} or {list(NO_DEPLOY_MENU)}")
        if self.verdict != "NO_VERDICT" and self.verdict not in self.menu:
            raise ValueError("the verdict must be on the menu offered")
        if (self.verdict == "DEPLOY") != (self.narrative is not None):
            raise ValueError("a DEPLOY carries every narrative field; no other verdict carries one")
        if (self.kind == "RENEWAL") != (self.renewal_of_version is not None):
            raise ValueError("renewal_of_version is set exactly for a RENEWAL")
        if self.renewal_of_version is not None:
            _matching(DIGEST, "renewal_of_version", self.renewal_of_version)
        return self

    def decided_at_utc(self) -> dt.datetime:
        return parse_aware(self.decided_at)

    def digest_body(self) -> dict:
        """The body the trader hashes and stores: times in UTC, so a respelled retry is the same judgment."""
        body = self.model_dump()
        body["decided_at"] = self.decided_at_utc().isoformat()
        return body


class ClaimEvaluationRequest(_Strict):
    request_id: str
    body: EvaluationRequestBody

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return _matching(REQUEST_ID, "request_id", value)


class GetEvaluationClaimRequest(_Strict):
    request_id: str

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return _matching(REQUEST_ID, "request_id", value)


class UpdateEvaluationClaimRequest(_Strict):
    request_id: str
    state: Literal["RUNNING", "DONE", "FAILED"]

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return _matching(REQUEST_ID, "request_id", value)


class GetBacktestJudgmentRequest(_Strict):
    """Exactly one key is set: the judgment id, or the case digest (one judgment per case)."""
    judgment_id: Optional[str]
    case_digest: Optional[str]

    @model_validator(mode="after")
    def _one_key(self) -> "GetBacktestJudgmentRequest":
        if (self.judgment_id is None) == (self.case_digest is None):
            raise ValueError("set exactly one of judgment_id or case_digest")
        if self.judgment_id is not None:
            _matching(JUDGMENT_ID, "judgment_id", self.judgment_id)
        else:
            _matching(DIGEST, "case_digest", self.case_digest)
        return self


class GetDeploymentForwardEvidenceRequest(_Strict):
    deployment_version: str

    @field_validator("deployment_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _matching(DIGEST, "deployment_version", value)
