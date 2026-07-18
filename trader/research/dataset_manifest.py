"""Immutable dataset manifests (P2 Task 2, design §4.1).

A ``DatasetManifest`` identifies the exact research data an experiment used:
provenance, boundaries, per-file checksums, quality findings, and verified
corrections. Its SHA-256 ``digest`` is the primary key every downstream artifact
and Ed25519 attestation references, so the digest is order-independent over the
nested collections (insertion order can never fork it) and covers every
provenance field. A REQUIRED quality finding that did not pass makes the whole
dataset research-ineligible -- there is no override.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trader.research.canonical import sha256_digest

MANIFEST_DIGEST_PREFIX = "dataset_manifest"


@dataclass(frozen=True)
class QualityFinding:
    """One data-quality check result (missing-bar, duplicate, outlier,
    session-completeness, ...). A REQUIRED finding with ``passed=False`` makes
    the dataset ineligible; no caller flag can override that."""

    name: str
    passed: bool
    required: bool
    detail: str = ""


@dataclass(frozen=True)
class DatasetFile:
    """A content-addressed data file in the dataset (checksum provenance)."""

    path: str
    sha256: str
    rows: int


@dataclass(frozen=True)
class DatasetCorrection:
    """A verified correction -- the ONLY sanctioned way data may differ from the
    vendor. Genuine gaps, halts, and crashes are kept as-is; only a correction
    carrying the original value, replacement, source, reason, and reviewer may
    alter a bar."""

    conid: int
    timestamp: dt.datetime
    field: str
    original_value: str
    replacement_value: str
    source: str
    reason: str
    reviewer: str


@dataclass(frozen=True)
class DatasetManifest:
    vendor: str
    retrieval_timestamp: dt.datetime
    bar_interval: str
    timestamp_convention: str
    session_calendar: str
    calendar_version: str
    adjustment_policy: str
    start_boundary: dt.datetime
    end_boundary: dt.datetime
    spread_source: str
    instruments: tuple[int, ...]
    files: tuple[DatasetFile, ...]
    findings: tuple[QualityFinding, ...]
    corrections: tuple[DatasetCorrection, ...] = ()

    @property
    def research_eligible(self) -> bool:
        """Ineligible iff any REQUIRED finding did not pass. Derived purely from
        the findings -- deliberately a property, not a settable field, so nothing
        can force eligibility."""
        return all(f.passed for f in self.findings if f.required)

    @property
    def digest(self) -> str:
        return dataset_manifest_digest(self)


def _canonical_body(m: DatasetManifest) -> dict:
    """Order-independent canonical projection for digesting: nested collections
    are sorted by a stable key so insertion order can't fork the digest."""
    return {
        "vendor": m.vendor,
        "retrieval_timestamp": m.retrieval_timestamp,
        "bar_interval": m.bar_interval,
        "timestamp_convention": m.timestamp_convention,
        "session_calendar": m.session_calendar,
        "calendar_version": m.calendar_version,
        "adjustment_policy": m.adjustment_policy,
        "start_boundary": m.start_boundary,
        "end_boundary": m.end_boundary,
        "spread_source": m.spread_source,
        "instruments": sorted(m.instruments),
        "files": [
            {"path": f.path, "sha256": f.sha256, "rows": f.rows}
            for f in sorted(m.files, key=lambda f: f.path)
        ],
        "findings": [
            {"name": f.name, "passed": f.passed, "required": f.required, "detail": f.detail}
            for f in sorted(m.findings, key=lambda f: f.name)
        ],
        "corrections": [
            {"conid": c.conid, "timestamp": c.timestamp, "field": c.field,
             "original_value": c.original_value, "replacement_value": c.replacement_value,
             "source": c.source, "reason": c.reason, "reviewer": c.reviewer}
            for c in sorted(m.corrections, key=lambda c: (c.conid, c.timestamp, c.field))
        ],
    }


def dataset_manifest_digest(manifest: DatasetManifest) -> str:
    """The manifest's SHA-256 identity (the research-DB primary key)."""
    return sha256_digest(MANIFEST_DIGEST_PREFIX, _canonical_body(manifest))
