#!/usr/bin/env python3
"""P3 release gate — end-to-end verification for deterministic automation.

Orchestrates the full P3 release checklist from
``docs/superpowers/rollout/trading-income-operations-runbook.md``.

See ``scripts/p1_release_gate.py`` and ``scripts/p2_release_gate.py`` for the
other program gates. Shared helpers live in ``scripts/release_gate_common.py``.

Usage:
    python3 scripts/p3_release_gate.py --synthetic-only
    python3 scripts/p3_release_gate.py --json --output p3-gate.json
    python3 scripts/p3_release_gate.py --ib-paper --watch-minutes 390
    python3 scripts/p3_release_gate.py --manual-soak-report soak.json
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
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
    ib_paper_session_watch,
    is_synthetic_ok,
    load_deployed_yaml,
    run_docker_compose_config,
    run_pytest,
    setup_paths,
    trader_status_snapshot,
    validate_commit_config_binding,
    xnys_session_open,
)

setup_paths()

import automation_paper_drill  # noqa: E402
import command_plane_drill  # noqa: E402

UTC = dt.timezone.utc


def _validate_manual_soak_report(
    data: dict[str, Any], *, commit: str, config: str,
) -> PhaseResult:
    name = "manual_ib_paper_soak"
    required = (
        "session_date", "replay_bundle_path", "replay_manifest_digest",
        "commit_digest", "config_digest", "passed",
    )
    missing = [k for k in required if k not in data]
    if missing:
        return PhaseResult(
            name, "failed",
            detail={"missing_fields": missing},
            error=f"manual soak report missing: {', '.join(missing)}",
        )
    if not data.get("passed"):
        return PhaseResult(name, "failed", detail=data, error="manual soak report passed=false")
    bind_err = validate_commit_config_binding(data, commit=commit, config=config)
    if bind_err:
        return PhaseResult(name, "failed", detail=data, error=bind_err)
    bundle = Path(str(data["replay_bundle_path"])).expanduser()
    if not bundle.is_dir():
        return PhaseResult(
            name, "failed", detail=data,
            error=f"replay bundle not found: {bundle}",
        )
    return PhaseResult(name, "passed", detail=data)


def _ib_paper_preflight(cfg_path: Path, raw: dict[str, Any]) -> PhaseResult:
    name = "ib_paper_preflight"
    errors: list[str] = []
    detail: dict[str, Any] = {"config_path": str(cfg_path)}

    authority = raw.get("command_authority") or {}
    automation = raw.get("automation") or {}
    detail["command_authority_enabled"] = bool(authority.get("enabled"))
    detail["automation_enabled"] = bool(automation.get("enabled"))
    detail["automation_live_enabled"] = bool(automation.get("live_enabled"))

    if not authority.get("enabled"):
        errors.append("command_authority.enabled must be true for P3 paper activation")
    if not automation.get("enabled"):
        errors.append("automation.enabled must be true for the P3 manual gate")
    if automation.get("live_enabled"):
        errors.append("automation.live_enabled must remain false through P3")
    if not automation.get("expected_artifact_id"):
        errors.append("automation.expected_artifact_id is required")
    if not automation.get("strategy_name"):
        errors.append("automation.strategy_name is required")

    bundle = Path(str(automation.get("artifact_bundle_path") or "")).expanduser()
    keyring = Path(str(automation.get("public_key_ring_path") or "")).expanduser()
    detail["artifact_bundle_path"] = str(bundle)
    detail["public_key_ring_path"] = str(keyring)
    if automation.get("enabled"):
        if not bundle.is_dir():
            errors.append(f"artifact bundle missing: {bundle}")
        if not keyring.is_dir():
            errors.append(f"public key ring missing: {keyring}")

    now = dt.datetime.now(tz=UTC)
    rth_open, session = xnys_session_open(now)
    detail["xnys"] = session
    detail["xnys_rth_open"] = rth_open
    if not rth_open:
        errors.append("XNYS regular session is not open — run during market hours")

    ready, status = trader_status_snapshot()
    detail["trader"] = status
    if not status.get("connected"):
        errors.append("trader_service is not reachable")
    elif not ready:
        failed = (status.get("semantic_readiness") or {}).get("failed") or []
        errors.append(f"semantic readiness not ready: {failed}")

    if errors:
        return PhaseResult(name, "failed", detail=detail, error="; ".join(errors))
    return PhaseResult(name, "passed", detail=detail)


def run_gate(
    *,
    soak_seconds: float = 120.0,
    skip_docker: bool = False,
    ib_paper: bool = False,
    watch_minutes: int = 0,
    manual_soak_report: Optional[Path] = None,
    skip_pytest: bool = False,
) -> ReleaseReport:
    started = time.monotonic()
    phases: list[PhaseResult] = []
    cfg_path = deployed_config_path()
    commit = git_digest()
    cfg_digest = config_digest(cfg_path)

    p1 = command_plane_drill.run_drills()
    phases.append(PhaseResult(
        "p1_command_plane_drill",
        "passed" if p1.passed else "failed",
        detail=dataclasses.asdict(p1),
        error=None if p1.passed else "command plane drill failed",
    ))

    p3 = automation_paper_drill.run_drills(soak_s=soak_seconds)
    phases.append(PhaseResult(
        "p3_automation_drill",
        "passed" if p3.passed else "failed",
        detail=p3.to_dict(),
        error=None if p3.passed else "automation drill failed",
    ))

    if not skip_pytest:
        phases.append(run_pytest([
            "tests/integration/test_command_plane_activation.py",
            "tests/integration/test_automated_vertical_slice.py",
        ], timeout=60))
        phases.append(run_pytest(["tests/automation/"], timeout=60))

    if skip_docker:
        phases.append(PhaseResult(
            "docker_compose_config", "skipped", detail={"reason": "--skip-docker"},
        ))
    else:
        phases.append(run_docker_compose_config())

    manual_complete = False
    if manual_soak_report is not None:
        try:
            data = json.loads(manual_soak_report.read_text())
        except Exception as exc:
            phases.append(PhaseResult(
                "manual_ib_paper_soak", "failed",
                error=f"could not read manual soak report: {exc}",
            ))
        else:
            phases.append(_validate_manual_soak_report(data, commit=commit, config=cfg_digest))
            manual_complete = phases[-1].status == "passed"
    elif ib_paper:
        raw = load_deployed_yaml(cfg_path)
        phases.append(_ib_paper_preflight(cfg_path, raw))
        if watch_minutes > 0 and phases[-1].status == "passed":
            phases.append(ib_paper_session_watch(watch_minutes))
        manual_complete = all(
            p.status == "passed"
            for p in phases
            if p.name in {"ib_paper_preflight", "ib_paper_session_watch", "manual_ib_paper_soak"}
        )
    else:
        phases.append(PhaseResult(
            "manual_ib_paper_soak",
            "pending",
            detail={
                "reason": "non-fungible gate — run during XNYS RTH with automation enabled",
                "command": (
                    "python3 scripts/p3_release_gate.py --ib-paper --watch-minutes 390 "
                    "--json --output p3-manual-soak.json"
                ),
            },
        ))

    passed = is_synthetic_ok(phases) and manual_complete
    return ReleaseReport(
        program="P3",
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
    parser.add_argument("--soak-seconds", type=float, default=120.0)
    parser.add_argument(
        "--ib-paper", action="store_true",
        help="Run IB-paper preflight (+ optional session watch). Requires live stack.",
    )
    parser.add_argument(
        "--watch-minutes", type=int, default=0,
        help="With --ib-paper: poll trader until flat or timeout (e.g. 390 for full session).",
    )
    parser.add_argument(
        "--manual-soak-report", type=Path, default=None,
        help="Validate a previously recorded manual soak JSON report.",
    )
    args = parser.parse_args(argv)

    report = run_gate(
        soak_seconds=args.soak_seconds,
        skip_docker=args.skip_docker,
        ib_paper=args.ib_paper,
        watch_minutes=args.watch_minutes,
        manual_soak_report=args.manual_soak_report,
        skip_pytest=args.skip_pytest,
    )
    pending_cmd = None
    for phase in report.phases:
        if phase.name == "manual_ib_paper_soak" and phase.status == "pending":
            pending_cmd = (phase.detail or {}).get("command")
    return emit_report(
        report,
        synthetic_only=args.synthetic_only,
        json_out=args.json,
        output=args.output,
        manual_pending_command=pending_cmd,
    )


if __name__ == "__main__":
    raise SystemExit(main())
