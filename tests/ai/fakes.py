"""Shared test doubles for the ai package."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.ai.config import AiConfig, load_ai_config

UTC = timezone.utc


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


def config_text(
    *,
    cap: str = "2000",
    calls_per_hour: int = 120,
    deadline: int = 60,
    max_in_flight: int = 2,
    orchestrator_backend: str = "openrouter",
    orchestrator_model: str = "vendor/orch-1",
    jev_backend: str = "openrouter",
    jev_model: str = "vendor/jev-1",
    call_timeout: str = "45",
    extra_top_level: str = "",
) -> str:
    return f"""
roles:
  orchestrator: {{backend: {orchestrator_backend}, model: "{orchestrator_model}", call_timeout_seconds: {call_timeout}}}
  jev: {{backend: {jev_backend}, model: "{jev_model}", call_timeout_seconds: {call_timeout}}}
pricing:
  openrouter:
    "vendor/orch-1": {{input_usd_per_million: 3.0, output_usd_per_million: 15.0}}
    "vendor/jev-1": {{input_usd_per_million: 1.0, output_usd_per_million: 5.0}}
budget:
  model_budget_usd_per_day: {cap}
  calls_per_hour: {calls_per_hour}
  max_in_flight: {max_in_flight}
  decision_deadline_seconds: {deadline}
{extra_top_level}
"""


def write_config(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "ai.yaml"
    path.write_text(text if text is not None else config_text())
    return path


def load_test_config(tmp_path: Path, **kwargs) -> AiConfig:
    return load_ai_config(str(write_config(tmp_path, config_text(**kwargs))))


# 240_000 micro-USD: 60000 input tokens at $3/M plus 4000 output tokens at $15/M.
ORCHESTRATOR_WORST_CASE_MICROS = 240_000
# 80_000 micro-USD: 60000 at $1/M plus 4000 at $5/M.
JEV_WORST_CASE_MICROS = 80_000
