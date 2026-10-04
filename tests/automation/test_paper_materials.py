"""Tests for shared paper automation keygen + fixture bundle export."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from trader.automation.paper_materials import (
    PaperMaterialsError,
    default_key_paths,
    ensure_signing_keypair,
    export_fixture_paper_eligible_bundle,
    verify_qualified_paper_bundle,
)


@pytest.mark.parametrize("bundle_kwargs", [
    {"reviewer": "bootstrap"},
    {"evidence_kind": "offline_fixture"},
])
def test_qualified_bundle_rejects_signed_fixture_provenance(tmp_path, bundle_kwargs):
    from .paper_evidence_helpers import NOW, research_bundle

    bundle_path, key_ring = research_bundle(tmp_path, **bundle_kwargs)
    with pytest.raises(PaperMaterialsError, match="fixture"):
        verify_qualified_paper_bundle(
            bundle_path=bundle_path, public_key_ring_path=key_ring,
            expected_artifact_id=bundle_path.name, now=NOW,
        )


def test_qualified_bundle_rejects_signed_but_empty_quantitative_decision(tmp_path):
    from .paper_evidence_helpers import NOW, research_bundle
    from trader.research.eligibility import EligibilityDecision
    from trader.research.rulesets.paper_v1 import PAPER_V1

    decision = EligibilityDecision(
        state="PAPER_ELIGIBLE", ruleset_name=PAPER_V1.name,
        ruleset_version=PAPER_V1.version, ruleset_digest=PAPER_V1.digest,
        passed=True, results=(),
    )
    bundle_path, key_ring = research_bundle(tmp_path, decision=decision)
    with pytest.raises(PaperMaterialsError, match="quantitative"):
        verify_qualified_paper_bundle(
            bundle_path=bundle_path, public_key_ring_path=key_ring,
            expected_artifact_id=bundle_path.name, now=NOW,
        )


def test_ensure_signing_keypair_writes_0600_and_reuses(tmp_path: Path) -> None:
    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"

    signer1, reused1 = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    assert reused1 is False
    assert private_path.is_file()
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert public_path.is_file()
    assert stat.S_IMODE(public_path.stat().st_mode) == 0o644

    signer2, reused2 = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    assert reused2 is True
    assert signer2.public_key_id == signer1.public_key_id


def test_fixture_export_requires_explicit_offline_opt_in(tmp_path):
    from trader.research.signing import AttestationSigner

    with pytest.raises(PaperMaterialsError, match="offline_fixture=True"):
        export_fixture_paper_eligible_bundle(
            signer=AttestationSigner.generate(), artifacts_root=tmp_path / "artifacts",
        )
    assert not (tmp_path / "artifacts").exists()


def test_offline_fixture_never_authorizes_paper_or_promotion(tmp_path):
    from trader.research.signing import AttestationSigner
    from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
    from .paper_evidence_helpers import NOW

    signer = AttestationSigner.generate()
    artifact_id = export_fixture_paper_eligible_bundle(
        signer=signer, artifacts_root=tmp_path, offline_fixture=True,
    )
    bundle_path = tmp_path / artifact_id
    attestation = json.loads((bundle_path / "attestation.json").read_text())
    family = json.loads((bundle_path / "family.json").read_text())
    assert attestation["eligibility_state"] == "CANDIDATE"
    assert attestation["permitted_account_mode"] == "none"
    assert "offline_fixture_not_for_promotion" in attestation["reason_codes"]
    assert family["validation_protocol"]["evidence_kind"] == "offline_fixture"
    for mode in ("paper", "live"):
        with pytest.raises(ArtifactVerifierError):
            ArtifactVerifier([signer.public_key]).verify(
                bundle_path, expected_mode=mode, expected_artifact_id=artifact_id, now=NOW,
            )


def test_export_fixture_bundle_is_candidate(tmp_path: Path) -> None:
    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"
    signer, _ = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )

    artifacts_root = tmp_path / "artifacts"
    artifact_id = export_fixture_paper_eligible_bundle(
        signer=signer,
        artifacts_root=artifacts_root,
        offline_fixture=True,
    )

    bundle_path = artifacts_root / artifact_id
    assert (bundle_path / "attestation.json").is_file()
    assert (bundle_path / "manifest.json").is_file()

    attestation = json.loads((bundle_path / "attestation.json").read_text())
    assert attestation["eligibility_state"] == "CANDIDATE"


def test_default_key_paths(tmp_path: Path) -> None:
    private_path, verify_dir, public_path = default_key_paths(tmp_path)
    assert private_path == tmp_path / "keys" / "private" / "signing.pem"
    assert verify_dir == tmp_path / "keys" / "verify"
    assert public_path == tmp_path / "keys" / "verify" / "paper-automation.pem"


def test_ensure_signing_keypair_rejects_mismatched_public_key(tmp_path: Path) -> None:
    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"

    ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    public_path.write_bytes(b"-----BEGIN PUBLIC KEY-----\nwrong\n-----END PUBLIC KEY-----\n")

    with pytest.raises(PaperMaterialsError, match="does not match private key"):
        ensure_signing_keypair(
            private_key_path=private_path,
            public_key_path=public_path,
        )


def test_ensure_signing_keypair_force_regenerates_mismatched_pair(tmp_path: Path) -> None:
    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"

    signer1, _ = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    public_path.write_bytes(b"-----BEGIN PUBLIC KEY-----\nwrong\n-----END PUBLIC KEY-----\n")

    signer2, reused = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
        force=True,
    )
    assert reused is False
    assert signer2.public_key_id != signer1.public_key_id
    assert public_path.read_bytes() == signer2.public_key_pem()


def test_export_fixture_bundle_reuses_existing_valid_bundle(tmp_path: Path) -> None:
    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"
    signer, _ = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    artifacts_root = tmp_path / "artifacts"

    first_id = export_fixture_paper_eligible_bundle(
        signer=signer,
        artifacts_root=artifacts_root,
        offline_fixture=True,
    )
    second_id = export_fixture_paper_eligible_bundle(
        signer=signer,
        artifacts_root=artifacts_root,
        offline_fixture=True,
    )

    assert first_id == second_id
    assert len(list(artifacts_root.iterdir())) == 1


def test_export_fixture_bundle_rejects_invalid_existing_dir(tmp_path: Path) -> None:
    import os

    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"
    signer, _ = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    artifacts_root = tmp_path / "artifacts"
    artifact_id = export_fixture_paper_eligible_bundle(
        signer=signer,
        artifacts_root=artifacts_root,
        offline_fixture=True,
    )
    export_dir = artifacts_root / artifact_id
    os.chmod(export_dir, 0o755)
    attestation_path = export_dir / "attestation.json"
    os.chmod(attestation_path, 0o644)
    attestation_path.write_text(
        json.dumps({"public_key_id": "wrong", "eligibility_state": "PAPER_ELIGIBLE"}),
        encoding="utf-8",
    )

    with pytest.raises(PaperMaterialsError, match="not a valid offline fixture bundle"):
        export_fixture_paper_eligible_bundle(
            signer=signer,
            artifacts_root=artifacts_root,
            offline_fixture=True,
        )


def test_export_fixture_bundle_cleans_up_orphan_dir_on_export_failure(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "keys" / "private" / "signing.pem"
    public_path = tmp_path / "keys" / "verify" / "paper-automation.pem"
    signer, _ = ensure_signing_keypair(
        private_key_path=private_path,
        public_key_path=public_path,
    )
    artifacts_root = tmp_path / "artifacts"

    def _export_fail_after_mkdir(_self, artifact_id: str, path: Path) -> None:
        path.mkdir(parents=True)
        raise RuntimeError("simulated export failure")

    with patch(
        "trader.automation.paper_materials.ResearchBundle.export",
        _export_fail_after_mkdir,
    ):
        with pytest.raises(RuntimeError, match="simulated export failure"):
            export_fixture_paper_eligible_bundle(
                signer=signer,
                artifacts_root=artifacts_root,
                offline_fixture=True,
            )

    orphan_dirs = list(artifacts_root.iterdir()) if artifacts_root.exists() else []
    assert orphan_dirs == []
