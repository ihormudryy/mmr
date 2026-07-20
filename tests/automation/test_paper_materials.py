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


def test_export_fixture_bundle_is_paper_eligible(tmp_path: Path) -> None:
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
    )

    bundle_path = artifacts_root / artifact_id
    assert (bundle_path / "attestation.json").is_file()
    assert (bundle_path / "manifest.json").is_file()

    attestation = json.loads((bundle_path / "attestation.json").read_text())
    assert attestation["eligibility_state"] == "PAPER_ELIGIBLE"


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
    )
    second_id = export_fixture_paper_eligible_bundle(
        signer=signer,
        artifacts_root=artifacts_root,
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
    )
    export_dir = artifacts_root / artifact_id
    os.chmod(export_dir, 0o755)
    attestation_path = export_dir / "attestation.json"
    os.chmod(attestation_path, 0o644)
    attestation_path.write_text(
        json.dumps({"public_key_id": "wrong", "eligibility_state": "PAPER_ELIGIBLE"}),
        encoding="utf-8",
    )

    with pytest.raises(PaperMaterialsError, match="not a valid PAPER_ELIGIBLE bundle"):
        export_fixture_paper_eligible_bundle(
            signer=signer,
            artifacts_root=artifacts_root,
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
            )

    orphan_dirs = list(artifacts_root.iterdir()) if artifacts_root.exists() else []
    assert orphan_dirs == []
