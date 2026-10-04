from trader.trading.command_stack import _bundle_root_for


def test_sha256_bundle_dir_resolves_to_its_parent(tmp_path):
    assert _bundle_root_for(str(tmp_path / 'artifacts' / 'sha256_abc')) == tmp_path / 'artifacts'


def test_legacy_artifact_dir_still_resolves_to_its_parent(tmp_path):
    assert _bundle_root_for(str(tmp_path / 'artifacts' / 'artifact-1')) == tmp_path / 'artifacts'


def test_root_dir_is_kept(tmp_path):
    assert _bundle_root_for(str(tmp_path / 'artifacts')) == tmp_path / 'artifacts'
