import datetime as dt
import json
import shutil

import pytest

from scripts.bootstrap_paper_automation import main
from tests.automation.fixture_bundle import export_fixture_paper_eligible_bundle
from trader.automation.paper_materials import default_key_paths, ensure_signing_keypair
from trader.research.signing import AttestationSigner


def _attest_with_new_key(config_dir, artifacts_root):
    """What `mmr research attest bundle` does: create the key pair, sign a bundle."""
    private_pem, _, public_pem = default_key_paths(config_dir)
    signer, _ = ensure_signing_keypair(private_key_path=private_pem, public_key_path=public_pem)
    artifact_id = export_fixture_paper_eligible_bundle(signer=signer, artifacts_root=artifacts_root)
    return artifacts_root / artifact_id, signer


def _snapshot(directory):
    """Every path under the directory with its bytes, to prove a run wrote nothing."""
    return {str(path.relative_to(directory)): path.read_bytes() if path.is_file() else None
            for path in sorted(directory.rglob('*'))}


@pytest.fixture
def attested(tmp_path, monkeypatch):
    """A signed bundle plus the config dir whose ring holds the signing key.

    The script checks expiry against the wall clock, so the fixture is attested
    now; otherwise its fixed 2026-07-18 attestation would expire and refusals
    would name expiry instead of the reason under test."""
    from tests.automation import fixture_bundle

    monkeypatch.setattr(fixture_bundle, 'T0', dt.datetime.now(dt.timezone.utc))
    config_dir = tmp_path / 'config'
    bundle, signer = _attest_with_new_key(config_dir, tmp_path / 'artifacts')
    return config_dir, bundle, signer


@pytest.fixture
def qualified(tmp_path, monkeypatch):
    """A bundle with qualified (non-fixture) evidence, signed by the ring key."""
    from tests.automation import paper_evidence_helpers

    monkeypatch.setattr(paper_evidence_helpers, 'NOW', dt.datetime.now(dt.timezone.utc))
    bundle, key_ring = paper_evidence_helpers.research_bundle(tmp_path)
    config_dir = tmp_path / 'config'
    _, verify_dir, public_pem = default_key_paths(config_dir)
    verify_dir.mkdir(parents=True)
    shutil.copy(key_ring / 'research.pem', public_pem)
    return config_dir, bundle


def _bundle_with_manifest_only(bundle, tmp_path):
    broken = tmp_path / 'broken'
    broken.mkdir()
    shutil.copy(bundle / 'manifest.json', broken)
    return broken


def _run(config_dir, bundle):
    return main(['--bundle', str(bundle), '--strategy-name', 'demo',
                 '--config-dir', str(config_dir)])


def test_qualified_bundle_signed_by_the_ring_key_prints_disabled_snippets(qualified, capsys):
    config_dir, bundle = qualified
    before = _snapshot(config_dir)

    assert _run(config_dir, bundle) == 0

    out = capsys.readouterr().out
    assert f'artifact_bundle_path: {bundle.resolve()}' in out
    assert 'automation:\n  enabled: false' in out
    assert 'Private key' not in out
    assert _snapshot(config_dir) == before
    assert not (config_dir / 'keys' / 'private').exists()


def test_fixture_bundle_signed_by_the_ring_key_is_refused(attested, capsys):
    config_dir, bundle, _ = attested
    before = _snapshot(config_dir)

    assert _run(config_dir, bundle) == 1

    captured = capsys.readouterr()
    assert 'fixture' in captured.err
    assert 'artifact_bundle_path' not in captured.out
    assert _snapshot(config_dir) == before


def test_a_bundle_is_required_before_any_write(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(['--strategy-name', 'demo', '--config-dir', str(tmp_path / 'config')])

    assert exc.value.code == 2
    assert '--bundle' in capsys.readouterr().err
    assert not (tmp_path / 'config').exists()


def test_refused_run_writes_nothing(attested, tmp_path, capsys):
    config_dir, _, ring_signer = attested
    bundle_signed_elsewhere = export_fixture_paper_eligible_bundle(
        signer=AttestationSigner.generate(), artifacts_root=tmp_path / 'elsewhere')
    before = _snapshot(config_dir)

    assert _run(config_dir, tmp_path / 'elsewhere' / bundle_signed_elsewhere) == 1

    captured = capsys.readouterr()
    assert f'ring holds {ring_signer.public_key_id}' in captured.err
    assert 'artifact_bundle_path' not in captured.out
    assert _snapshot(config_dir) == before


def test_bundle_signed_on_another_machine_is_refused(attested, tmp_path, capsys):
    _, bundle, signer = attested
    other_config = tmp_path / 'other_config'
    _attest_with_new_key(other_config, tmp_path / 'other_artifacts')
    before = _snapshot(other_config)

    assert _run(other_config, bundle) == 1

    assert f'bundle signed by key {signer.public_key_id}' in capsys.readouterr().err
    assert _snapshot(other_config) == before


def test_missing_signing_key_is_refused_without_creating_one(attested, tmp_path, capsys):
    _, bundle, _ = attested
    empty_config = tmp_path / 'empty_config'

    assert _run(empty_config, bundle) == 1

    captured = capsys.readouterr()
    assert f'no signing key under {empty_config / "keys" / "verify"}' in captured.err
    assert 'mmr research attest bundle' in captured.err
    assert 'artifact_bundle_path' not in captured.out
    assert not empty_config.exists()


def test_malformed_signing_key_is_refused_naming_the_file(attested, capsys):
    config_dir, bundle, _ = attested
    _, _, public_pem = default_key_paths(config_dir)
    public_pem.write_text('not a key')
    before = _snapshot(config_dir)

    assert _run(config_dir, bundle) == 1

    captured = capsys.readouterr()
    assert str(public_pem) in captured.err
    assert 'artifact_bundle_path' not in captured.out
    assert _snapshot(config_dir) == before


def test_missing_attestation_is_refused(attested, tmp_path, capsys):
    config_dir, bundle, _ = attested
    broken = _bundle_with_manifest_only(bundle, tmp_path)

    assert _run(config_dir, broken) == 1

    captured = capsys.readouterr()
    assert 'attestation.json' in captured.err
    assert 'artifact_bundle_path' not in captured.out


def test_unreadable_attestation_is_refused(attested, tmp_path, capsys):
    config_dir, bundle, _ = attested
    broken = _bundle_with_manifest_only(bundle, tmp_path)
    (broken / 'attestation.json').write_text(json.dumps(['not', 'an', 'object']))

    assert _run(config_dir, broken) == 1

    assert 'attestation.json' in capsys.readouterr().err


@pytest.mark.parametrize('key_id', [None, '', 7])
def test_attestation_without_a_key_id_string_is_refused(attested, tmp_path, capsys, key_id):
    config_dir, bundle, _ = attested
    broken = _bundle_with_manifest_only(bundle, tmp_path)
    (broken / 'attestation.json').write_text(json.dumps({'public_key_id': key_id}))

    assert _run(config_dir, broken) == 1

    err = capsys.readouterr().err
    assert 'attestation.json' in err and 'public_key_id' in err


@pytest.mark.parametrize('retired_flag', ['--force', '--offline-fixture'])
def test_key_and_fixture_writing_flags_are_gone(attested, retired_flag):
    config_dir, bundle, _ = attested

    with pytest.raises(SystemExit):
        main(['--bundle', str(bundle), '--strategy-name', 'demo',
              '--config-dir', str(config_dir), retired_flag])
