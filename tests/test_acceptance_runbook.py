"""SP1 Plan 6 Task 8: every command in the owner runbook parses, and every docker.sh flag exists."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from trader.mmr_cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "PAPER_ACCEPTANCE_SP1.md"
PLACEHOLDERS = {"<DU account>": "DU1234567", "<run_id>": "acc-20261007-abcdef",
                "<version digest>": "sha256:" + "a" * 64, "<base digest>": "sha256:" + "b" * 64}


def _spans(text):
    """Every inline code span and every line of a fenced block, newlines folded."""
    spans = [m.group(1).replace("\n", " ") for m in re.finditer(r"(?<!`)`([^`]+)`(?!`)", text)]
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S):
        spans.extend(line for line in block.splitlines() if line.strip())
    return spans


def mmr_commands(text):
    commands = []
    for span in _spans(text):
        for match in re.finditer(r"(?:^|&&\s*|;\s*|\s)mmr\s+(.+)", span):
            command = match.group(1)
            command = re.split(r"\s+(?:>|\||&&|;)\s*", command)[0].strip()
            for placeholder, value in PLACEHOLDERS.items():
                command = command.replace(placeholder, value)
            if re.search(r"<[^>]+>", command):
                raise AssertionError(f"unknown placeholder in {command!r}")
            commands.append(command)
    return commands


def test_the_runbook_exists_and_names_the_gate():
    text = RUNBOOK.read_text()
    assert "mmr experiment acceptance preflight" in text and "OCA_SHRINK_UNPROVEN" in text
    assert "mmr flatten" in text and "verify-report" in text


def test_every_mmr_command_in_the_runbook_parses():
    commands = mmr_commands(RUNBOOK.read_text())
    assert len(commands) >= 20, commands
    parser = build_parser()
    for command in commands:
        try:
            parser.parse_args(shlex.split(command))
        except SystemExit as exc:                             # argparse refuses with SystemExit(2)
            pytest.fail(f"runbook command does not parse: mmr {command} ({exc})")


def test_the_parser_used_here_rejects_a_bad_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args(shlex.split("experiment acceptance run --no-such-flag"))


def test_every_docker_sh_flag_in_the_runbook_exists():
    flags = set(re.findall(r"\./docker\.sh\s+(-[A-Za-z])\b", RUNBOOK.read_text()))
    assert flags, "the runbook names docker.sh flags"
    options = (ROOT / "docker.sh").read_text()
    for flag in flags:
        assert re.search(rf"^\s*{re.escape(flag)}\|", options, flags=re.M), f"docker.sh has no {flag}"
