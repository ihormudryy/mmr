"""SP2c Plan 2 Task 4: the trader verifies a bundle with public keys only (spec 5.2 item 4, 9 tampering)."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil

import pytest

from tests.automation.judged_bundle import export_judged_bundle
from tests.research.evaluation_fixtures import FIXED_NOW, judge_qualified_evidence_by_holdout_ruleset
from trader.automation.ai_bundle_check import BundleRefused, ResearchBundleCheck, _conids
from trader.research.signing import AttestationSigner

LATER = FIXED_NOW + dt.timedelta(days=1)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    repo = tmp_path_factory.mktemp("judged")
    return export_judged_bundle(repo, str(repo / "market.duckdb"), judgment_id="jdg-1")


@pytest.fixture
def qualified(monkeypatch):
    judge_qualified_evidence_by_holdout_ruleset(monkeypatch)


def check(bundle, root=None, verify=None):
    return ResearchBundleCheck(artifacts_root=root or bundle.bundle_path.parent, verify_dir=verify or bundle.verify_dir)


@pytest.mark.timeout(240)
def test_a_verified_bundle_gives_its_binding(bundle, qualified):
    facts = check(bundle).check(bundle.bundle_digest, artifact_id=bundle.artifact_id, now=LATER)
    assert facts.reviewer == "openrouter/jev#jdg-1" and facts.reviewer_kind == "llm"
    assert facts.conids == tuple(sorted(bundle.spec.conids)) and facts.bar_size == "15 mins"
    assert facts.file_hash.startswith("sha256:") and facts.strategy_path == "strategies/time_of_day.py"
    assert check(bundle).manifest_artifact_id(bundle.bundle_digest) == bundle.artifact_id


def _copy(bundle, tmp_path):
    root = tmp_path / "artifacts"
    shutil.copytree(bundle.bundle_path, root / bundle.bundle_path.name)
    return root, root / bundle.bundle_path.name


def _rewrite_read_only(file, text):
    os.chmod(file, 0o644)
    file.write_text(text)
    os.chmod(file, 0o444)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("case,code", [("params", "BUNDLE_INVALID"), ("expired", "BUNDLE_EXPIRED"),
                                       ("unknown_key", "BUNDLE_INVALID"), ("missing", "BUNDLE_MISSING"),
                                       ("no_keys", "BUNDLE_KEYS_MISSING"), ("unqualified", "BUNDLE_NOT_QUALIFIED")])
def test_tampered_expired_or_unknown_key_bundles_are_refused(bundle, qualified, tmp_path, case, code, monkeypatch):
    root, target = _copy(bundle, tmp_path)
    verify, now = bundle.verify_dir, LATER
    if case == "params":
        artifact = json.loads((target / "artifact.json").read_text())
        artifact["selected_parameters"]["ENTRY_MINUTE"] = 601
        _rewrite_read_only(target / "artifact.json", json.dumps(artifact))
    elif case == "expired":
        now = FIXED_NOW + dt.timedelta(days=91)
    elif case == "unknown_key":
        verify = tmp_path / "other"
        verify.mkdir()
        (verify / "x.pem").write_bytes(AttestationSigner.generate().public_key_pem())
    elif case == "missing":
        os.chmod(target, 0o755)
        shutil.rmtree(target)
    elif case == "no_keys":
        verify = tmp_path / "empty"
        verify.mkdir()
    elif case == "unqualified":
        monkeypatch.undo()                      # the full paper-v1 rule set: the synthetic bundle fails it
    with pytest.raises(BundleRefused) as refused:
        check(bundle, root, verify).check(bundle.bundle_digest, artifact_id=bundle.artifact_id, now=now)
    assert refused.value.code == code, refused.value.message


@pytest.mark.parametrize("digest", ["", "sha256:abc", "../../etc", None, 7, "sha256:" + "a" * 64 + "\n"])
def test_a_malformed_digest_is_refused_without_touching_the_disk(bundle, digest):
    with pytest.raises(BundleRefused) as refused:
        check(bundle).check(digest, artifact_id=bundle.artifact_id, now=LATER)
    assert refused.value.code == "BUNDLE_INVALID"


@pytest.mark.parametrize("instrument", ["\u0662\u0666\u0665\u0665\u0669\u0668", "0265598", "265598\n", "+265598", ""])
def test_an_attested_instrument_must_be_an_ascii_conid(instrument):
    with pytest.raises(BundleRefused) as refused:
        _conids(["272093", instrument])
    assert refused.value.code == "BUNDLE_INVALID"
    assert _conids(["272093", "265598"]) == (265598, 272093)


def test_a_garbage_key_file_is_refused_as_missing_keys(bundle, tmp_path):
    (tmp_path / "bad.pem").write_text("not a key")
    with pytest.raises(BundleRefused) as refused:
        check(bundle, verify=tmp_path).check(bundle.bundle_digest, artifact_id=bundle.artifact_id, now=LATER)
    assert refused.value.code == "BUNDLE_KEYS_MISSING"


def test_a_digest_that_names_another_bundle_is_not_found(bundle):
    other = "sha256:" + "0" * 64
    with pytest.raises(BundleRefused) as refused:
        check(bundle).check(other, artifact_id=bundle.artifact_id, now=LATER)
    assert refused.value.code == "BUNDLE_MISSING"
    with pytest.raises(BundleRefused) as manifest_refused:
        check(bundle).manifest_artifact_id(other)
    assert manifest_refused.value.code == "BUNDLE_MISSING"
