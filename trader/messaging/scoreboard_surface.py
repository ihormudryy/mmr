"""Scoreboard reads over typed RPC (SP1 Plan 5 Task 7). Reads only; the ingestion commands are in
ai_ingest_surface."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, field_validator

_EXPERIMENT_ID = re.compile(r"^exp-[0-9a-f]{20}$")


def _experiment_id_shape(value: Optional[str]) -> Optional[str]:
    if value is not None and not _EXPERIMENT_ID.fullmatch(value):
        raise ValueError("experiment_id must match exp-<20 hex>")
    return value


class GetScoreboardRequest(BaseModel):
    """get_scoreboard and verify_scoreboard: the latest experiment, or the named one."""
    model_config = ConfigDict(extra="forbid", strict=True)

    experiment_id: Optional[str] = None

    @field_validator("experiment_id")
    @classmethod
    def _shape(cls, value: Optional[str]) -> Optional[str]:
        return _experiment_id_shape(value)


class GetExperimentTripsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    experiment_id: str

    @field_validator("experiment_id")
    @classmethod
    def _shape(cls, value: str) -> str:
        return _experiment_id_shape(value)


def register_scoreboard_surface(registry: Any, service: Any) -> None:
    """``service`` is the trader's ``ScoreboardService``; None (no experiment stack) registers nothing."""
    if service is None:
        return

    def get_scoreboard(parsed: GetScoreboardRequest) -> Dict[str, Any]:
        service.refresh(parsed.experiment_id)
        return service.report(parsed.experiment_id)

    def verify_scoreboard(parsed: GetScoreboardRequest) -> Dict[str, Any]:
        return service.verify(parsed.experiment_id)

    def get_experiment_trips(parsed: GetExperimentTripsRequest) -> Dict[str, Any]:
        service.refresh(parsed.experiment_id)
        return service.trips(parsed.experiment_id)

    registry.register("query", "get_scoreboard", GetScoreboardRequest, dict, get_scoreboard, execution="thread")
    registry.register("query", "verify_scoreboard", GetScoreboardRequest, dict, verify_scoreboard,
                      execution="thread")
    registry.register("query", "get_experiment_trips", GetExperimentTripsRequest, dict, get_experiment_trips,
                      execution="thread")
