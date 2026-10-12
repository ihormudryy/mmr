"""Typed RPC bodies of the SP3 authored-strategy methods (spec 5, 7.3, 7.9; Plan 3 Cross-plan additions).

Strict: unknown keys, missing keys and coercions are refused (VALIDATION_ERROR). Business rules that the
author must learn from (tunable names and ranges, the bar size, the static check) are checked by the
store and answered as refusals, not here.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal, Optional, Union

from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, ValidationInfo, field_validator,
    model_validator,
)

from trader.automation.backtest_judge_wire import DIGEST, JEV_MODEL, parse_aware
from trader.research.request_rules import MAX_PARAMS

MAX_SOURCE_CHARS = 20_480
MAX_NOTE_CHARS = 600
MAX_DETAIL_CHARS = 500
MAX_CHECKS = 64
CHECK_NAME = re.compile(r"^[a-z0-9_.:-]{1,64}$")
# Plan 2 ruling 1: the sandbox image's identity is its build id (<git sha>-<UTC time>), baked into the image.
IMAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
Number = Union[StrictInt, StrictFloat]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _digest(name: str, value: str) -> str:
    if not DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be sha256:<64 hex>")
    return value


def _image_id(name: str, value: str) -> str:
    if value == "unset" or not IMAGE_ID.fullmatch(value):
        raise ValueError(f"{name} must be a build id matching {IMAGE_ID.pattern}, never 'unset'")
    return value


class TunableRange(_Strict):
    low: Number
    high: Number
    default: Number          # equals the class constant (ruling 5, decision D5)


class SubmitAuthoredStrategyRequest(_Strict):
    source: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    tunables: dict[str, TunableRange] = Field(max_length=MAX_PARAMS)
    bar_size: str = Field(min_length=1, max_length=16)
    note: str = Field(max_length=MAX_NOTE_CHARS)
    author_model: str

    @field_validator("source", "note")
    @classmethod
    def _utf8(cls, value: str, info: ValidationInfo) -> str:
        """JSON can carry a lone surrogate; the digest and the stored files need UTF-8 bytes."""
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"{info.field_name} must be valid UTF-8 text") from None
        return value

    @field_validator("author_model")
    @classmethod
    def _author_model(cls, value: str) -> str:
        if not JEV_MODEL.fullmatch(value):
            raise ValueError(f"author_model must match {JEV_MODEL.pattern}")
        return value


class GetAuthoredStrategiesRequest(_Strict):
    """No fields: the reply is the whole list, which the cap keeps small."""


class ReportAuthoredEvaluationRequest(_Strict):
    digest: str
    request_id: str
    event: Literal["STARTED", "SANDBOX_UNAVAILABLE", "SANDBOX_ERROR"]
    sandbox_image_digest: Optional[str]
    detail: str = Field(max_length=MAX_DETAIL_CHARS)

    @field_validator("digest", "request_id")
    @classmethod
    def _digests(cls, value: str, info: ValidationInfo) -> str:
        return _digest(info.field_name, value)

    @model_validator(mode="after")
    def _image(self) -> "ReportAuthoredEvaluationRequest":
        if (self.event == "STARTED") != (self.sandbox_image_digest is not None):
            raise ValueError("sandbox_image_digest is required for STARTED and null for every other event")
        if self.sandbox_image_digest is not None:
            _image_id("sandbox_image_digest", self.sandbox_image_digest)
        return self


class RetireAuthoredStrategyRequest(_Strict):
    digest: str
    code: Literal["SANDBOX_ERROR", "SANDBOX_DIGEST_MISMATCH"]
    detail: str = Field(max_length=MAX_DETAIL_CHARS)

    @field_validator("digest")
    @classmethod
    def _digest_field(cls, value: str) -> str:
        return _digest("digest", value)


class IsolationCheck(_Strict):
    name: str
    passed: StrictBool
    detail: str = Field(max_length=MAX_DETAIL_CHARS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not CHECK_NAME.fullmatch(value):
            raise ValueError(f"a check name must match {CHECK_NAME.pattern}")
        return value


class RecordIsolationProofRequest(_Strict):
    image_digest: str
    passed: StrictBool
    checks: list[IsolationCheck] = Field(min_length=1, max_length=MAX_CHECKS)
    ran_at: str

    @field_validator("image_digest")
    @classmethod
    def _image(cls, value: str) -> str:
        return _image_id("image_digest", value)

    @field_validator("ran_at")
    @classmethod
    def _ran_at(cls, value: str) -> str:
        parse_aware(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RecordIsolationProofRequest":
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("check names must be distinct")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("passed must equal every check passing (Plan 3 ruling 21)")
        return self

    def ran_at_utc(self) -> dt.datetime:
        return parse_aware(self.ran_at)


class GetIsolationProofRequest(_Strict):
    image_digest: Optional[str]

    @field_validator("image_digest")
    @classmethod
    def _image(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _image_id("image_digest", value)
