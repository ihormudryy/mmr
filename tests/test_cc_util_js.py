"""Runs the pure-logic node test for the command-center client's cc_util.js.

The dashboard JS has no browser unit-test harness, so the correctness-critical
side-effect-free helpers (quote-freshness math, degraded-banner predicate,
snapshot-supersede/rollback guard) are extracted into web/static/cc_util.js and
exercised by web/static/cc_util.test.js under node. This wrapper makes that
part of the pytest suite, skipping cleanly when node isn't installed (e.g. CI
images without a JS runtime) rather than failing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "cc_util.test.js"


@pytest.mark.skipif(_NODE is None, reason="node runtime not available")
def test_cc_util_js_pure_logic():
    result = subprocess.run(
        [_NODE, str(_TEST_JS)],
        capture_output=True, text=True, timeout=30, cwd=str(_TEST_JS.parent),
    )
    assert result.returncode == 0, (
        f"cc_util.test.js failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
