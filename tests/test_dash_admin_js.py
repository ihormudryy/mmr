"""Runs the dependency-free dashboard tab-controller regression under Node."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "dash_admin.test.js"


@pytest.mark.skipif(_NODE is None, reason="node runtime not available")
def test_research_tab_can_be_activated_from_location_hash():
    result = subprocess.run(
        [_NODE, str(_TEST_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_TEST_JS.parent),
    )
    assert result.returncode == 0, (
        f"dash_admin.test.js failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
