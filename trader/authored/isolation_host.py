"""Host step of ``mmr ai-author prove-isolation`` (SP3 spec 7.9; decisions D2, D3; rulings 15 and 22).

It runs the in-container step in the research container and checks its JSON against the trader's wire model.
It needs no key and runs no ``docker inspect``; the CLI signs the record as ``cli``.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Callable

from pydantic import ValidationError

from trader.automation.authored_wire import RecordIsolationProofRequest
from trader.research.strategy_paths import repo_root

RUN_TIMEOUT_SECONDS = 900
Runner = Callable[[list[str]], str]


class ProveIsolationError(Exception):
    """The run could not finish (no sandbox, no output, a malformed proof); nothing is recorded."""


def compose_command(text: str) -> list[str]:
    """The compose command as docker.sh uses it, on this checkout's docker-compose.yml."""
    return [*shlex.split(text), "-f", str(repo_root() / "docker-compose.yml")]


def in_container_command(compose: list[str]) -> list[str]:
    return [*compose, "exec", "-T", "research", "mmr", "ai-author", "prove-isolation", "--in-container"]


def run_command(argv: list[str]) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=RUN_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise ProveIsolationError(f"{' '.join(argv[:3])} ... exited {completed.returncode}: "
                                  f"{completed.stderr.strip()[-500:]}")
    return completed.stdout


def parse_proof(stdout: str) -> dict:
    """The last output line as a proof body, checked by the same model the trader uses (ruling 21)."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ProveIsolationError("the isolation checks printed nothing")
    try:
        body = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise ProveIsolationError("the isolation checks did not print JSON") from None
    try:
        RecordIsolationProofRequest.model_validate(body)
    except ValidationError as exc:
        raise ProveIsolationError(f"the isolation checks printed not a valid proof: {exc.errors()[:3]}") from None
    return body


def prove_isolation(compose: list[str], run: Runner = run_command) -> dict:
    return parse_proof(run(in_container_command(compose)))
