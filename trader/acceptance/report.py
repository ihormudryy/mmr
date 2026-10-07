"""The signed acceptance report (Plan 6 rulings 10, 13 and 18-22).

It follows ``FaultDrillReport`` (scripts/automation_fault_drill.py): canonical
JSON, an Ed25519 signature over every field but the signature itself, and the
commit and config digests. ``passed`` is computed, never set by a caller: every
step and end check passed, the live OCA shrink is ``PROVEN`` on ``ib_paper``
evidence, and an operator key signed it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

REPORT_KIND = "sp1_acceptance"
REPORT_VERSION = 1
HARNESS_NOTE = ("The deployment is a harness fixture (deployment_record: harness_fixture): its strategy_digest is a "
                "claim, not research evidence. Live restart recovery was not proven in this session; the synthetic "
                "restart tests are the gate. oca_shrink counts only when PROVEN on ib_paper evidence.")
OCA_RESULTS = ("PROVEN", "UNPROVEN", "FAILED", "NOT_RUN")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def commit_digest() -> str:
    try:
        out = subprocess.run(["git", "-C", str(_PROJECT_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def config_digest(path: Optional[Path] = None) -> str:
    target = path or Path(os.environ.get("TRADER_CONFIG", "~/.config/mmr/trader.yaml")).expanduser()
    try:
        return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    except Exception:
        return "unknown"


@dataclass
class AcceptanceReport:
    run_id: str
    account_id: str
    phase: str                                   # "run" or "finish"
    generated_at: str
    commit_digest: str
    config_digest: str
    steps: list[dict]
    end_checks: list[dict]
    oca_shrink: str = "NOT_RUN"
    evidence_source: str = "synthetic"           # "ib_paper" only from a real IB generation
    live_restart_recovery: str = "NOT_PROVEN"     # ruling 19: never claimed by this report
    telegram_live_delivery: str = "UNTESTED"     # ruling 18
    deployment_record: str = "harness_fixture"   # ruling 22
    signing_key: str = "ephemeral"               # "operator" | "ephemeral" (ruling 20)
    order_evidence: list[dict] = field(default_factory=list)
    harness_note: str = HARNESS_NOTE
    version: int = REPORT_VERSION
    kind: str = REPORT_KIND
    passed: bool = False
    public_key_id: str = ""
    signature: str = ""

    def __post_init__(self) -> None:
        if self.oca_shrink not in OCA_RESULTS:
            raise ValueError(f"oca_shrink must be one of {OCA_RESULTS}")
        if self.signing_key not in ("operator", "ephemeral"):
            raise ValueError("signing_key must be operator or ephemeral")
        self.passed = self.compute_passed()

    def compute_passed(self) -> bool:
        checks = list(self.steps) + list(self.end_checks)
        return (bool(checks) and all(c.get("passed") is True for c in checks)
                and self.oca_shrink == "PROVEN" and self.evidence_source == "ib_paper"
                and self.signing_key == "operator" and self.live_restart_recovery == "NOT_PROVEN"
                and self.deployment_record == "harness_fixture")

    def _signable_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature", None)
        return payload

    def sign(self, signer: Any, *, key_source: str) -> None:
        from trader.research.canonical import canonical_json_bytes
        if key_source not in ("operator", "ephemeral"):
            raise ValueError("key_source must be operator or ephemeral")
        self.signing_key = key_source
        self.public_key_id = signer.public_key_id
        self.passed = self.compute_passed()
        self.signature = signer.sign_message(canonical_json_bytes(self._signable_payload()))

    def verify(self, public_key: Any) -> None:
        """Raises when unsigned, tampered with, or signed by another key."""
        from trader.research.canonical import canonical_json_bytes
        from trader.research.signing import verify_bytes
        if not self.signature:
            raise ValueError("report is unsigned")
        verify_bytes(public_key, canonical_json_bytes(self._signable_payload()), self.signature)
        if self.passed != self.compute_passed():
            raise ValueError("report passed flag does not match its own steps")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> Path:
        from trader.research.canonical import canonical_json_bytes
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, canonical_json_bytes(self.to_payload()) + b"\n")
        finally:
            os.close(fd)
        return path

    @classmethod
    def from_payload(cls, payload: dict) -> "AcceptanceReport":
        fields = dict(payload)
        passed, signature = fields.pop("passed", False), fields.pop("signature", "")
        report = cls(**fields)
        report.passed, report.signature = passed, signature
        return report

    @classmethod
    def load(cls, path: Path) -> "AcceptanceReport":
        return cls.from_payload(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))


def build_report(*, run_id: str = "acc-test", account_id: str = "DU111111", phase: str = "run",
                 steps: Iterable[Any] = (), end_checks: Iterable[Any] = (), oca_shrink: str = "NOT_RUN",
                 evidence_source: str = "synthetic", telegram_live_delivery: str = "UNTESTED",
                 order_evidence: Optional[list] = None, now: Optional[dt.datetime] = None) -> AcceptanceReport:
    """A report from step results (``StepResult`` or dicts). Signing sets ``signing_key``."""
    steps = [_as_dict(s) for s in steps]
    end_checks = [_as_dict(c) for c in end_checks]
    if not steps and not end_checks:
        steps = [{"name": "nothing_ran", "passed": False, "code": "NOT_RUN", "evidence": {}}]
    return AcceptanceReport(
        run_id=run_id, account_id=account_id, phase=phase,
        generated_at=(now or dt.datetime.now(dt.timezone.utc)).isoformat(),
        commit_digest=commit_digest(), config_digest=config_digest(), steps=steps, end_checks=end_checks,
        oca_shrink=oca_shrink, evidence_source=evidence_source, telegram_live_delivery=telegram_live_delivery,
        order_evidence=list(order_evidence or []))


def _as_dict(result: Any) -> dict:
    if isinstance(result, dict):
        return dict(result)
    return {"name": result.name, "passed": result.passed, "code": result.code, "evidence": result.evidence}
