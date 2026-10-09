"""The canonical evaluation request (SP2c spec 5.1 step 1).

The request id is the digest of the canonical body, so a caller cannot choose it.
The research service and the trader hash the same bytes: the model refuses every
input with two spellings (unsorted or repeated conids, repeated points).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

from trader.objects import BarSize
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.evaluation_spec import LONGEST_BAR_SIZE
from trader.research.strategy_key import is_strategy_key

EVALUATION_REQUEST_DOMAIN = "mmr.research.evaluation-request.v1"
REQUEST_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
TUNABLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_COHORT_POINTS_LIMIT = 10
MAX_CONIDS = 20
MAX_PARAMS = 32
ParamValue = Union[StrictBool, StrictInt, StrictFloat, StrictStr]


def check_params(params: dict) -> dict:
    """One parameter point: upper-case tunable names and finite scalar values only."""
    if len(params) > MAX_PARAMS:
        raise ValueError(f"a parameter point has at most {MAX_PARAMS} tunables")
    for name, value in params.items():
        if not TUNABLE_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an upper-case tunable name")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if isinstance(value, str) and not 1 <= len(value) <= 128:
            raise ValueError(f"{name} must be 1-128 characters")
    return params


def check_cohort(points: list[dict]) -> list[dict]:
    """Every point is a valid parameter set and no point repeats (by its canonical spelling)."""
    for point in points:
        check_params(point)
    if len({canonical_json_bytes(point) for point in points}) != len(points):
        raise ValueError("cohort points must be distinct")
    return points


def is_cohort_point(params: dict, cohort: list[dict]) -> bool:
    return canonical_json_bytes(params) in {canonical_json_bytes(point) for point in cohort}


def check_conids(conids: list[int]) -> list[int]:
    if any(conid <= 0 for conid in conids):
        raise ValueError("conids must be positive")
    if any(later <= earlier for earlier, later in zip(conids, conids[1:])):
        raise ValueError("conids must be strictly increasing (sorted, no repeats)")
    return conids


def check_bar_size(bar_size: str) -> str:
    try:
        parsed = BarSize.parse_str(bar_size)
    except ValueError:
        raise ValueError(f"{bar_size!r} is not a bar size") from None
    if parsed > LONGEST_BAR_SIZE:
        raise ValueError(f"{bar_size!r} is longer than 15 minutes")
    return bar_size


def check_day(value: str, name: str) -> str:
    """One spelling only: YYYY-MM-DD (``fromisoformat`` alone also accepts 20261008 and 2026-W41-4)."""
    if not DAY.fullmatch(value):
        raise ValueError(f"{name} must be YYYY-MM-DD")
    dt.date.fromisoformat(value)
    return value


class EvaluationRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    strategy_key: str
    cohort: list[dict[str, ParamValue]] = Field(min_length=1, max_length=MAX_COHORT_POINTS_LIMIT)
    conids: list[int] = Field(min_length=1, max_length=MAX_CONIDS)
    bar_size: str
    research_day: str

    @field_validator("strategy_key")
    @classmethod
    def _strategy_key(cls, value: str) -> str:
        if not is_strategy_key(value):
            raise ValueError("strategy_key must be strategies/<file>.py:<Class>")
        return value

    @field_validator("cohort")
    @classmethod
    def _cohort(cls, value: list[dict]) -> list[dict]:
        return check_cohort(value)

    @field_validator("conids")
    @classmethod
    def _conids(cls, value: list[int]) -> list[int]:
        return check_conids(value)

    @field_validator("bar_size")
    @classmethod
    def _bar_size(cls, value: str) -> str:
        return check_bar_size(value)

    @field_validator("research_day")
    @classmethod
    def _research_day(cls, value: str) -> str:
        return check_day(value, "research_day")


def canonical_request_json(body: EvaluationRequestBody) -> str:
    return canonical_json_bytes(body.model_dump()).decode("utf-8")


def evaluation_request_id(body: EvaluationRequestBody) -> str:
    return "sha256:" + sha256_digest(EVALUATION_REQUEST_DOMAIN, body.model_dump())
