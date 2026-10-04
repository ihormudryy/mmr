"""Activate's only source of bundles: real, eligible, and bound to the strategy.

Every candidate under ``artifacts/sha256_*`` must verify in paper mode, carry
qualified research evidence (no fixture provenance, a complete passing paper-v1
decision) and pass the strategy binding check; the newest by expiry wins. Each rejected candidate
keeps its reason so a refusal can explain itself.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from trader.automation.paper_materials import require_qualified_research_evidence
from trader.automation.strategy_binding import check_strategy_binding


@dataclass(frozen=True)
class EligibleBundle:
    path: Path
    artifact_id: str
    expires_at: dt.datetime


class NoEligibleBundle(Exception):
    def __init__(self, message: str, reasons: tuple[str, ...] = ()):
        super().__init__(message)
        self.reasons = reasons


def _manifest_artifact_id(bundle_dir: Path) -> str:
    manifest = json.loads((bundle_dir / 'manifest.json').read_text(encoding='utf-8'))
    artifact_id = manifest.get('artifact_id') if isinstance(manifest, dict) else None
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError('manifest.json has no artifact_id')
    return artifact_id


def find_eligible_bundle(*, artifacts_root: Path, verifier: Any, strategy: Mapping[str, Any],
                         strategy_file: Path, now: dt.datetime) -> EligibleBundle:
    candidates: list[EligibleBundle] = []
    reasons: list[str] = []
    bundle_dirs = sorted(artifacts_root.glob('sha256_*')) if artifacts_root.is_dir() else []
    for bundle_dir in bundle_dirs:
        try:
            artifact_id = _manifest_artifact_id(bundle_dir)
            verified = verifier.verify(bundle_dir, 'paper', artifact_id, now)
            require_qualified_research_evidence(bundle_dir)
            check_strategy_binding(
                verified.attested_strategy, module_file=strategy_file,
                class_name=str(strategy.get('class_name', '')), params=strategy.get('params'),
                conids=strategy.get('conids'), bar_size=str(strategy.get('bar_size', '')))
        except Exception as exc:  # each bundle's reason is kept; the caller raises once
            reasons.append(f'{bundle_dir.name[:24]}: {type(exc).__name__}: {exc}')
            continue
        candidates.append(EligibleBundle(bundle_dir, artifact_id, verified.expires_at))
    if not candidates:
        raise NoEligibleBundle(
            f'no eligible bundle under {artifacts_root} is bound to this strategy', tuple(reasons))
    return max(candidates, key=lambda bundle: bundle.expires_at)
