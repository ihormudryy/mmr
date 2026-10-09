import datetime as dt
import json
import threading

import pytest

from tests.research.evaluation_fixtures import FIXED_NOW, export_eligible_bundle, holdout_ruleset
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.research import attest_export
from trader.research.attest_export import (
    ATTESTATION_LIFETIME, AttestExportError, attest_and_export, bundle_dir_name,
)
from trader.research.bundle import ResearchBundle
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.signing import AttestationSigner
from tests.research.test_research_bundle import build_populated_db


@pytest.fixture(scope='module')
def exported(tmp_path_factory):
    repo = tmp_path_factory.mktemp('attest')
    return export_eligible_bundle(repo, str(repo / 'market.duckdb'))


@pytest.mark.timeout(240)
def test_bundle_lands_under_its_manifest_digest(exported):
    manifest = json.loads((exported.bundle_path / 'manifest.json').read_text())
    assert exported.bundle_path.name == bundle_dir_name(manifest['manifest_digest'])


@pytest.mark.timeout(240)
def test_bundle_verifies_for_research_and_for_the_runtime(exported):
    keys = {exported.signer.public_key_id: exported.signer.public_key}
    ResearchBundle(exported.research_db).verify(exported.bundle_path, trusted_public_keys=keys)
    verified = ArtifactVerifier([exported.signer.public_key]).verify(
        exported.bundle_path, 'paper', exported.artifact_id, FIXED_NOW)
    assert set(verified.allowlist) == {str(c) for c in exported.spec.conids}


@pytest.mark.timeout(240)
def test_attesting_twice_reuses_the_attestation_and_the_directory(exported):
    again = attest_and_export(exported.research_db, artifact_id=exported.artifact_id,
                              signer=exported.signer, artifacts_root=exported.bundle_path.parent,
                              now=FIXED_NOW, ruleset=holdout_ruleset())
    assert again == exported.bundle_path
    assert sorted(p.name for p in exported.bundle_path.parent.glob('sha256_*')) == [again.name]


@pytest.mark.timeout(240)
def test_unknown_artifact_is_refused(exported, tmp_path):
    with pytest.raises(AttestExportError, match='unknown artifact'):
        attest_and_export(exported.research_db, artifact_id='nope',
                          signer=AttestationSigner.generate(), artifacts_root=tmp_path / 'x',
                          now=FIXED_NOW)


@pytest.mark.timeout(240)
def test_a_decision_made_under_a_weaker_ruleset_is_refused(exported, tmp_path):
    with pytest.raises(AttestExportError, match='ruleset'):
        attest_and_export(exported.research_db, artifact_id=exported.artifact_id,
                          signer=exported.signer, artifacts_root=tmp_path / 'x', now=FIXED_NOW)


def test_an_artifact_whose_holdout_failed_is_refused_even_with_an_eligible_decision(tmp_path):
    db, artifact_id, signer = build_populated_db(tmp_path, return_signer=True,
                                                 holdout_passed=False)
    with pytest.raises(AttestExportError, match='holdout'):
        attest_and_export(db, artifact_id=artifact_id, signer=signer,
                          artifacts_root=tmp_path / 'artifacts', now=FIXED_NOW, ruleset=PAPER_V1)
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.timeout(240)
def test_an_expired_attestation_is_not_exported_again(exported, tmp_path):
    expired = FIXED_NOW + ATTESTATION_LIFETIME + dt.timedelta(seconds=1)
    with pytest.raises(AttestExportError, match='expired') as refusal:
        attest_and_export(exported.research_db, artifact_id=exported.artifact_id,
                          signer=exported.signer, artifacts_root=tmp_path / 'x', now=expired,
                          ruleset=holdout_ruleset())
    assert 'evaluating again over a newer period' in str(refusal.value)
    assert not (tmp_path / 'x').exists()


@pytest.mark.timeout(240)
def test_an_attestation_signed_by_another_key_is_not_exported_again(exported, tmp_path):
    other = AttestationSigner.generate()
    with pytest.raises(AttestExportError, match=exported.signer.public_key_id) as refusal:
        attest_and_export(exported.research_db, artifact_id=exported.artifact_id,
                          signer=other, artifacts_root=tmp_path / 'x', now=FIXED_NOW,
                          ruleset=holdout_ruleset())
    assert other.public_key_id in str(refusal.value)
    assert 'evaluating again over a newer period' in str(refusal.value)
    assert not (tmp_path / 'x').exists()


@pytest.mark.timeout(240)
def test_two_instances_attesting_one_decision_store_one_attestation(exported, tmp_path):
    barrier = threading.Barrier(2, timeout=30)
    real = attest_export._attestation

    def meet_before_recording(*args, **kwargs):
        signed = real(*args, **kwargs)
        barrier.wait()                          # both have seen "no attestation yet"
        return signed
    db = exported.research_db
    db.execute("DELETE FROM eligibility_attestations")
    results, errors = [], []

    def attest(offset):
        try:
            results.append(attest_and_export(db, artifact_id=exported.artifact_id, signer=exported.signer,
                                             artifacts_root=tmp_path / 'x',
                                             now=FIXED_NOW + dt.timedelta(seconds=offset),
                                             ruleset=holdout_ruleset()))
        except Exception as exc:                # surfaced below
            errors.append(exc)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(attest_export, '_attestation', meet_before_recording)
        threads = [threading.Thread(target=attest, args=(offset,)) for offset in (0, 1)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert errors == []
    assert db.execute('SELECT count(*) FROM eligibility_attestations', fetch='one')[0] == 1
    assert len(set(results)) == 1
