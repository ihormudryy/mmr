"""Artifact verification gate — the only path that authorises a signed strategy bundle to run.

``ArtifactVerifier.verify`` enforces the following chain in strict order (fail-closed):

1. Bundle integrity (manifest checksum + file checksums + canonical JSON).
2. Attestation signature (Ed25519; unknown key → reject).
3. Eligibility state — CANDIDATE / SUSPENDED / RETIRED are always rejected.
4. Mode gate — PAPER_ELIGIBLE cannot run live; CANARY_ELIGIBLE is required for live.
5. Attestation expiry.
6. Revocation check (caller-supplied revoked digest set).
7. Expected-binding match (artifact id, allowlist, ruleset, mode, allocation, instruments).
8. Read-only mount enforcement in live mode (writable mounts are rejected).

Only a ``VerifiedArtifact`` returned by this function may be passed downstream.
Callers must NOT cache raw attestation dicts or pass strategy-service "verified"
booleans — every load and every command re-runs the full chain.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Literal, Mapping, Optional, Sequence, Tuple

from trader.research.attestation import (
    AttestationError,
    AttestationVerifier,
    BadSignature,
    BindingMismatch,
    EligibilityAttestation,
    Expired,
    ExpectedBindings,
    Revoked,
    UnknownKey,
    VerifiedEligibility,
)
from trader.research.bundle import BundleError, ResearchBundle


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ArtifactVerifierError(Exception):
    """Any verification failure — always fail-closed, never a soft warning."""


class ArtifactStateForbidden(ArtifactVerifierError):
    """The artifact's eligibility state does not permit the requested mode."""


class ArtifactExpired(ArtifactVerifierError):
    """The attestation has passed its ``expires_at`` timestamp."""


class ArtifactRevoked(ArtifactVerifierError):
    """The attestation's payload digest is in the revocation set."""


class ArtifactSignatureFailed(ArtifactVerifierError):
    """The Ed25519 signature does not verify, or the key is untrusted."""


class ArtifactBindingMismatch(ArtifactVerifierError):
    """A validly-signed attestation does not match the expected bindings."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifiedArtifact:
    """Immutable record of a successfully verified strategy artifact.

    Only the fields needed downstream are exposed — no raw key material, no
    full attestation blob. Callers record ``verification_reason_codes`` in the
    audit log; ``public_key_id`` is the stable rotation identifier.
    """
    artifact_id: str
    manifest_digest: str
    dataset_manifest_digest: str
    # Strategy execution parameters (from artifact.json, verified by bundle checksum)
    parameters: Mapping[str, Any]
    # Instrument allow-list (from attested permitted_instruments)
    allowlist: Tuple[str, ...]
    # Risk ceiling from attestation (strategy may not exceed this)
    max_gross_allocation: float
    # Attestation expiry — callers must re-verify before this time
    expires_at: dt.datetime
    # Signing key identity (public; safe to log / persist)
    public_key_id: str
    # Compact reason codes from the eligibility decision (safe to persist)
    verification_reason_codes: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

_FORBIDDEN_STATES = frozenset({"CANDIDATE", "SUSPENDED", "RETIRED"})
_PAPER_STATES = frozenset({"PAPER_ELIGIBLE", "CANARY_ELIGIBLE"})
_LIVE_STATES = frozenset({"CANARY_ELIGIBLE"})


class ArtifactVerifier:
    """Stateless verifier that enforces the complete authority chain.

    Construct once with trusted Ed25519 public keys (one or more); call
    ``verify`` for every bundle load and every command boundary.

    Thread-safe after construction (no mutable state).
    """

    def __init__(self, trusted_public_keys: Sequence[Any]) -> None:
        """
        Args:
            trusted_public_keys: Sequence of ``Ed25519PublicKey`` objects.
                Must contain at least one key; an empty sequence raises
                ``ArtifactVerifierError`` immediately.
        """
        if not trusted_public_keys:
            raise ArtifactVerifierError(
                "ArtifactVerifier requires at least one trusted public key"
            )
        try:
            self._verifier = AttestationVerifier(list(trusted_public_keys))
        except ValueError as exc:
            raise ArtifactVerifierError(str(exc)) from exc
        # Expose as a {key_id: public_key} mapping for bundle.verify()
        self._keys_by_id: Mapping[str, Any] = self._verifier._keys

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(
        self,
        bundle_path: Path,
        expected_mode: Literal["paper", "live"],
        expected_artifact_id: str,
        now: dt.datetime,
        *,
        revoked_digests: FrozenSet[str] = frozenset(),
    ) -> VerifiedArtifact:
        """Verify the bundle and return immutable trusted facts, or raise.

        Args:
            bundle_path:          Read-only directory containing the exported
                                  research bundle (manifest.json + companion files).
            expected_mode:        ``"paper"`` or ``"live"``; gates eligibility state.
            expected_artifact_id: The artifact ID this deployment is configured to run.
                                  Rejects bundles containing a different artifact.
            now:                  The clock instant to use for expiry checks. Callers
                                  should pass ``datetime.now(UTC)``; tests may inject.
            revoked_digests:      Set of revoked attestation payload digests; any match
                                  raises ``ArtifactRevoked``.

        Returns:
            ``VerifiedArtifact`` containing only safe-to-persist fields.

        Raises:
            ArtifactVerifierError    — base class for all verification failures.
            ArtifactStateForbidden   — eligibility state rejects this mode.
            ArtifactExpired          — attestation has passed ``expires_at``.
            ArtifactRevoked          — payload digest is in ``revoked_digests``.
            ArtifactSignatureFailed  — unknown key or bad Ed25519 signature.
            ArtifactBindingMismatch  — signed fields don't match expected config.
        """
        self._check_bundle_path(bundle_path)
        if expected_mode == "live":
            self._enforce_read_only_mount(bundle_path)

        # ── 1. Bundle integrity + Ed25519 signature ──────────────────────────
        verified_bundle = self._verify_bundle_integrity(bundle_path)

        if verified_bundle.artifact_id != expected_artifact_id:
            raise ArtifactVerifierError(
                f"bundle artifact_id {verified_bundle.artifact_id!r} does not match "
                f"expected {expected_artifact_id!r}"
            )

        # ── 2. Reconstruct full attestation from the checksum-validated file ─
        attestation = self._load_attestation(bundle_path)

        # ── 3. State / mode gate (before cryptographic checks so we give a
        #       clear error when an operator accidentally points automation at
        #       a CANDIDATE artifact) ─────────────────────────────────────────
        self._check_state_and_mode(attestation.eligibility_state, expected_mode)

        # ── 4. Expiry, revocation, binding via AttestationVerifier ───────────
        #  expected_account_mode is what the attestation *claims* and what we
        #  independently expect from the mode we were asked to enforce.
        expected_account_mode = (
            "paper" if attestation.permitted_account_mode == "paper" else attestation.permitted_account_mode
        )
        expected_bindings = ExpectedBindings(
            artifact_digest=expected_artifact_id,
            allowlist_digest=attestation.allowlist_digest,
            ruleset_digest=attestation.ruleset_digest,
            account_mode=expected_account_mode,
            max_gross_allocation=attestation.max_gross_allocation,
            permitted_instruments=tuple(attestation.permitted_instruments),
        )
        try:
            verified: VerifiedEligibility = self._verifier.verify(
                attestation=attestation,
                expected=expected_bindings,
                now=now,
                revoked_digests=revoked_digests,
            )
        except UnknownKey as exc:
            raise ArtifactSignatureFailed(str(exc)) from exc
        except BadSignature as exc:
            raise ArtifactSignatureFailed(str(exc)) from exc
        except Expired as exc:
            raise ArtifactExpired(str(exc)) from exc
        except Revoked as exc:
            raise ArtifactRevoked(str(exc)) from exc
        except BindingMismatch as exc:
            raise ArtifactBindingMismatch(str(exc)) from exc
        except AttestationError as exc:
            raise ArtifactVerifierError(f"attestation verification failed: {exc}") from exc

        # ── 5. Extract strategy parameters from the already-checksum-verified
        #       artifact.json ───────────────────────────────────────────────
        parameters = self._load_parameters(bundle_path)

        return VerifiedArtifact(
            artifact_id=expected_artifact_id,
            manifest_digest=verified_bundle.manifest_digest,
            dataset_manifest_digest=verified_bundle.dataset_manifest_digest,
            parameters=parameters,
            allowlist=tuple(verified.permitted_instruments),
            max_gross_allocation=verified.max_gross_allocation,
            expires_at=verified.expires_at,
            public_key_id=verified.public_key_id,
            verification_reason_codes=tuple(attestation.reason_codes),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_bundle_path(self, path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise ArtifactVerifierError(
                f"bundle root must be a real directory, not a symlink or file: {path}"
            )

    def _enforce_read_only_mount(self, path: Path) -> None:
        """Refuse writable bundle mounts in live mode (defence-in-depth)."""
        if os.access(path, os.W_OK):
            raise ArtifactVerifierError(
                "bundle mount must be read-only in live mode"
            )
        for root, _, files in os.walk(path):
            for name in files:
                if os.access(os.path.join(root, name), os.W_OK):
                    raise ArtifactVerifierError(
                        "bundle mount must be read-only in live mode"
                    )

    def _verify_bundle_integrity(self, path: Path):
        """Run bundle checksum + Ed25519 signature verification."""
        service = ResearchBundle(db=None)
        try:
            return service.verify(path, trusted_public_keys=self._keys_by_id)
        except BundleError as exc:
            raise ArtifactVerifierError(f"bundle integrity check failed: {exc}") from exc

    def _load_attestation(self, bundle_path: Path) -> EligibilityAttestation:
        """Parse the full attestation from the already-checksum-validated file."""
        attestation_path = bundle_path / "attestation.json"
        try:
            raw = json.loads(attestation_path.read_bytes())

            def _parse_dt(v: Optional[str]) -> Optional[dt.datetime]:
                if v is None:
                    return None
                parsed = dt.datetime.fromisoformat(v)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=dt.timezone.utc)
                return parsed

            return EligibilityAttestation(
                artifact_digest=raw["artifact_digest"],
                source_digest=raw["source_digest"],
                config_digest=raw["config_digest"],
                dataset_manifest_digest=raw["dataset_manifest_digest"],
                allowlist_digest=raw["allowlist_digest"],
                training_boundary=raw["training_boundary"],
                validation_boundary=raw["validation_boundary"],
                holdout_boundary=raw["holdout_boundary"],
                evidence_boundary=raw["evidence_boundary"],
                cost_assumptions=raw["cost_assumptions"],
                capacity_assumptions=raw["capacity_assumptions"],
                ruleset_name=raw["ruleset_name"],
                ruleset_version=raw["ruleset_version"],
                ruleset_digest=raw["ruleset_digest"],
                eligibility_state=raw["eligibility_state"],
                permitted_account_mode=raw["permitted_account_mode"],
                max_gross_allocation=float(raw["max_gross_allocation"]),
                permitted_instruments=tuple(raw["permitted_instruments"]),
                created_at=_parse_dt(raw["created_at"]),
                expires_at=_parse_dt(raw["expires_at"]),
                operator_approved_at=_parse_dt(raw["operator_approved_at"]),
                reason_codes=tuple(raw.get("reason_codes", ())),
                evidence_refs=tuple(raw.get("evidence_refs", ())),
                eligibility_decision_digest=raw["eligibility_decision_digest"],
                review_digest=raw["review_digest"],
                public_key_id=raw["public_key_id"],
                signature=raw["signature"],
                promoted_at=_parse_dt(raw.get("promoted_at")),
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ArtifactVerifierError(
                f"failed to parse bundle attestation.json: {exc}"
            ) from exc

    def _check_state_and_mode(self, state: str, mode: Literal["paper", "live"]) -> None:
        if state in _FORBIDDEN_STATES:
            raise ArtifactStateForbidden(
                f"artifact in state {state!r} is not eligible to run "
                f"(only PAPER_ELIGIBLE or CANARY_ELIGIBLE artifacts may run)"
            )
        if mode == "live" and state not in _LIVE_STATES:
            raise ArtifactStateForbidden(
                f"live mode requires CANARY_ELIGIBLE; artifact is {state!r}"
            )
        if mode == "paper" and state not in _PAPER_STATES:
            raise ArtifactStateForbidden(
                f"paper mode requires PAPER_ELIGIBLE or CANARY_ELIGIBLE; "
                f"artifact is {state!r}"
            )

    def _load_parameters(self, bundle_path: Path) -> Mapping[str, Any]:
        """Return ``selected_parameters`` from the checksum-validated artifact.json."""
        artifact_path = bundle_path / "artifact.json"
        try:
            artifact = json.loads(artifact_path.read_bytes())
            params = artifact.get("selected_parameters")
            if not isinstance(params, dict):
                raise ArtifactVerifierError(
                    "artifact.json is missing 'selected_parameters'"
                )
            return dict(params)
        except (OSError, ValueError) as exc:
            raise ArtifactVerifierError(
                f"failed to read artifact.json parameters: {exc}"
            ) from exc
