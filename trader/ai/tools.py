"""Every trader read of a decision goes through here, so it can be recorded and replayed (spec 11; Plan 6 Ruling 1)."""
from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

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


TRADER_ROOT = Path(__file__).resolve().parents[1]
# The code a judgment depends on: the ai package and the three trader modules it may import (Plan 6).
JUDGMENT_SOURCES: tuple[Path, ...] = (
    TRADER_ROOT / "ai", TRADER_ROOT / "automation" / "ai_discovery_wire.py",
    TRADER_ROOT / "automation" / "risk_limits.py", TRADER_ROOT / "automation" / "ai_paper_sizing.py")


def source_digest(sources: Iterable[Path], *, root: Path) -> str:
    """sha256 over every judgment source file (sorted relative path and bytes), not a package version."""
    files = sorted({file for source in sources
                    for file in (source.rglob("*.py") if source.is_dir() else (source,))})
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(file.read_bytes() + b"\0")
    return "src-sha256:" + digest.hexdigest()


@functools.lru_cache(maxsize=1)
def code_version() -> str:
    """The manifest's code identity (PR #86 thread 4211895936): the content of the judgment source, computed
    once per process, plus MMR_CONTAINER_DIGEST when the container sets it."""
    identity = source_digest(JUDGMENT_SOURCES, root=TRADER_ROOT)
    container = os.environ.get("MMR_CONTAINER_DIGEST")
    return f"{identity}+{container}" if container else identity


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

    async def recorded_refusal(self, try_no: int) -> Optional[str]:
        """Live there is nothing to read ahead: the call itself decides."""
        return None

    async def record_try(self, try_no: int, refusal_code: Optional[str]) -> None:
        """What try N of a retrying unit came to: the refusal code, or None when the call went out. A refusal
        creates no journal attempt, so replay needs this to take the same branch on every try."""
        await self.given(f"try:{try_no}", refusal_code)

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

    async def recorded_refusal(self, try_no: int) -> Optional[str]:
        return await self.given(f"try:{try_no}", None)

    async def record_try(self, try_no: int, refusal_code: Optional[str]) -> None:
        return None

    async def finish(self, config_digest: str) -> None:
        return None
