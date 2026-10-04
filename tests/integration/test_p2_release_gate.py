"""P2 release gate orchestrator — synthetic path must pass in CI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import p2_release_gate as gate  # noqa: E402
from release_gate_common import config_digest, deployed_config_path, git_digest  # noqa: E402


def test_synthetic_gate_passes_without_manual_attestation():
    report = gate.run_gate(skip_docker=True, skip_pytest=True)
    assert report.program == "P2"
    assert report.commit_digest and report.commit_digest != "unknown"
    statuses = {p.name: p.status for p in report.phases}
    assert statuses["research_db_isolation"] == "passed"
    assert statuses["research_bundle_verify"] == "skipped"
    assert statuses["manual_paper_attestation"] == "pending"
    assert report.manual_gate_complete is False
    assert report.passed is False


def test_manual_attestation_report_validates(tmp_path: Path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    commit = git_digest()
    cfg_digest = config_digest(deployed_config_path())
    report_path = tmp_path / "attest.json"
    report_path.write_text(json.dumps({
        "artifact_id": "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bundle_path": str(bundle),
        "bundle_manifest_digest": "sha256:deadbeef",
        "attestation_state": "PAPER_ELIGIBLE",
        "commit_digest": commit,
        "config_digest": cfg_digest,
        "passed": True,
    }))
    result = gate.run_gate(
        skip_docker=True,
        skip_pytest=True,
        manual_attestation_report=report_path,
    )
    assert result.manual_gate_complete is True
    assert result.passed is True
