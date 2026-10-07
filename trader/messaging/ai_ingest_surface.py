"""Cost and simulation ingestion over typed RPC (SP2 Plan 2). ai_supervisor only; facts, not edits."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest


class GetAiModelBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def register_ai_ingest_surface(registry: Any, ingest: Any, *, model_budget: Optional[float] = None) -> None:
    """``ingest`` is the trader's ``AiIngest`` (None: no scoreboard, no command). ``model_budget`` is
    ``ai_paper.model_budget_usd_per_day`` from trader.yaml, read once at start (Ruling 20); None: no query."""
    if model_budget is not None:
        reply = {"model_budget_usd_per_day": float(model_budget), "source": "trader.yaml"}
        registry.register("query", "get_ai_model_budget", GetAiModelBudgetRequest, dict,
                          lambda _request: dict(reply), execution="thread")
    if ingest is None:
        return
    registry.register("command", "record_ai_cost", RecordAiCostRequest, dict, ingest.record_cost,
                      execution="thread")
    registry.register("command", "record_simulated_decision", RecordSimulatedDecisionRequest, dict,
                      ingest.record_simulated, execution="thread")
