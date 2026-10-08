"""Verify a research bundle for a judgment-bound registration (SP2c spec 5.2 item 4).

Public keys only. The trader reads the bundle and never writes into the artifacts directory.
Every failure is a BundleRefused with its own code; no other exception leaves this module.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from trader.automation.artifact_verifier import ArtifactExpired, ArtifactVerifier, ArtifactVerifierError
from trader.automation.paper_materials import PaperMaterialsError, require_qualified_research_evidence
from trader.research.attest_export import bundle_dir_name
from trader.research.signing import InvalidKeyType, MalformedKey, load_verify_key

BUNDLE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CONID = re.compile(r"[1-9][0-9]*")
DEFAULT_ARTIFACTS_ROOT = "~/.local/share/mmr/artifacts"
DEFAULT_VERIFY_DIR = "~/.config/mmr/keys/verify"


class BundleRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class BundleFacts:
    bundle_digest: str
    artifact_id: str
    family_id: str
    strategy_path: str
    class_name: str
    file_hash: str
    params: Mapping[str, Any]
    conids: tuple[int, ...]
    bar_size: Optional[str]
    order_notional: Optional[float]
    reviewer: str
    reviewer_kind: str
    expires_at: dt.datetime


def _conids(instruments) -> tuple[int, ...]:
    values = []
    for text in instruments:
        if not isinstance(text, str) or not _CONID.fullmatch(text):     # ASCII only: int() takes other digits
            raise BundleRefused("BUNDLE_INVALID", f"attested instrument {text!r} is not a conid")
        values.append(int(text))
    return tuple(sorted(values))


class ResearchBundleCheck:
    def __init__(self, *, artifacts_root: Path, verify_dir: Path):
        self._root = Path(artifacts_root).expanduser()
        self._verify_dir = Path(verify_dir).expanduser()

    def _path(self, bundle_digest: str) -> Path:
        if not isinstance(bundle_digest, str) or not BUNDLE_DIGEST.fullmatch(bundle_digest):
            raise BundleRefused("BUNDLE_INVALID", "bundle_digest must be sha256:<64 hex>")
        return self._root / bundle_dir_name(bundle_digest)

    def _keys(self) -> list:
        try:
            paths = sorted(self._verify_dir.glob("*.pem"))
        except OSError as ex:
            raise BundleRefused("BUNDLE_KEYS_MISSING", f"verify key directory is unreadable: {type(ex).__name__}") from None
        if not paths:
            raise BundleRefused("BUNDLE_KEYS_MISSING", f"no public key in {self._verify_dir}")
        try:
            return [load_verify_key(str(path)) for path in paths]
        except (InvalidKeyType, MalformedKey, OSError) as ex:
            raise BundleRefused("BUNDLE_KEYS_MISSING", f"a verify key is unusable: {type(ex).__name__}") from None

    def manifest_artifact_id(self, bundle_digest: str) -> str:
        """The artifact the bundle names, read before verification only to choose JUDGMENT_MISMATCH."""
        try:
            return str(json.loads((self._path(bundle_digest) / "manifest.json").read_text())["artifact_id"])
        except (OSError, ValueError, KeyError, TypeError):
            raise BundleRefused("BUNDLE_MISSING", "no readable bundle manifest for this digest") from None

    def check(self, bundle_digest: str, *, artifact_id: str, now: dt.datetime) -> BundleFacts:
        path = self._path(bundle_digest)
        if not path.is_dir():
            raise BundleRefused("BUNDLE_MISSING", f"no bundle directory {path.name}")
        keys = self._keys()
        try:
            verified = ArtifactVerifier(keys).verify(path, "paper", artifact_id, now)
        except ArtifactExpired as ex:
            raise BundleRefused("BUNDLE_EXPIRED", str(ex)) from None
        except (ArtifactVerifierError, OSError, ValueError, KeyError, TypeError) as ex:
            raise BundleRefused("BUNDLE_INVALID", str(ex)) from None
        try:
            require_qualified_research_evidence(path)
        except PaperMaterialsError as ex:
            raise BundleRefused("BUNDLE_NOT_QUALIFIED", str(ex)) from None
        if "sha256:" + verified.manifest_digest != bundle_digest:
            raise BundleRefused("BUNDLE_INVALID", "the directory does not hold the bundle its name claims")
        attested = verified.attested_strategy
        if attested is None:
            raise BundleRefused("BUNDLE_INVALID", "the bundle carries no attested strategy")
        try:
            artifact = json.loads((path / "artifact.json").read_text())     # checksum-verified above
            review = json.loads((path / "review.json").read_text())
            return BundleFacts(
                bundle_digest=bundle_digest, artifact_id=verified.artifact_id, family_id=artifact["family_id"],
                strategy_path=attested.strategy_path, class_name=attested.class_name,
                file_hash="sha256:" + attested.source_digest, params=dict(verified.parameters),
                conids=_conids(verified.allowlist), bar_size=attested.bar_size,
                order_notional=attested.order_notional, reviewer=review["reviewer"],
                reviewer_kind=review["reviewer_kind"], expires_at=verified.expires_at)
        except (OSError, ValueError, KeyError, TypeError) as ex:
            raise BundleRefused("BUNDLE_INVALID", f"bundle file unreadable: {type(ex).__name__}") from None
