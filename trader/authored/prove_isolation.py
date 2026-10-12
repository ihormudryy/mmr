"""The in-container step of ``mmr ai-author prove-isolation`` (SP3 spec 7.9; decision D3; ruling 15).

The host runs ``mmr ai-author prove-isolation --in-container`` with ``docker compose exec -T research``: the
research container mounts the research socket, so the checks use the research pool and never a live slot.
This step prints one JSON proof body and holds no key; the host checks it and records it as ``cli``.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any, Callable, TextIO

from trader.authored.sandbox_client import SandboxClient, SandboxUnavailable
from trader.sandbox.isolation_checks import run_isolation_checks
from trader.sandbox.protocol import RESEARCH_SOCKET

MAX_DETAIL_CHARS = 500
LIVE_SESSIONS_OPEN = "LIVE_SESSIONS_OPEN"
EXIT_LIVE_SESSIONS_OPEN = 3


def result_body(result: Any, ran_at: dt.datetime) -> dict:
    """A ``record_isolation_proof`` body: Plan 2's result, the build id from PONG and the run time."""
    return {"image_digest": result.image_digest, "passed": bool(result.passed), "ran_at": ran_at.isoformat(),
            "checks": [{"name": check.name, "passed": bool(check.passed),
                        "detail": str(check.detail)[:MAX_DETAIL_CHARS]} for check in result.checks]}


def run_in_container(socket_path: str = RESEARCH_SOCKET, out: TextIO = sys.stdout,
                     now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> int:
    ran_at = now()
    client = SandboxClient(socket_path)
    try:
        if _live_sessions_open(client):
            return EXIT_LIVE_SESSIONS_OPEN
        result = run_isolation_checks(client)
        if _live_sessions_open(client):           # one opened during the run: the run proves nothing
            return EXIT_LIVE_SESSIONS_OPEN
    except SandboxUnavailable as exc:
        print(f"the sandbox at {socket_path} is unavailable: {exc}", file=sys.stderr)
        return 2
    out.write(json.dumps(result_body(result, ran_at)) + "\n")
    return 0


def _live_sessions_open(client: SandboxClient) -> bool:
    """Owner ruling 2026-10-11 (Plan 2 rulings 21, 29): no hostile probe runs next to a live strategy's child."""
    live_open = client.ping().live_open
    if live_open == 0:
        return False
    print(f"{LIVE_SESSIONS_OPEN}: the sandbox reports {live_open!r} open live session(s); turn "
          "ai_paper.authored_strategies.enabled off (or stop the strategy service), wait for the sessions to "
          "close and run again", file=sys.stderr)
    return True
