"""Public research verification, signing keys and bundle hints (no bundle creation)."""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
from pathlib import Path

from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError, VerifiedArtifact
from trader.research.canonical import canonical_json_bytes
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.signing import (
    AttestationSigner,
    InvalidKeyType,
    MalformedKey,
    generate_private_key_pem,
    load_verify_key,
)


class PaperMaterialsError(Exception):
    """Raised when paper automation key setup or bundle verification fails."""


def verify_qualified_paper_bundle(
    *,
    bundle_path: Path,
    public_key_ring_path: Path,
    expected_artifact_id: str,
    now: dt.datetime,
    revoked_digests: frozenset[str] = frozenset(),
    strategy: dict | None = None,
) -> VerifiedArtifact:
    """Read existing offline evidence using public keys only; never mint it."""
    try:
        keys = [load_verify_key(str(path)) for path in sorted(public_key_ring_path.glob("*.pem"))]
        verified = ArtifactVerifier(keys).verify(
            bundle_path, expected_mode="paper", expected_artifact_id=expected_artifact_id,
            now=now, revoked_digests=revoked_digests,
        )
        require_qualified_research_evidence(bundle_path)
        if strategy is not None:
            family = json.loads((bundle_path / "family.json").read_text())
            params = dict(strategy.get("params") or {})
            params.pop("artifact_bundle_path", None)  # transport binding, not a strategy parameter
            if (
                strategy.get("module") != family["strategy_path"]
                or strategy.get("class_name") != family["class_name"]
                or canonical_json_bytes(params) != canonical_json_bytes(verified.parameters)
            ):
                raise PaperMaterialsError("strategy module, class or parameters do not match research artifact")
        return verified
    except (ArtifactVerifierError, InvalidKeyType, MalformedKey, OSError, ValueError, TypeError) as exc:
        raise PaperMaterialsError(f"research evidence verification failed: {exc}") from exc


def require_qualified_research_evidence(bundle_path: Path) -> None:
    """Reject drill provenance AFTER full bundle/signature verification.

    This is an additional gate for activation and dispatch, not a replacement
    for ArtifactVerifier. Signatures establish integrity, not that
    an experiment was measured. Operators must audit the offline evidence.
    """
    try:
        family = json.loads((bundle_path / "family.json").read_text())
        review = json.loads((bundle_path / "review.json").read_text())
        decision = json.loads((bundle_path / "decision.json").read_text())
        if (
            review["reviewer"] == "bootstrap"  # pre-separation fixture bundles
            or family["validation_protocol"].get("evidence_kind") == "offline_fixture"
        ):
            raise PaperMaterialsError("offline fixture is not qualified research or promotion evidence")
        results = decision["results"]
        if (
            decision["state"] != "PAPER_ELIGIBLE"
            or decision["passed"] is not True
            or decision["ruleset_digest"] != PAPER_V1.digest
            or len(results) != len(PAPER_V1.rules)
            or {result["code"] for result in results} != {rule.code for rule in PAPER_V1.rules}
        ):
            raise PaperMaterialsError("complete passing paper-v1 quantitative research evidence required")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PaperMaterialsError(f"invalid research provenance: {exc}") from exc


def default_key_paths(config_dir: Path) -> tuple[Path, Path, Path]:
    """Return ``(private_pem, verify_dir, public_pem)`` under *config_dir*."""
    verify_dir = config_dir / "keys" / "verify"
    private_pem = config_dir / "keys" / "private" / "signing.pem"
    public_pem = verify_dir / "paper-automation.pem"
    return private_pem, verify_dir, public_pem


def read_allocation_binding_hints(bundle_path: Path | str) -> dict[str, str]:
    """Read digests + public_key_id from a research artifact ``attestation.json``.

    Used by the Command Center Scaling form Prefill — never invents digests.
    Returns only non-empty string fields present on the attestation. Does not
    include ``evidence_digest`` (that comes from ScalingGate / prepare, not
    the research eligibility attestation).
    """
    path = Path(bundle_path).expanduser()
    attestation_path = path / "attestation.json" if path.is_dir() else path
    if not attestation_path.is_file():
        return {}
    try:
        raw = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    hints: dict[str, str] = {}
    for key in (
        "artifact_digest",
        "allowlist_digest",
        "ruleset_digest",
        "public_key_id",
    ):
        value = raw.get(key)
        if value is None and key == "artifact_digest":
            value = raw.get("artifact_id")
        text = str(value).strip() if value is not None else ""
        if text:
            hints[key] = text
    return hints


def _write_private_key(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(
            f"refusing to overwrite existing private key {path}; pass --force"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = generate_private_key_pem()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PaperMaterialsError(f"private key permissions too open: {oct(mode)}")


def _write_public_key(signer: AttestationSigner, path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(
            f"refusing to overwrite existing public key {path}; pass --force"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(signer.public_key_pem())
    os.chmod(path, 0o644)


def ensure_signing_keypair(
    *,
    private_key_path: Path,
    public_key_path: Path,
    force: bool = False,
) -> tuple[AttestationSigner, bool]:
    """Ensure Ed25519 PKCS8 signing keypair exists; return ``(signer, reused)``."""
    if (
        private_key_path.exists()
        and public_key_path.exists()
        and not force
    ):
        signer = AttestationSigner.from_key_file(str(private_key_path))
        stored_public = public_key_path.read_bytes()
        expected_public = signer.public_key_pem()
        if stored_public != expected_public:
            raise PaperMaterialsError(
                f"public key at {public_key_path} does not match private key "
                f"at {private_key_path} (public_key_id={signer.public_key_id}); "
                "pass force=True to regenerate"
            )
        return signer, True

    _write_private_key(private_key_path, force=force)
    signer = AttestationSigner.from_key_file(str(private_key_path))
    _write_public_key(signer, public_key_path, force=force)
    return signer, False
