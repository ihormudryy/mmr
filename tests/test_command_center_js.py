"""Runs the dependency-free stateful browser-client tests under Node."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "command_center.test.js"


@pytest.mark.skipif(_NODE is None, reason="node runtime not available")
def test_command_center_js_stateful_behaviour():
    result = subprocess.run(
        [_NODE, str(_TEST_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_TEST_JS.parent),
    )
    assert result.returncode == 0, (
        f"command_center.test.js failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
