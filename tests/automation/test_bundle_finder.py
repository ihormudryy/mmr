import shutil

import pytest

from tests.automation.fixture_bundle import export_fixture_paper_eligible_bundle
from tests.research.evaluation_fixtures import (
    FIXED_NOW,
    export_eligible_bundle,
    judge_qualified_evidence_by_holdout_ruleset,
)
from trader.automation.artifact_verifier import ArtifactVerifier
from trader.automation.bundle_finder import NoEligibleBundle, find_eligible_bundle


@pytest.fixture(autouse=True)
def _synthetic_ruleset(monkeypatch):
    judge_qualified_evidence_by_holdout_ruleset(monkeypatch)


@pytest.fixture(scope='module')
def repo(tmp_path_factory):
    return tmp_path_factory.mktemp('finder')


@pytest.fixture(scope='module')
def exported(repo):
    return export_eligible_bundle(repo, str(repo / 'market.duckdb'))


def _strategy(exported, **overrides):
    entry = {'name': 'time_of_day', 'module': 'strategies/time_of_day.py',
             'class_name': 'TimeOfDay', 'bar_size': '15 mins',
             'conids': list(exported.spec.conids),
             'params': {'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 660}}
    entry.update(overrides)
    return entry


def _find(exported, repo, strategy, artifacts_root=None):
    return find_eligible_bundle(
        artifacts_root=artifacts_root or exported.bundle_path.parent,
        verifier=ArtifactVerifier([exported.signer.public_key]), strategy=strategy,
        strategy_file=repo / 'strategies' / 'time_of_day.py', now=FIXED_NOW)


@pytest.mark.timeout(240)
def test_finds_the_bound_bundle(exported, repo):
    found = _find(exported, repo, _strategy(exported))
    assert found.path == exported.bundle_path and found.artifact_id == exported.artifact_id


@pytest.mark.timeout(240)
def test_yaml_changed_after_attesting_is_refused(exported, repo):
    with pytest.raises(NoEligibleBundle) as exc:
        _find(exported, repo, _strategy(exported, params={'ENTRY_MINUTE': 615, 'EXIT_MINUTE': 660}))
    assert any('StrategyBindingError: params' in reason for reason in exc.value.reasons)


@pytest.mark.timeout(240)
def test_unbound_bundles_next_to_the_bound_one_are_skipped(exported, repo, tmp_path):
    artifacts_root = tmp_path / 'artifacts'
    shutil.copytree(exported.bundle_path.parent, artifacts_root)
    fixture_id = export_fixture_paper_eligible_bundle(
        signer=exported.signer, artifacts_root=tmp_path / 'fx')
    shutil.copytree(tmp_path / 'fx' / fixture_id, artifacts_root / 'sha256_fixture')

    found = _find(exported, repo, _strategy(exported), artifacts_root=artifacts_root)

    assert found.path == artifacts_root / exported.bundle_path.name


@pytest.mark.timeout(240)
def test_empty_artifacts_root_is_refused(exported, repo, tmp_path):
    with pytest.raises(NoEligibleBundle):
        _find(exported, repo, _strategy(exported), artifacts_root=tmp_path / 'none')


@pytest.mark.timeout(240)
@pytest.mark.parametrize('manifest_text', ['["artifact_id"]', '"artifact_id"', 'not json', '{}'])
def test_malformed_manifest_is_skipped_next_to_the_bound_bundle(exported, repo, tmp_path, manifest_text):
    artifacts_root = tmp_path / 'artifacts'
    shutil.copytree(exported.bundle_path.parent, artifacts_root)
    malformed = artifacts_root / 'sha256_malformed'
    malformed.mkdir()
    (malformed / 'manifest.json').write_text(manifest_text)

    found = _find(exported, repo, _strategy(exported), artifacts_root=artifacts_root)

    assert found.path == artifacts_root / exported.bundle_path.name


@pytest.mark.timeout(240)
def test_malformed_manifest_alone_is_refused_with_its_reason(exported, repo, tmp_path):
    artifacts_root = tmp_path / 'artifacts'
    (artifacts_root / 'sha256_malformed').mkdir(parents=True)
    (artifacts_root / 'sha256_malformed' / 'manifest.json').write_text('["artifact_id"]')

    with pytest.raises(NoEligibleBundle) as exc:
        _find(exported, repo, _strategy(exported), artifacts_root=artifacts_root)

    assert any(reason.startswith('sha256_malformed: ValueError: ') for reason in exc.value.reasons)


@pytest.mark.timeout(240)
def test_signed_fixture_provenance_is_refused_with_its_reason(exported, repo, tmp_path):
    artifacts_root = tmp_path / 'artifacts'
    fixture_id = export_fixture_paper_eligible_bundle(
        signer=exported.signer, artifacts_root=tmp_path / 'fx')
    shutil.copytree(tmp_path / 'fx' / fixture_id, artifacts_root / 'sha256_fixture')

    with pytest.raises(NoEligibleBundle) as exc:
        _find(exported, repo, _strategy(exported), artifacts_root=artifacts_root)

    assert any(reason.startswith('sha256_fixture: PaperMaterialsError: ') and 'fixture' in reason
               for reason in exc.value.reasons)


@pytest.mark.timeout(240)
def test_bundle_judged_by_a_reduced_ruleset_is_refused(exported, repo, monkeypatch):
    monkeypatch.undo()  # the real gate demands every paper-v1 rule

    with pytest.raises(NoEligibleBundle) as exc:
        _find(exported, repo, _strategy(exported))

    assert any('PaperMaterialsError' in reason and 'paper-v1' in reason
               for reason in exc.value.reasons)
