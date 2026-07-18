"""Research DB schema + dataset-manifest persistence (P2 Task 2).

Research migrations 1-2 create the append-only, digest-keyed manifest tables in
the SEPARATE research DuckDB. ``DatasetManifestRepository`` exposes ONLY
``seal``/``get`` -- a sealed manifest is immutable (a correction creates a NEW
manifest linked by digest, never an in-place edit), so there is deliberately no
update/delete/unseal. ``seal`` is content-addressed and idempotent.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator
from trader.research.dataset_manifest import (
    DatasetCorrection,
    DatasetFile,
    DatasetManifest,
    QualityFinding,
)

RESEARCH_MIGRATION_DATASET_MANIFESTS = 1
RESEARCH_MIGRATION_DATASET_QUALITY = 2

_MANIFEST_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS dataset_manifests (
        digest VARCHAR PRIMARY KEY,
        vendor VARCHAR NOT NULL,
        retrieval_timestamp TIMESTAMPTZ NOT NULL,
        bar_interval VARCHAR NOT NULL,
        timestamp_convention VARCHAR NOT NULL,
        session_calendar VARCHAR NOT NULL,
        calendar_version VARCHAR NOT NULL,
        adjustment_policy VARCHAR NOT NULL,
        start_boundary TIMESTAMPTZ NOT NULL,
        end_boundary TIMESTAMPTZ NOT NULL,
        spread_source VARCHAR NOT NULL,
        instruments VARCHAR NOT NULL,
        research_eligible BOOLEAN NOT NULL,
        sealed_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dataset_files (
        manifest_digest VARCHAR NOT NULL,
        path VARCHAR NOT NULL,
        sha256 VARCHAR NOT NULL,
        row_count BIGINT NOT NULL,
        PRIMARY KEY (manifest_digest, path)
    )
    """,
)

_QUALITY_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS dataset_quality_findings (
        manifest_digest VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        passed BOOLEAN NOT NULL,
        required BOOLEAN NOT NULL,
        detail VARCHAR NOT NULL,
        PRIMARY KEY (manifest_digest, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dataset_corrections (
        manifest_digest VARCHAR NOT NULL,
        conid BIGINT NOT NULL,
        bar_timestamp TIMESTAMPTZ NOT NULL,
        field VARCHAR NOT NULL,
        original_value VARCHAR NOT NULL,
        replacement_value VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        reason VARCHAR NOT NULL,
        reviewer VARCHAR NOT NULL,
        PRIMARY KEY (manifest_digest, conid, bar_timestamp, field)
    )
    """,
)


def apply_research_migrations(migrator: SchemaMigrator) -> None:
    """Set up the WHOLE offline research DB (idempotent): dataset manifests
    (migrations 1-2), the experiment registry (migrations 3-6), and the
    eligibility decision store (migration 7). The research DB is a single file,
    so one call bootstraps every research table. The downstream migrations are
    imported lazily to keep this module free of a hard dependency on the
    registry/eligibility modules at import time."""
    migrator.apply(version=RESEARCH_MIGRATION_DATASET_MANIFESTS,
                   name="research_dataset_manifests",
                   statements=list(_MANIFEST_STATEMENTS))
    migrator.apply(version=RESEARCH_MIGRATION_DATASET_QUALITY,
                   name="research_dataset_quality",
                   statements=list(_QUALITY_STATEMENTS))
    from trader.research.experiment_registry import apply_experiment_migrations
    apply_experiment_migrations(migrator)
    from trader.research.eligibility import apply_eligibility_migrations
    apply_eligibility_migrations(migrator)


class DigestConflict(Exception):
    """A claimed digest doesn't match the manifest, or a stored manifest's
    content no longer hashes to its digest key (corruption). Fail closed --
    never return or overwrite evidence under a mismatched digest."""


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class DatasetManifestRepository:
    """Append-only, digest-keyed store for sealed dataset manifests."""

    def __init__(self, db: Any):
        self._db = db

    def seal(self, manifest: DatasetManifest, *, sealed_at: dt.datetime,
             expected_digest: Optional[str] = None) -> str:
        digest = manifest.digest
        if expected_digest is not None and expected_digest != digest:
            raise DigestConflict(
                f"claimed digest {expected_digest!r} != computed {digest!r}")

        def _tx(conn):
            if conn.execute("SELECT 1 FROM dataset_manifests WHERE digest = ?",
                            [digest]).fetchone() is not None:
                return digest  # idempotent: content-addressed, already sealed
            conn.execute(
                "INSERT INTO dataset_manifests (digest, vendor, retrieval_timestamp, "
                "bar_interval, timestamp_convention, session_calendar, calendar_version, "
                "adjustment_policy, start_boundary, end_boundary, spread_source, "
                "instruments, research_eligible, sealed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [digest, manifest.vendor, manifest.retrieval_timestamp,
                 manifest.bar_interval, manifest.timestamp_convention,
                 manifest.session_calendar, manifest.calendar_version,
                 manifest.adjustment_policy, manifest.start_boundary,
                 manifest.end_boundary, manifest.spread_source,
                 json.dumps(sorted(manifest.instruments)),
                 manifest.research_eligible, sealed_at])
            for f in manifest.files:
                conn.execute(
                    "INSERT INTO dataset_files (manifest_digest, path, sha256, row_count) "
                    "VALUES (?, ?, ?, ?)", [digest, f.path, f.sha256, f.rows])
            for q in manifest.findings:
                conn.execute(
                    "INSERT INTO dataset_quality_findings (manifest_digest, name, passed, "
                    "required, detail) VALUES (?, ?, ?, ?, ?)",
                    [digest, q.name, q.passed, q.required, q.detail])
            for c in manifest.corrections:
                conn.execute(
                    "INSERT INTO dataset_corrections (manifest_digest, conid, bar_timestamp, "
                    "field, original_value, replacement_value, source, reason, reviewer) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [digest, c.conid, c.timestamp, c.field, c.original_value,
                     c.replacement_value, c.source, c.reason, c.reviewer])
            return digest

        return self._db.transaction(_tx)

    def get(self, digest: str) -> Optional[DatasetManifest]:
        def _tx(conn):
            row = conn.execute(
                "SELECT vendor, retrieval_timestamp, bar_interval, timestamp_convention, "
                "session_calendar, calendar_version, adjustment_policy, start_boundary, "
                "end_boundary, spread_source, instruments FROM dataset_manifests "
                "WHERE digest = ?", [digest]).fetchone()
            if row is None:
                return None
            files = tuple(
                DatasetFile(path=r[0], sha256=r[1], rows=int(r[2]))
                for r in conn.execute(
                    "SELECT path, sha256, row_count FROM dataset_files "
                    "WHERE manifest_digest = ? ORDER BY path", [digest]).fetchall())
            findings = tuple(
                QualityFinding(name=r[0], passed=bool(r[1]), required=bool(r[2]), detail=r[3])
                for r in conn.execute(
                    "SELECT name, passed, required, detail FROM dataset_quality_findings "
                    "WHERE manifest_digest = ? ORDER BY name", [digest]).fetchall())
            corrections = tuple(
                DatasetCorrection(conid=int(r[0]), timestamp=_as_utc(r[1]), field=r[2],
                                  original_value=r[3], replacement_value=r[4], source=r[5],
                                  reason=r[6], reviewer=r[7])
                for r in conn.execute(
                    "SELECT conid, bar_timestamp, field, original_value, replacement_value, "
                    "source, reason, reviewer FROM dataset_corrections "
                    "WHERE manifest_digest = ? ORDER BY conid, bar_timestamp, field",
                    [digest]).fetchall())
            manifest = DatasetManifest(
                vendor=row[0], retrieval_timestamp=_as_utc(row[1]), bar_interval=row[2],
                timestamp_convention=row[3], session_calendar=row[4], calendar_version=row[5],
                adjustment_policy=row[6], start_boundary=_as_utc(row[7]),
                end_boundary=_as_utc(row[8]), spread_source=row[9],
                instruments=tuple(json.loads(row[10])), files=files, findings=findings,
                corrections=corrections)
            if manifest.digest != digest:
                raise DigestConflict(
                    f"stored manifest {digest!r} no longer matches its content digest "
                    f"{manifest.digest!r} (corruption)")
            return manifest

        return self._db.transaction(_tx)
