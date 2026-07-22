"""Shared helpers for P1/P2/P3 program release-gate scripts."""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"

PhaseStatus = Literal["passed", "failed", "skipped", "pending"]
UTC = dt.timezone.utc

MANUAL_PHASE_NAMES = frozenset({
    "manual_ib_paper_soak",
    "manual_paper_attestation",
    "ib_paper_preflight",
    "ib_paper_session_watch",
})


def setup_paths() -> None:
    for path in (PROJECT_ROOT, SCRIPTS):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


@dataclass
class PhaseResult:
    name: str
    status: PhaseStatus
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ReleaseReport:
    program: str
    version: int
    commit_digest: str
    config_digest: str
    deployed_config_path: str
    phases: list[PhaseResult]
    passed: bool
    elapsed_s: float
    manual_gate_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def git_digest() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
        ).strip()
    except Exception:
        return "unknown"


def deployed_config_path() -> Path:
    env = os.environ.get("TRADER_CONFIG", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".config" / "mmr" / "trader.yaml"


def config_digest(path: Path) -> str:
    raw = path.read_bytes() if path.is_file() else b""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_deployed_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import yaml

    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def run_pytest(targets: list[str], *, timeout: int = 60) -> PhaseResult:
    name = "pytest:" + ",".join(Path(t).stem for t in targets)
    cmd = [
        "uv", "run", "--frozen", "--extra", "test", "pytest",
        *targets, "-q", f"--timeout={timeout}",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        return PhaseResult(name, "failed", error=str(exc))
    detail = {
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout.strip()[-2000:],
        "stderr_tail": proc.stderr.strip()[-2000:],
    }
    if proc.returncode == 0:
        return PhaseResult(name, "passed", detail=detail)
    return PhaseResult(
        name, "failed", detail=detail, error=f"pytest exited {proc.returncode}",
    )


def run_docker_compose_config() -> PhaseResult:
    name = "docker_compose_config"
    try:
        proc = subprocess.run(
            ["docker", "compose", "config", "--quiet"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return PhaseResult(name, "skipped", detail={"reason": "docker binary not found"})
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "docker compose failed"
        if "permission denied" in err.lower() or "cannot connect" in err.lower():
            return PhaseResult(name, "skipped", detail={"reason": err})
        return PhaseResult(name, "failed", detail={"stderr": err}, error=err)
    return PhaseResult(name, "passed")


def xnys_session_open(now: dt.datetime) -> tuple[bool, dict[str, Any]]:
    try:
        import exchange_calendars as xcals

        cal = xcals.get_calendar("XNYS")
        if not cal.is_session(now.date()):
            return False, {"reason": "not_trading_session", "date": str(now.date())}
        open_ts = cal.session_open(now.date())
        close_ts = cal.session_close(now.date())
        open_utc = open_ts.to_pydatetime().replace(tzinfo=UTC)
        close_utc = close_ts.to_pydatetime().replace(tzinfo=UTC)
        in_rth = open_utc <= now <= close_utc
        return in_rth, {
            "session_date": str(now.date()),
            "open_utc": open_utc.isoformat(),
            "close_utc": close_utc.isoformat(),
            "calendar_version": getattr(xcals, "__version__", "unknown"),
        }
    except Exception as exc:
        return False, {"reason": "calendar_error", "error": str(exc)}


def trader_status_snapshot() -> tuple[bool, dict[str, Any]]:
    try:
        from trader.messaging.clientserver import consume
        from trader.sdk import MMR

        mmr = MMR()
        status = mmr.status()
        if not status.get("connected"):
            return False, {"connected": False, "reason": "trader_service unreachable"}

        svc: dict[str, Any] = {}
        try:
            svc = consume(mmr._rpc.rpc(return_type=dict).get_status())  # type: ignore[attr-defined]
        except Exception as exc:
            svc = {"get_status_error": str(exc)}

        payload = {
            "connected": True,
            "account": status.get("account"),
            "ib_upstream_connected": status.get(
                "ib_upstream_connected", svc.get("ib_upstream_connected"),
            ),
            "positions": status.get("positions"),
            "open_orders": status.get("open_orders"),
            "semantic_readiness": svc.get("semantic_readiness"),
            "liveness": svc.get("liveness"),
        }
        ready = bool((svc.get("semantic_readiness") or {}).get("ready"))
        return ready, payload
    except Exception as exc:
        return False, {"connected": False, "error": str(exc)}


def ib_paper_session_watch(minutes: int, interval_s: int = 60) -> PhaseResult:
    name = "ib_paper_session_watch"
    snapshots: list[dict[str, Any]] = []
    deadline = dt.datetime.now(tz=UTC) + dt.timedelta(minutes=minutes)

    while dt.datetime.now(tz=UTC) < deadline:
        ready, status = trader_status_snapshot()
        snapshots.append({
            "at": dt.datetime.now(tz=UTC).isoformat(),
            "semantic_ready": ready,
            **status,
        })
        time.sleep(interval_s)

    final = snapshots[-1] if snapshots else {}
    flat = (
        final.get("connected")
        and int(final.get("positions") or 0) == 0
        and int(final.get("open_orders") or 0) == 0
    )
    detail = {
        "snapshots": len(snapshots),
        "watch_minutes": minutes,
        "final": final,
        "broker_flat": flat,
    }
    if not snapshots:
        return PhaseResult(name, "failed", detail=detail, error="no snapshots collected")
    if not flat:
        return PhaseResult(
            name, "failed", detail=detail,
            error="session ended without broker-confirmed flat book",
        )
    return PhaseResult(name, "passed", detail=detail)


def is_synthetic_ok(phases: Sequence[PhaseResult]) -> bool:
    return all(
        p.status in {"passed", "skipped"}
        for p in phases
        if p.name not in MANUAL_PHASE_NAMES
    )


def validate_commit_config_binding(
    data: dict[str, Any], *, commit: str, config: str,
) -> Optional[str]:
    if data.get("commit_digest") != commit:
        return f"commit mismatch: report {data['commit_digest']!r} != {commit!r}"
    if data.get("config_digest") != config:
        return f"config mismatch: report {data['config_digest']!r} != {config!r}"
    return None


def add_standard_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument(
        "--synthetic-only", action="store_true",
        help="Exit 0 when the synthetic half passes; manual gate may stay pending.",
    )


def emit_report(
    report: ReleaseReport,
    *,
    synthetic_only: bool,
    json_out: bool,
    output: Optional[Path],
    manual_pending_command: Optional[str] = None,
    manual_phase_name: str = "manual_ib_paper_soak",
) -> int:
    payload = report.to_dict()
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if output:
        output.write_text(text)

    if json_out:
        print(text)
    else:
        label = "PASSED" if report.passed else "FAILED"
        print(f"{report.program} release gate {label} ({report.elapsed_s}s)")
        print(f"  commit={report.commit_digest}")
        print(f"  config={report.config_digest}")
        print(f"  deployed_config={report.deployed_config_path}")
        print(f"  manual_gate_complete={report.manual_gate_complete}")
        for phase in report.phases:
            print(f"  - {phase.name}: {phase.status.upper()}")
            if phase.error:
                print(f"      {phase.error}")
            if (
                phase.name == manual_phase_name
                and phase.status == "pending"
                and manual_pending_command
            ):
                print(f"      next: {manual_pending_command}")

    if synthetic_only:
        return 0 if is_synthetic_ok(report.phases) else 1
    return 0 if report.passed else 1
