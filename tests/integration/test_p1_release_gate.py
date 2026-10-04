"""P1 release gate orchestrator — synthetic path must pass in CI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import p1_release_gate as gate  # noqa: E402
from release_gate_common import config_digest, deployed_config_path, git_digest  # noqa: E402


def test_synthetic_gate_passes_without_manual_soak():
    report = gate.run_gate(skip_docker=True, skip_pytest=True)
    assert report.program == "P1"
    assert report.commit_digest and report.commit_digest != "unknown"
    assert report.config_digest.startswith("sha256:")
    statuses = {p.name: p.status for p in report.phases}
    assert statuses["command_plane_drill"] == "passed"
    assert statuses["manual_ib_paper_soak"] == "pending"
    assert report.manual_gate_complete is False
    assert report.passed is False


def test_manual_soak_report_validates_commit_and_config(tmp_path: Path):
    commit = git_digest()
    cfg_digest = config_digest(deployed_config_path())
    report_path = tmp_path / "soak.json"
    report_path.write_text(json.dumps({
        "session_date": "2026-07-18",
        "commit_digest": commit,
        "config_digest": cfg_digest,
        "passed": True,
    }))
    result = gate.run_gate(
        skip_docker=True,
        skip_pytest=True,
        manual_soak_report=report_path,
    )
    assert result.manual_gate_complete is True
    assert result.passed is True
    manual = next(p for p in result.phases if p.name == "manual_ib_paper_soak")
    assert manual.status == "passed"
