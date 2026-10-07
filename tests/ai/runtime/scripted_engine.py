"""A deterministic DecisionEngine for the Plan 5 tests: fixed results per hook, every call recorded."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional, Union

from trader.ai.engine import EngineResult, ProposedDecision

Scripted = Union[EngineResult, Callable[[Any], EngineResult]]
HOOKS = ("entry_signal", "exit_signal", "entry_cycle", "position_cycle")


class ScriptedEngine:
    def __init__(self, *, block: Optional[asyncio.Event] = None, error: Optional[Exception] = None,
                 **results: Scripted):
        unknown = set(results) - set(HOOKS)
        if unknown:
            raise ValueError(f"unknown hooks {sorted(unknown)}")
        self.results = {hook: results.get(hook, EngineResult()) for hook in HOOKS}
        self.block, self.error = block, error
        self.calls: list[tuple[str, Any]] = []

    @classmethod
    def from_file(cls, path: str) -> "ScriptedEngine":
        """JSON {"entry_signal": [ProposedDecision fields, ...], ...} for the child-process test."""
        raw = json.loads(Path(path).read_text())
        return cls(**{hook: EngineResult(decisions=tuple(ProposedDecision(**d) for d in decisions))
                      for hook, decisions in raw.items()})

    async def _run(self, hook: str, ctx: Any) -> EngineResult:
        self.calls.append((hook, ctx))
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error
        result = self.results[hook]
        return result(ctx) if callable(result) else result

    async def on_entry_signal(self, ctx):
        return await self._run("entry_signal", ctx)

    async def on_exit_signal(self, ctx):
        return await self._run("exit_signal", ctx)

    async def on_entry_cycle(self, ctx):
        return await self._run("entry_cycle", ctx)

    async def on_position_cycle(self, ctx):
        return await self._run("position_cycle", ctx)

    def hooks_called(self) -> list[str]:
        return [hook for hook, _ in self.calls]
