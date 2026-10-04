#!/usr/bin/env python3
"""P2 release gate — research evidence foundation verification.

Orchestrates the P2 release checklist from
``docs/superpowers/rollout/trading-income-research-runbook.md``:

**Synthetic (CI-safe, offline):**
  1. Research DB isolation guard on deployed config
  2. Full research pytest suite (manifests, registry, validation, attestations, bundles)
  3. Optional exported bundle verification (``--bundle PATH``)
  4. ``docker compose config`` validity (skipped when Docker unavailable)

**Manual (non-fungible):**
  5. Signed ``PAPER_ELIGIBLE`` attestation on a real qualified experiment family
     — attach with ``--manual-attestation-report PATH``

Usage:
    python3 scripts/p2_release_gate.py --synthetic-only
    python3 scripts/p2_release_gate.py --json --output p2-gate.json
    python3 scripts/p2_release_gate.py --bundle ~/.local/share/mmr/artifacts/<id>
    python3 scripts/p2_release_gate.py --manual-attestation-report attest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from release_gate_common import (
    ReleaseReport,
    PhaseResult,
    add_standard_cli,
    config_digest,
    deployed_config_path,
    emit_report,
    git_digest,
    is_synthetic_ok,
    load_deployed_yaml,
    run_docker_compose_config,
    run_pytest,
    setup_paths,
    validate_commit_config_binding,
)

setup_paths()


def _research_path_isolation(cfg_path: Path, raw: dict[str, Any]) -> PhaseResult:
    name = "research_db_isolation"
    detail: dict[str, Any] = {"config_path": str(cfg_path)}
    try:
        from trader.config import MMRConfig

        cfg = MMRConfig.from_yaml(str(cfg_path))
        research = cfg.storage.research_duckdb_path
        operational = {
            "duckdb_path": cfg.storage.duckdb_path,
            "history_duckdb_path": cfg.storage.history_duckdb_path,
            "journal_duckdb_path": cfg.storage.journal_duckdb_path,
        }
        detail["research_duckdb_path"] = research
        detail["operational_paths"] = operational
        if research in operational.values():
            return PhaseResult(
                name, "failed", detail=detail,
                error="research_duckdb_path collides with an operational DuckDB path",
            )
        return PhaseResult(name, "passed", detail=detail)
    except Exception as exc:
        detail["yaml_keys"] = sorted(raw.keys())
        return PhaseResult(name, "failed", detail=detail, error=str(exc))


def _verify_exported_bundle(bundle_path: Path, keyring: Optional[Path]) -> PhaseResult:
    name = "research_bundle_verify"
    detail: dict[str, Any] = {"bundle_path": str(bundle_path)}
    if not bundle_path.is_dir():
        return PhaseResult(
            name, "failed", detail=detail,
            error=f"bundle directory not found: {bundle_path}",
        )
    try:
        from trader.data.duckdb_store import DuckDBConnection
        from trader.data.schema_migrations import SchemaMigrator
        from trader.research.bundle import ResearchBundle
        from trader.research.schema import apply_research_migrations
        from trader.research.signing import load_verify_key

        trusted: dict[str, Any] = {}
        if keyring and keyring.is_dir():
            for pem in sorted(keyring.glob("*.pem")):
                trusted[pem.stem] = load_verify_key(str(pem))
            detail["public_key_ids"] = sorted(trusted)

        import tempfile
        with tempfile.TemporaryDirectory(prefix="p2-bundle-verify-") as tmp:
            db = DuckDBConnection.get_instance(str(Path(tmp) / "verify.duckdb"))
            apply_research_migrations(SchemaMigrator(db))
            verified = ResearchBundle(db).verify(
                bundle_path, trusted_public_keys=trusted,
            )
        detail["manifest_digest"] = verified.manifest_digest
        detail["artifact_id"] = verified.artifact_id
        detail["dataset_manifest_digest"] = verified.dataset_manifest_digest
        return PhaseResult(name, "passed", detail=detail)
    except Exception as exc:
        return PhaseResult(name, "failed", detail=detail, error=str(exc))


def _run_reproduce_experiment(bundle_path: Path) -> PhaseResult:
    name = "reproduce_experiment"
    detail = {"bundle_path": str(bundle_path)}
    import subprocess

    from release_gate_common import PROJECT_ROOT

    cmd = [
        "uv", "run", "python3", "scripts/reproduce_experiment.py",
        "--bundle", str(bundle_path),
    ]
    proc = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    detail["exit_code"] = proc.returncode
    detail["stdout"] = proc.stdout.strip()[-1000:]
    detail["stderr"] = proc.stderr.strip()[-1000:]
    if proc.returncode != 0:
        return PhaseResult(
            name, "failed", detail=detail,
            error=f"reproduce_experiment.py exited {proc.returncode}",
        )
    return PhaseResult(name, "passed", detail=detail)


def _validate_manual_attestation_report(
    data: dict[str, Any], *, commit: str, config: str,
) -> PhaseResult:
    name = "manual_paper_attestation"
    required = (
        "artifact_id", "bundle_path", "bundle_manifest_digest",
        "attestation_state", "commit_digest", "config_digest", "passed",
    )
    missing = [k for k in required if k not in data]
    if missing:
        return PhaseResult(
            name, "failed",
            detail={"missing_fields": missing},
            error=f"manual attestation report missing: {', '.join(missing)}",
        )
    if not data.get("passed"):
        return PhaseResult(name, "failed", detail=data, error="manual attestation passed=false")
    if data.get("attestation_state") != "PAPER_ELIGIBLE":
        return PhaseResult(
            name, "failed", detail=data,
            error=f"attestation_state must be PAPER_ELIGIBLE, got {data.get('attestation_state')!r}",
        )
    bind_err = validate_commit_config_binding(data, commit=commit, config=config)
    if bind_err:
        return PhaseResult(name, "failed", detail=data, error=bind_err)
    bundle = Path(str(data["bundle_path"])).expanduser()
    if not bundle.is_dir():
        return PhaseResult(
            name, "failed", detail=data,
            error=f"bundle not found: {bundle}",
        )
    return PhaseResult(name, "passed", detail=data)


def run_gate(
    *,
    skip_docker: bool = False,
    skip_pytest: bool = False,
    bundle_path: Optional[Path] = None,
    public_key_ring: Optional[Path] = None,
    manual_attestation_report: Optional[Path] = None,
) -> ReleaseReport:
    started = time.monotonic()
    phases: list[PhaseResult] = []
    cfg_path = deployed_config_path()
    commit = git_digest()
    cfg_digest = config_digest(cfg_path)
    raw = load_deployed_yaml(cfg_path)

    phases.append(_research_path_isolation(cfg_path, raw))

    if not skip_pytest:
        phases.append(run_pytest([
            "tests/research/",
            "tests/test_research_config.py",
        ], timeout=120))

    if bundle_path is not None:
        phases.append(_verify_exported_bundle(bundle_path, public_key_ring))
        phases.append(_run_reproduce_experiment(bundle_path))
    else:
        phases.append(PhaseResult(
            "research_bundle_verify",
            "skipped",
            detail={"reason": "pass --bundle PATH to verify an exported artifact bundle"},
        ))

    if skip_docker:
        phases.append(PhaseResult(
            "docker_compose_config", "skipped", detail={"reason": "--skip-docker"},
        ))
    else:
        phases.append(run_docker_compose_config())

    manual_complete = False
    if manual_attestation_report is not None:
        try:
            data = json.loads(manual_attestation_report.read_text())
        except Exception as exc:
            phases.append(PhaseResult(
                "manual_paper_attestation", "failed",
                error=f"could not read manual attestation report: {exc}",
            ))
        else:
            phases.append(_validate_manual_attestation_report(
                data, commit=commit, config=cfg_digest,
            ))
            manual_complete = phases[-1].status == "passed"
    else:
        phases.append(PhaseResult(
            "manual_paper_attestation",
            "pending",
            detail={
                "reason": (
                    "non-fungible gate — a real qualified dataset/family must earn "
                    "a signed PAPER_ELIGIBLE attestation"
                ),
                "command": (
                    "python3 scripts/p2_release_gate.py "
                    "--bundle ~/.local/share/mmr/artifacts/<artifact-id> "
                    "--manual-attestation-report p2-attestation.json"
                ),
            },
        ))

    passed = is_synthetic_ok(phases) and manual_complete
    return ReleaseReport(
        program="P2",
        version=1,
        commit_digest=commit,
        config_digest=cfg_digest,
        deployed_config_path=str(cfg_path),
        phases=phases,
        passed=passed,
        elapsed_s=round(time.monotonic() - started, 3),
        manual_gate_complete=manual_complete,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_standard_cli(parser)
    parser.add_argument(
        "--bundle", type=Path, default=None,
        help="Verify an exported read-only research artifact bundle.",
    )
    parser.add_argument(
        "--public-key-ring", type=Path, default=None,
        help="Directory of *.pem verification keys for --bundle (default: ~/.config/mmr/keys).",
    )
    parser.add_argument(
        "--manual-attestation-report", type=Path, default=None,
        help="Validate a previously recorded signed PAPER_ELIGIBLE attestation report.",
    )
    args = parser.parse_args(argv)

    keyring = args.public_key_ring
    if keyring is None and args.bundle is not None:
        keyring = Path.home() / ".config" / "mmr" / "keys"

    report = run_gate(
        skip_docker=args.skip_docker,
        skip_pytest=args.skip_pytest,
        bundle_path=args.bundle,
        public_key_ring=keyring,
        manual_attestation_report=args.manual_attestation_report,
    )
    pending_cmd = None
    for phase in report.phases:
        if phase.name == "manual_paper_attestation" and phase.status == "pending":
            pending_cmd = (phase.detail or {}).get("command")
    return emit_report(
        report,
        synthetic_only=args.synthetic_only,
        json_out=args.json,
        output=args.output,
        manual_pending_command=pending_cmd,
        manual_phase_name="manual_paper_attestation",
    )


if __name__ == "__main__":
    raise SystemExit(main())
