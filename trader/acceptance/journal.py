"""The acceptance run journal (Plan 6 rulings 7 and 15).

Append-only JSON lines, one file per run, private to the operator. Before each
send the scenario writes an ``intent`` with the exact request body text; after
the reply it writes a ``receipt``. A resume replays the stored text unchanged,
so a host crash can never build a second, different command.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Optional

JOURNAL_NAME = "journal.jsonl"
KINDS = frozenset({"settings", "intent", "receipt", "step", "reading"})


class JournalError(RuntimeError):
    pass


def canonical_text(body: dict) -> str:
    """The exact body text kept in the journal: sorted keys, no spaces, no NaN."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)


class RunJournal:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._prepare_directory()
        self.path = self.directory / JOURNAL_NAME
        if self.path.is_symlink():
            raise JournalError(f"{self.path} is a symlink; refusing to open it")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)

    def _prepare_directory(self) -> None:
        if self.directory.is_symlink():
            raise JournalError(f"{self.directory} is a symlink; refusing to use it")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        if not stat.S_ISDIR(os.lstat(self.directory).st_mode):
            raise JournalError(f"{self.directory} is not a directory")

    # -- writing ----------------------------------------------------------------------------
    def append(self, kind: str, payload: dict) -> None:
        if kind not in KINDS:
            raise JournalError(f"unknown journal record kind {kind!r}")
        line = json.dumps({"kind": kind, **payload}, sort_keys=True, default=str, allow_nan=False)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def record_intent(self, step: str, method: str, principal: str, body_json: str,
                      command_id: Optional[str]) -> None:
        self.append("intent", {"step": step, "method": method, "principal": principal, "body_json": body_json,
                               "command_id": command_id})

    def record_receipt(self, step: str, reply: Optional[dict] = None, method: Optional[str] = None) -> None:
        self.append("receipt", {"step": step, "method": method, "reply": reply or {}})

    # -- reading ----------------------------------------------------------------------------
    def entries(self) -> list[dict]:
        with open(self.path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def of_kind(self, kind: str) -> list[dict]:
        return [entry for entry in self.entries() if entry["kind"] == kind]

    def settings(self) -> Optional[dict]:
        found = self.of_kind("settings")
        return found[-1] if found else None

    def intent_for(self, step: str, method: str) -> Optional[dict]:
        found = [e for e in self.of_kind("intent") if e["step"] == step and e["method"] == method]
        return found[-1] if found else None

    def receipt_for(self, step: str, method: Optional[str] = None) -> Optional[dict]:
        found = [e for e in self.of_kind("receipt")
                 if e["step"] == step and (method is None or e.get("method") in (None, method))]
        return found[-1] if found else None

    def step_result(self, name: str) -> Optional[dict]:
        found = [e for e in self.of_kind("step") if e["name"] == name]
        return found[-1] if found else None

    def sent_intents(self) -> list[dict]:
        return self.of_kind("intent")
