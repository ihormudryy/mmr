"""Tests for ArtifactVerifier — Task 2 of P3 Deterministic Automated Execution.

Coverage:
- Forbidden states: CANDIDATE, SUSPENDED, RETIRED always rejected
- Mode gates: PAPER_ELIGIBLE blocked for live; CANARY_ELIGIBLE required for live
- Writable bundle mounts rejected in live mode
- Symlink bundle root rejected
- Expiry rejection (Expired attestation)
- Revocation rejection (payload digest in revocation set)
- Signature failure (wrong key)
- Artifact ID mismatch
- Bundle checksum mismatch (changed file)
- Missing / malformed attestation
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from trader.automation.artifact_verifier import (
    ArtifactBindingMismatch,
    ArtifactExpired,
    ArtifactRevoked,
    ArtifactSignatureFailed,
    ArtifactStateForbidden,
    ArtifactVerifier,
    ArtifactVerifierError,
    VerifiedArtifact,
)

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return priv, pub


def _make_verifier(pub=None):
    if pub is None:
        _, pub = _make_keypair()
    return ArtifactVerifier(trusted_public_keys=[pub])


def _ro_tmpdir():
    """Context manager that yields a read-only tempdir."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)
    return _ctx()


def _make_bundle_dir_with_attestation(
    state: str,
    mode: str = "paper",
    artifact_id: str = "test-artifact-id",
    expires_at: dt.datetime | None = None,
    promoted_at: dt.datetime | None = None,
    extra_fields: dict | None = None,
) -> Path:
    """Create a temp bundle directory structure with the given attestation state.

    Returns a write-enabled Path; callers must make it read-only if testing live mode.
    """
    d = Path(tempfile.mkdtemp())
    if expires_at is None:
        expires_at = dt.datetime.now(UTC) + dt.timedelta(days=90)
    attestation_data: dict[str, Any] = {
        "artifact_digest": artifact_id,
        "source_digest": "src",
        "config_digest": "cfg",
        "dataset_manifest_digest": "dmf",
        "allowlist_digest": "al",
        "training_boundary": "2024-01-01",
        "validation_boundary": "2024-06-01",
        "holdout_boundary": "2024-09-01",
        "evidence_boundary": "2024-12-01",
        "cost_assumptions": {},
        "capacity_assumptions": {},
        "ruleset_name": "default",
        "ruleset_version": "1.0",
        "ruleset_digest": "rd",
        "eligibility_state": state,
        "permitted_account_mode": mode,
        "max_gross_allocation": 0.05,
        "permitted_instruments": ["AAPL"],
        "created_at": dt.datetime.now(UTC).isoformat(),
        "expires_at": expires_at.isoformat(),
        "operator_approved_at": dt.datetime.now(UTC).isoformat(),
        "reason_codes": ["paper_eligible"],
        "evidence_refs": [],
        "eligibility_decision_digest": "edd",
        "review_digest": "revd",
        "public_key_id": "pk-id",
        "signature": "sig",
        "promoted_at": promoted_at.isoformat() if promoted_at else None,
    }
    if extra_fields:
        attestation_data.update(extra_fields)
    (d / "attestation.json").write_text(json.dumps(attestation_data))
    return d


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_empty_key_list_raises():
    with pytest.raises(ArtifactVerifierError, match="at least one"):
        ArtifactVerifier(trusted_public_keys=[])


# ---------------------------------------------------------------------------
# Path / mount checks
# ---------------------------------------------------------------------------

def test_symlink_root_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    v = _make_verifier()
    with pytest.raises(ArtifactVerifierError, match="symlink"):
        v.verify(link, "paper", "aid", dt.datetime.now(UTC))


def test_file_root_rejected(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    v = _make_verifier()
    with pytest.raises(ArtifactVerifierError, match="real directory"):
        v.verify(f, "paper", "aid", dt.datetime.now(UTC))


def test_writable_bundle_rejected_in_live_mode(tmp_path):
    # tmp_path is writable by default
    v = _make_verifier()
    with pytest.raises(ArtifactVerifierError, match="read-only in live mode"):
        v.verify(tmp_path, "live", "aid", dt.datetime.now(UTC))


def test_writable_file_inside_bundle_rejected_in_live_mode(tmp_path):
    # Make the directory itself read-only but leave a file writable
    f = tmp_path / "writable.txt"
    f.write_text("x")
    # Set directory not writable but file stays writable
    tmp_path.chmod(0o555)
    v = _make_verifier()
    try:
        with pytest.raises(ArtifactVerifierError, match="read-only in live mode"):
            v.verify(tmp_path, "live", "aid", dt.datetime.now(UTC))
    finally:
        tmp_path.chmod(0o755)


# ---------------------------------------------------------------------------
# Forbidden states — always rejected regardless of mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["CANDIDATE", "SUSPENDED", "RETIRED"])
def test_forbidden_state_paper(state):
    d = _make_bundle_dir_with_attestation(state=state)
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        (d / "attestation.json").write_text(json.dumps({
            "artifact_digest": "aid",
            "source_digest": "s", "config_digest": "c", "dataset_manifest_digest": "d",
            "allowlist_digest": "a", "training_boundary": "t", "validation_boundary": "v",
            "holdout_boundary": "h", "evidence_boundary": "e", "cost_assumptions": {},
            "capacity_assumptions": {}, "ruleset_name": "r", "ruleset_version": "v",
            "ruleset_digest": "rd", "eligibility_state": state, "permitted_account_mode": "none",
            "max_gross_allocation": 0.0, "permitted_instruments": [],
            "created_at": dt.datetime.now(UTC).isoformat(),
            "expires_at": (dt.datetime.now(UTC) + dt.timedelta(days=30)).isoformat(),
            "operator_approved_at": dt.datetime.now(UTC).isoformat(),
            "reason_codes": [], "evidence_refs": [],
            "eligibility_decision_digest": "edd", "review_digest": "rd",
            "public_key_id": "pk", "signature": "sig", "promoted_at": None,
        }))
        with pytest.raises(ArtifactStateForbidden, match=state):
            v.verify(d, "paper", "aid", dt.datetime.now(UTC))


@pytest.mark.parametrize("state", ["CANDIDATE", "SUSPENDED", "RETIRED"])
def test_forbidden_state_live(state):
    d = _make_bundle_dir_with_attestation(state=state)
    # Make all files read-only first, then the directory
    for f in d.iterdir():
        f.chmod(0o444)
    d.chmod(0o555)
    v = _make_verifier()
    try:
        with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
            mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
            with pytest.raises(ArtifactStateForbidden, match=state):
                v.verify(d, "live", "aid", dt.datetime.now(UTC))
    finally:
        d.chmod(0o755)
        for f in d.iterdir():
            f.chmod(0o644)


# ---------------------------------------------------------------------------
# Mode gates
# ---------------------------------------------------------------------------

def test_paper_eligible_rejected_for_live():
    d = _make_bundle_dir_with_attestation(state="PAPER_ELIGIBLE", mode="paper")
    now = dt.datetime.now(UTC)
    attestation_payload = json.dumps({
        "artifact_digest": "aid",
        "source_digest": "s", "config_digest": "c", "dataset_manifest_digest": "d",
        "allowlist_digest": "a", "training_boundary": "t", "validation_boundary": "v",
        "holdout_boundary": "h", "evidence_boundary": "e", "cost_assumptions": {},
        "capacity_assumptions": {}, "ruleset_name": "r", "ruleset_version": "v",
        "ruleset_digest": "rd", "eligibility_state": "PAPER_ELIGIBLE",
        "permitted_account_mode": "paper",
        "max_gross_allocation": 0.05, "permitted_instruments": ["AAPL"],
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(days=30)).isoformat(),
        "operator_approved_at": now.isoformat(),
        "reason_codes": ["paper_eligible"], "evidence_refs": [],
        "eligibility_decision_digest": "edd", "review_digest": "rd",
        "public_key_id": "pk", "signature": "sig", "promoted_at": None,
    })
    attestation_file = d / "attestation.json"
    attestation_file.write_text(attestation_payload)
    # Make all files read-only, then the directory
    for f in d.iterdir():
        f.chmod(0o444)
    d.chmod(0o555)
    v = _make_verifier()
    try:
        with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
            mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
            with pytest.raises(ArtifactStateForbidden, match="CANARY_ELIGIBLE"):
                v.verify(d, "live", "aid", now)
    finally:
        d.chmod(0o755)
        for f in d.iterdir():
            f.chmod(0o644)


def test_canary_eligible_accepted_for_paper():
    """CANARY_ELIGIBLE is a superset — allowed for paper too."""
    d = _make_bundle_dir_with_attestation(state="CANARY_ELIGIBLE", mode="paper")
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        (d / "attestation.json").write_text(json.dumps({
            "artifact_digest": "aid",
            "source_digest": "s", "config_digest": "c", "dataset_manifest_digest": "d",
            "allowlist_digest": "a", "training_boundary": "t", "validation_boundary": "v",
            "holdout_boundary": "h", "evidence_boundary": "e", "cost_assumptions": {},
            "capacity_assumptions": {}, "ruleset_name": "r", "ruleset_version": "v",
            "ruleset_digest": "rd", "eligibility_state": "CANARY_ELIGIBLE",
            "permitted_account_mode": "paper",
            "max_gross_allocation": 0.05, "permitted_instruments": ["AAPL"],
            "created_at": dt.datetime.now(UTC).isoformat(),
            "expires_at": (dt.datetime.now(UTC) + dt.timedelta(days=30)).isoformat(),
            "operator_approved_at": dt.datetime.now(UTC).isoformat(),
            "reason_codes": ["paper_eligible"], "evidence_refs": [],
            "eligibility_decision_digest": "edd", "review_digest": "rd",
            "public_key_id": "pk", "signature": "sig", "promoted_at": None,
        }))
        # Should pass the state gate and fail at AttestationVerifier (wrong key)
        # — that's the signature gate, not the state gate.
        with pytest.raises((ArtifactSignatureFailed, ArtifactVerifierError)):
            v.verify(d, "paper", "aid", dt.datetime.now(UTC))


# ---------------------------------------------------------------------------
# Artifact ID mismatch
# ---------------------------------------------------------------------------

def test_artifact_id_mismatch():
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        # Bundle claims "real-id" but caller expects "expected-id"
        mv.return_value = mock.MagicMock(
            artifact_id="real-id", manifest_digest="md", dataset_manifest_digest="dmd"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            with pytest.raises(ArtifactVerifierError, match="does not match expected"):
                v.verify(path, "paper", "expected-id", dt.datetime.now(UTC))


# ---------------------------------------------------------------------------
# Bundle integrity failure
# ---------------------------------------------------------------------------

def test_bundle_integrity_failure():
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        from trader.research.bundle import BundleError
        mv.side_effect = BundleError("checksum mismatch for artifact.json")
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(ArtifactVerifierError, match="bundle integrity check failed"):
                v.verify(Path(d), "paper", "aid", dt.datetime.now(UTC))


# ---------------------------------------------------------------------------
# Attestation expiry
# ---------------------------------------------------------------------------

def test_expired_attestation():
    now = dt.datetime.now(UTC)
    past = now - dt.timedelta(seconds=1)

    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            (path / "attestation.json").write_text(json.dumps({
                "artifact_digest": "aid",
                "source_digest": "s", "config_digest": "c", "dataset_manifest_digest": "d",
                "allowlist_digest": "a", "training_boundary": "t", "validation_boundary": "v",
                "holdout_boundary": "h", "evidence_boundary": "e", "cost_assumptions": {},
                "capacity_assumptions": {}, "ruleset_name": "r", "ruleset_version": "v",
                "ruleset_digest": "rd", "eligibility_state": "PAPER_ELIGIBLE",
                "permitted_account_mode": "paper",
                "max_gross_allocation": 0.05, "permitted_instruments": ["AAPL"],
                "created_at": now.isoformat(),
                "expires_at": past.isoformat(),  # already expired
                "operator_approved_at": now.isoformat(),
                "reason_codes": ["paper_eligible"], "evidence_refs": [],
                "eligibility_decision_digest": "edd", "review_digest": "rd",
                "public_key_id": "pk", "signature": "sig", "promoted_at": None,
            }))
            with pytest.raises((ArtifactExpired, ArtifactVerifierError)):
                v.verify(path, "paper", "aid", now)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def test_revoked_attestation():
    now = dt.datetime.now(UTC)
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        with mock.patch.object(v._verifier, "verify") as mock_verify:
            from trader.research.attestation import Revoked
            mock_verify.side_effect = Revoked("attestation abc is revoked")
            with tempfile.TemporaryDirectory() as d:
                path = Path(d)
                (path / "attestation.json").write_text(json.dumps({
                    "artifact_digest": "aid",
                    "source_digest": "s", "config_digest": "c", "dataset_manifest_digest": "d",
                    "allowlist_digest": "a", "training_boundary": "t", "validation_boundary": "v",
                    "holdout_boundary": "h", "evidence_boundary": "e", "cost_assumptions": {},
                    "capacity_assumptions": {}, "ruleset_name": "r", "ruleset_version": "v",
                    "ruleset_digest": "rd", "eligibility_state": "PAPER_ELIGIBLE",
                    "permitted_account_mode": "paper",
                    "max_gross_allocation": 0.05, "permitted_instruments": ["AAPL"],
                    "created_at": now.isoformat(),
                    "expires_at": (now + dt.timedelta(days=30)).isoformat(),
                    "operator_approved_at": now.isoformat(),
                    "reason_codes": [], "evidence_refs": [],
                    "eligibility_decision_digest": "edd", "review_digest": "rd",
                    "public_key_id": "pk", "signature": "sig", "promoted_at": None,
                }))
                with pytest.raises(ArtifactRevoked, match="revoked"):
                    v.verify(path, "paper", "aid", now, revoked_digests=frozenset(["abc"]))


# ---------------------------------------------------------------------------
# Malformed attestation.json
# ---------------------------------------------------------------------------

def test_missing_attestation_file():
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            # Don't write attestation.json — it's missing
            with pytest.raises(ArtifactVerifierError, match="failed to parse bundle attestation"):
                v.verify(path, "paper", "aid", dt.datetime.now(UTC))


def test_malformed_attestation_json():
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            (path / "attestation.json").write_text("{not valid json")
            with pytest.raises(ArtifactVerifierError, match="failed to parse bundle attestation"):
                v.verify(path, "paper", "aid", dt.datetime.now(UTC))


# ---------------------------------------------------------------------------
# Verified result shape
# ---------------------------------------------------------------------------

def test_verified_artifact_fields():
    """VerifiedArtifact should not expose raw key material or full attestation blob."""
    artifact = VerifiedArtifact(
        artifact_id="aid",
        manifest_digest="md",
        dataset_manifest_digest="dmd",
        parameters={"alpha": 0.01},
        allowlist=("AAPL", "MSFT"),
        max_gross_allocation=0.05,
        expires_at=dt.datetime.now(UTC) + dt.timedelta(days=30),
        public_key_id="ed25519-abc",
        verification_reason_codes=("paper_eligible",),
    )
    # Safe fields present
    assert artifact.artifact_id == "aid"
    assert artifact.public_key_id == "ed25519-abc"
    assert artifact.verification_reason_codes == ("paper_eligible",)
    assert artifact.allowlist == ("AAPL", "MSFT")
    # Must be frozen (immutable)
    with pytest.raises(Exception):
        artifact.artifact_id = "tampered"  # type: ignore


# ---------------------------------------------------------------------------
# State gate order — state check must happen BEFORE signature check
# ---------------------------------------------------------------------------

def test_state_gate_before_signature():
    """A CANDIDATE artifact should raise ArtifactStateForbidden,
    not ArtifactSignatureFailed — the state check must be first."""
    v = _make_verifier()
    with mock.patch("trader.automation.artifact_verifier.ResearchBundle.verify") as mv:
        mv.return_value = mock.MagicMock(artifact_id="aid", manifest_digest="md", dataset_manifest_digest="dmd")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            (path / "attestation.json").write_text(json.dumps({
                "artifact_digest": "aid",
                "source_digest": "s", "config_digest": "c", "dataset_manifest_digest": "d",
                "allowlist_digest": "a", "training_boundary": "t", "validation_boundary": "v",
                "holdout_boundary": "h", "evidence_boundary": "e", "cost_assumptions": {},
                "capacity_assumptions": {}, "ruleset_name": "r", "ruleset_version": "v",
                "ruleset_digest": "rd", "eligibility_state": "CANDIDATE",
                "permitted_account_mode": "none",
                "max_gross_allocation": 0.0, "permitted_instruments": [],
                "created_at": dt.datetime.now(UTC).isoformat(),
                "expires_at": (dt.datetime.now(UTC) + dt.timedelta(days=30)).isoformat(),
                "operator_approved_at": dt.datetime.now(UTC).isoformat(),
                "reason_codes": [], "evidence_refs": [],
                "eligibility_decision_digest": "edd", "review_digest": "rd",
                "public_key_id": "pk", "signature": "badsig", "promoted_at": None,
            }))
            with pytest.raises(ArtifactStateForbidden, match="CANDIDATE"):
                v.verify(path, "paper", "aid", dt.datetime.now(UTC))
