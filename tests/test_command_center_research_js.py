"""Runs the dependency-free Research browser-client tests under Node."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


_NODE = shutil.which("node")
_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "static"
    / "command_center_research.test.js"
)


@pytest.mark.skipif(_NODE is None, reason="node runtime not available")
def test_command_center_research_client():
    result = subprocess.run(
        [_NODE, str(_SCRIPT)],
        cwd=str(_SCRIPT.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"command_center_research.test.js failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
