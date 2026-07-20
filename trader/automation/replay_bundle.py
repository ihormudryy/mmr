"""P3 Task 8 — sealed forensic trading-day replay bundles.

``ReplayBundle.seal(session_id)`` collects immutable session evidence, refuses
incomplete or unresolved days, and atomically writes a read-only checksummed
bundle (tmp + fsync + rename). ``ReplayBundle.verify`` accepts only a complete,
checksum-valid, non-traversing tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from trader.research.bundle import BundleDigest
from trader.research.canonical import canonical_json_bytes

FORMAT_VERSION = 1

# Payload entries written beside manifest.json (order is not significant;
# manifest file table is always sorted).
ENTRY_KEYS: tuple[str, ...] = (
    "artifact_attestation",
    "bars",
    "quote_evidence",
    "broker_snapshots",
    "policies",
    "intents",
    "decisions",
    "commands",
    "broker_events",
    "attribution",
    "breaker_actions",
    "reconciliation_actions",
    "operator_actions",
    "xnys_schedule",
    "sizing",
    "signals",
    "decision_trace",
)

_FILE_NAMES: tuple[str, ...] = tuple(f"{key}.json" for key in ENTRY_KEYS) + ("manifest.json",)

# Commands that may appear in a sealed day. Everything else is unresolved.
_TERMINAL_COMMAND_STATES = frozenset({"RESOLVED", "REJECTED"})

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReplayBundleError(Exception):
    """Session evidence cannot be sealed or the on-disk bundle is unsafe."""


class SessionEvidenceStore(Protocol):
    def load_session(self, session_id: str) -> Mapping[str, Any]:
        """Return the complete evidence mapping for ``session_id``."""


@dataclass(frozen=True)
class VerifiedReplayBundle:
    """Verified, read-only projection of a sealed trading day."""

    session_id: str
    manifest_digest: str
    path: Path
    entries: Mapping[str, Any]
    manifest: Mapping[str, Any]


class ReplayBundle:
    """Seal and verify forensic trading-day bundles."""

    def __init__(self, store: SessionEvidenceStore, *, output_dir: Path):
        self._store = store
        self._output_dir = Path(output_dir)

    def seal(self, session_id: str) -> BundleDigest:
        """Atomically seal one complete session into a read-only bundle directory."""
        _require_safe_session_id(session_id)
        path = self._output_dir / session_id
        if path.exists():
            raise ReplayBundleError(f"bundle destination already exists: {path}")
        try:
            evidence = dict(self._store.load_session(session_id))
        except KeyError as exc:
            raise ReplayBundleError(f"missing session evidence: {session_id}") from exc
        _validate_seal_evidence(session_id, evidence)
        files = _canonical_files(session_id, evidence)
        return self._write_staged(path, files)

    @staticmethod
    def verify(path: Path) -> VerifiedReplayBundle:
        """Accept only a complete, checksum-valid, read-only seal directory."""
        root = Path(path)
        if not root.is_dir() or root.is_symlink():
            raise ReplayBundleError("bundle root must be a real directory")
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ReplayBundleError("bundle manifest is unsafe")
        try:
            raw_manifest = manifest_path.read_bytes()
            manifest = json.loads(raw_manifest)
        except (OSError, ValueError) as exc:
            raise ReplayBundleError("invalid bundle manifest") from exc
        if canonical_json_bytes(manifest) != raw_manifest:
            raise ReplayBundleError("manifest is not canonical JSON")
        _validate_manifest(manifest)
        if {child.name for child in root.iterdir()} != set(_FILE_NAMES):
            raise ReplayBundleError("bundle has unexpected or missing files")
        for name, checksum in manifest["files"].items():
            _require_safe_entry_name(name)
            child = root / name
            if child.is_symlink() or not child.is_file():
                raise ReplayBundleError(f"bundle file is unsafe: {name}")
            if _sha256(child.read_bytes()) != checksum:
                raise ReplayBundleError(f"checksum mismatch for {name}")
        if root.stat().st_mode & 0o222 or any(
                (root / name).stat().st_mode & 0o222 for name in _FILE_NAMES):
            raise ReplayBundleError("bundle is not read-only")

        entries: dict[str, Any] = {}
        for key in ENTRY_KEYS:
            name = f"{key}.json"
            raw = (root / name).read_bytes()
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ReplayBundleError(f"corrupt payload: {name}") from exc
            if canonical_json_bytes(payload) != raw:
                raise ReplayBundleError(f"noncanonical payload: {name}")
            entries[key] = payload
        return VerifiedReplayBundle(
            session_id=manifest["session_id"],
            manifest_digest=manifest["manifest_digest"],
            path=root,
            entries=entries,
            manifest=manifest,
        )

    def _write_staged(self, path: Path, files: Mapping[str, bytes]) -> BundleDigest:
        if set(files) != set(_FILE_NAMES):
            raise ReplayBundleError("internal bundle file set is invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{path.name}.staging-", dir=path.parent))
        try:
            for name in sorted(files):
                target = staging / name
                with target.open("wb") as handle:
                    handle.write(files[name])
                    handle.flush()
                    os.fsync(handle.fileno())
            _validate_staged_checksums(staging)
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.replace(staging, path)
            _chmod_read_only(path)
            return BundleDigest(
                manifest_digest=_manifest_digest(path / "manifest.json"),
                path=path,
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


def _require_safe_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id:
        raise ReplayBundleError("session_id is required")
    if (session_id != session_id.strip() or
            "/" in session_id or "\\" in session_id or
            ".." in session_id or session_id.startswith(".") or
            not _SAFE_SESSION_ID.match(session_id)):
        raise ReplayBundleError(f"unsafe session_id (path traversal): {session_id!r}")


def _require_safe_entry_name(name: str) -> None:
    if (not isinstance(name, str) or not name or
            "/" in name or "\\" in name or ".." in name or
            name != Path(name).name or name == "manifest.json"):
        raise ReplayBundleError(f"unsafe bundle entry name (path traversal): {name!r}")
    if name not in _FILE_NAMES:
        raise ReplayBundleError(f"unexpected bundle entry: {name}")


def _validate_seal_evidence(session_id: str, evidence: Mapping[str, Any]) -> None:
    missing = [key for key in ENTRY_KEYS if key not in evidence]
    if missing:
        raise ReplayBundleError(f"missing evidence: {', '.join(missing)}")
    if evidence.get("session_id") not in (None, session_id):
        raise ReplayBundleError("evidence session_id disagrees with seal target")
    schedule = evidence["xnys_schedule"]
    if not isinstance(schedule, Mapping):
        raise ReplayBundleError("missing evidence: xnys_schedule")
    for field in ("calendar_name", "calendar_version", "session_date",
                  "open_utc", "close_utc"):
        if not schedule.get(field):
            raise ReplayBundleError(f"missing evidence: xnys_schedule.{field}")
    commands = evidence["commands"]
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
        raise ReplayBundleError("commands must be a list")
    unresolved = []
    for cmd in commands:
        if not isinstance(cmd, Mapping):
            raise ReplayBundleError("invalid command row")
        state = cmd.get("state")
        if state not in _TERMINAL_COMMAND_STATES:
            unresolved.append(str(cmd.get("command_id") or state))
    if unresolved:
        raise ReplayBundleError(
            f"unresolved commands refuse seal: {', '.join(unresolved)}")
    # Bars / quotes / artifacts must be present even if empty lists are allowed
    # only for optional action logs. Core market evidence must be non-empty.
    for key in ("artifact_attestation", "bars", "quote_evidence",
                "broker_snapshots", "policies", "xnys_schedule", "decision_trace"):
        value = evidence[key]
        if value is None or value == {} or value == []:
            raise ReplayBundleError(f"missing evidence: {key}")


def _canonical_files(session_id: str, evidence: Mapping[str, Any]) -> dict[str, bytes]:
    files = {f"{key}.json": canonical_json_bytes(evidence[key]) for key in ENTRY_KEYS}
    checksums = {name: _sha256(data) for name, data in sorted(files.items())}
    schedule = evidence["xnys_schedule"]
    manifest = {
        "format_version": FORMAT_VERSION,
        "session_id": session_id,
        "calendar_name": schedule["calendar_name"],
        "calendar_version": schedule["calendar_version"],
        "files": checksums,
    }
    manifest["manifest_digest"] = _sha256(canonical_json_bytes(manifest))
    files["manifest.json"] = canonical_json_bytes(manifest)
    return files


def _validate_staged_checksums(staging: Path) -> None:
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    for name, checksum in manifest["files"].items():
        if _sha256((staging / name).read_bytes()) != checksum:
            raise ReplayBundleError(f"staged checksum mismatch for {name}")


def _validate_manifest(manifest: Any) -> None:
    required = {
        "format_version", "session_id", "calendar_name", "calendar_version",
        "files", "manifest_digest",
    }
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise ReplayBundleError("unsupported bundle format")
    if set(manifest) != required:
        raise ReplayBundleError("invalid bundle manifest")
    if not isinstance(manifest["session_id"], str) or not manifest["session_id"]:
        raise ReplayBundleError("invalid bundle session_id")
    _require_safe_session_id(manifest["session_id"])
    for key in ("calendar_name", "calendar_version", "manifest_digest"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise ReplayBundleError(f"invalid manifest field: {key}")
    files = manifest["files"]
    if not isinstance(files, dict):
        raise ReplayBundleError("invalid manifest file table")
    # Reject traversal / absolute names before the whitelist set check so
    # attackers cannot hide behind "unexpected file" wording.
    for name in files:
        if (not isinstance(name, str) or not name or
                "/" in name or "\\" in name or ".." in name or
                name != Path(name).name):
            raise ReplayBundleError(
                f"unsafe bundle entry name (path traversal): {name!r}")
    expected = set(_FILE_NAMES) - {"manifest.json"}
    if set(files) != expected or list(files) != sorted(files):
        raise ReplayBundleError("invalid manifest file table")
    for name, checksum in files.items():
        _require_safe_entry_name(name)
        if (not isinstance(checksum, str) or len(checksum) != 64 or
                not all(char in "0123456789abcdef" for char in checksum)):
            raise ReplayBundleError("invalid manifest checksum table")
    digest_body = dict(manifest)
    digest = digest_body.pop("manifest_digest")
    if _sha256(canonical_json_bytes(digest_body)) != digest:
        raise ReplayBundleError("manifest checksum mismatch")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_digest(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["manifest_digest"]


def _chmod_read_only(root: Path) -> None:
    for child in root.iterdir():
        child.chmod(0o444)
    root.chmod(0o555)
