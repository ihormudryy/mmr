import io
import os
import stat

from cryptography.hazmat.primitives import serialization

from trader.automation.paper_materials import default_key_paths
from trader.messaging.keys_cli import main


def _run(tmp_path, in_container=False):
    out = io.StringIO()
    code = main(["init-signing", "--config-dir", str(tmp_path)], in_container=lambda: in_container, out=out)
    return code, out.getvalue()


def _paths(tmp_path):
    private, _verify_dir, public = default_key_paths(tmp_path)
    return private, public


def test_fresh_install_creates_both_keys_and_prints_only_paths(tmp_path):
    code, out = _run(tmp_path)
    private, public = _paths(tmp_path)
    assert code == 0 and "created" in out
    assert str(private) in out and str(public) in out
    assert "PRIVATE KEY" not in out and "PUBLIC KEY" not in out
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    derived = serialization.load_pem_private_key(private.read_bytes(), password=None).public_key()
    assert public.read_bytes() == derived.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def test_restore_case_derives_the_missing_public_half_and_keeps_the_private_key(tmp_path):
    _run(tmp_path)
    private, public = _paths(tmp_path)
    original_private, original_public = private.read_bytes(), public.read_bytes()
    public.unlink()
    code, out = _run(tmp_path)
    assert code == 0 and "derived" in out
    assert private.read_bytes() == original_private and public.read_bytes() == original_public


def test_both_present_changes_nothing(tmp_path):
    _run(tmp_path)
    private, public = _paths(tmp_path)
    before = (private.read_bytes(), public.read_bytes(), private.stat().st_mtime_ns, public.stat().st_mtime_ns)
    code, out = _run(tmp_path)
    assert code == 0 and "unchanged" in out
    assert before == (private.read_bytes(), public.read_bytes(),
                      private.stat().st_mtime_ns, public.stat().st_mtime_ns)


def test_public_key_without_private_key_is_refused_and_no_key_is_created(tmp_path):
    _run(tmp_path)
    private, public = _paths(tmp_path)
    private.unlink()
    code, out = _run(tmp_path)
    assert code == 1 and "Error" in out and not private.exists()


def test_a_world_readable_private_key_is_refused(tmp_path):
    _run(tmp_path)
    private, public = _paths(tmp_path)
    public.unlink()
    os.chmod(private, 0o644)
    code, out = _run(tmp_path)
    assert code == 1 and "Error" in out and not public.exists()


def test_refused_inside_a_container(tmp_path):
    code, out = _run(tmp_path, in_container=True)
    assert code == 2 and not _paths(tmp_path)[0].exists()


def test_a_directory_in_place_of_a_key_is_a_clean_one_line_error(tmp_path):
    private, public = _paths(tmp_path)
    public.mkdir(parents=True)
    private.parent.mkdir(parents=True)
    private.mkdir()
    code, out = _run(tmp_path)
    assert code == 1 and out.startswith("Error:") and len(out.strip().splitlines()) == 1
