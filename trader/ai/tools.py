"""Every trader read of a decision goes through here, so it can be recorded and replayed (spec 11; Plan 6 Ruling 1)."""
from __future__ import annotations

import importlib.metadata
import os
from typing import Any, Mapping

from trader.ai.replay import RecordingClock
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused

TOOL_METHODS: Mapping[str, str] = {
    "quote": "get_ai_entry_quote", "policy": "get_ai_risk_policy", "deployment": "get_ai_deployment",
    "account": "get_account_values", "positions": "get_positions", "discovery": "discover_ai_candidates",
}
TOOL_ERROR_KEY = "__tool_error__"


class ToolUnavailable(Exception):
    def __init__(self, tool: str, code: str):
        super().__init__(f"{tool}: {code}")
        self.tool, self.code = tool, code


def code_version() -> str:
    """Ruling 18: MMR_CODE_VERSION, else the installed distribution. A missing distribution raises."""
    return os.environ.get("MMR_CODE_VERSION") or importlib.metadata.version("mmr")


def _request_key(unit_key: str, role: str, call_seq: int) -> str:
    return f"{unit_key}/{role}/{call_seq}"                       # Plan 4 Ruling 10


class LiveTools:
    def __init__(self, *, unit_key: str, reads: Any, recorder: Any, clock: Any, gateway: Any, deadline: Any):
        self.unit_key, self.gateway, self.deadline = unit_key, gateway, deadline
        self.clock = RecordingClock(clock)
        self._reads, self._recorder = reads, recorder

    def request_key(self, role: str, call_seq: int) -> str:
        return _request_key(self.unit_key, role, call_seq)

    async def read(self, tool: str, args: Mapping[str, Any]) -> Any:
        try:
            reply = await self._reads.call(TOOL_METHODS[tool], dict(args))
        except (RpcNotSent, RpcOutcomeUnknown, RpcRefused) as exc:
            await self._recorder.record_tool_result(self.unit_key, tool, args, {TOOL_ERROR_KEY: exc.code})
            raise ToolUnavailable(tool, exc.code) from None
        await self._recorder.record_tool_result(self.unit_key, tool, args, reply)
        return reply

    async def given(self, name: str, value: Any) -> Any:
        """A caller input (the entry source, a role's health): recorded, so replay gets the same value."""
        await self._recorder.record_tool_result(self.unit_key, f"given:{name}", {}, value)
        return value

    async def finish(self, config_digest: str) -> None:
        await self._recorder.record_clock_values(self.unit_key, self.clock.values)
        await self._recorder.record_manifest(self.unit_key, code_version=code_version(), config_digest=config_digest)


class ReplayTools:
    """No reads object at all: a reply that was not recorded is ReplayIncomplete, never a fetch."""

    def __init__(self, session: Any, unit_key: str):
        self.unit_key, self._session = unit_key, session
        self.clock, self.gateway = session.clock, session.gateway
        self.deadline = session.gateway.new_deadline(unit_key)

    def request_key(self, role: str, call_seq: int) -> str:
        return _request_key(self.unit_key, role, call_seq)

    async def read(self, tool: str, args: Mapping[str, Any]) -> Any:
        reply = self._session.tool_result(tool, args)
        if isinstance(reply, dict) and TOOL_ERROR_KEY in reply:
            raise ToolUnavailable(tool, reply[TOOL_ERROR_KEY])
        return reply

    async def given(self, name: str, value: Any) -> Any:
        return self._session.tool_result(f"given:{name}", {})

    async def finish(self, config_digest: str) -> None:
        return None
