"""Tests for shared paper automation keygen + fixture bundle export."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from trader.automation.paper_materials import (
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
