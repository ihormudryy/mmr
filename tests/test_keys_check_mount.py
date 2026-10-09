"""PR #50 round 1, finding 4: the in-container key-mount check behind ./docker.sh -K."""
import io

import pytest

from trader.messaging.keys_cli import main
from trader.messaging.principals import SERVICE_PRINCIPAL, rpc_files_for, service_rpc_files
from trader.research.signing import generate_private_key_pem


@pytest.fixture(autouse=True)
def rpc_keys_dir(tmp_path, monkeypatch):
    """The signing-key load refuses an RPC identity key; it must look at the test's keys, not the owner's."""
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "rpc"))


def _container_view(tmp_path, service, *, extra=(), missing=(), hmac=b""):
    import shutil

    from tests.rpc_identity_fixtures import write_keyset

    host = tmp_path / "host_rpc"
    write_keyset(host)
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    for name in (service_rpc_files(service) - set(missing)) | set(extra):
        shutil.copy2(host / name, rpc / name)
    if service == "research":
        (tmp_path / "private").mkdir(exist_ok=True)
        signing = tmp_path / "private" / "signing.pem"
        signing.write_bytes(generate_private_key_pem())
        signing.chmod(0o600)
    hmac_file = tmp_path / "service_hmac.key"
    if hmac is not None:
        hmac_file.write_bytes(hmac)
    return ["check-mount", service, "--keys-dir", str(rpc), "--hmac-file", str(hmac_file)]


def _run(argv):
    out = io.StringIO()
    return main(argv, in_container=lambda: True, out=out), out.getvalue()


@pytest.mark.parametrize("service", sorted(SERVICE_PRINCIPAL))
def test_exact_own_pair_and_peer_pubs_with_empty_hmac_passes(tmp_path, service):
    code, out = _run(_container_view(tmp_path, service))
    assert code == 0, out
    assert service in out


def test_check_runs_in_a_service_container_without_the_keygen_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("MMR_KEYGEN_CONTAINER", raising=False)
    code, out = _run(_container_view(tmp_path, "trader"))
    assert code == 0, out


def test_a_signing_key_outside_research_fails_the_gate(tmp_path):
    argv = _container_view(tmp_path, "trader")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "signing.pem").write_text("x")
    code, out = _run(argv)
    assert code == 1 and "unexpected" in out and "signing.pem" in out


@pytest.mark.parametrize("mode,content,error", [
    (0o644, None, "InsecureKeyFile"),                  # a real key, readable by others
    (0o600, b"not a key", "MalformedKey"),
])
def test_research_with_a_signing_key_that_does_not_load_fails_the_gate(tmp_path, mode, content, error):
    argv = _container_view(tmp_path, "research")
    signing = tmp_path / "private" / "signing.pem"
    if content is not None:
        signing.write_bytes(content)
    key_bytes = signing.read_bytes()
    signing.chmod(mode)
    code, out = _run(argv)
    assert code == 1 and "signing.pem" in out and error in out
    assert key_bytes.decode(errors="replace").strip() not in out


def test_research_without_its_signing_key_fails_the_gate(tmp_path):
    argv = _container_view(tmp_path, "research")
    (tmp_path / "private" / "signing.pem").unlink()
    code, out = _run(argv)
    assert code == 1 and "missing" in out and "signing.pem" in out


@pytest.mark.parametrize("case", [
    {"extra": ["cli.key"]},
    {"extra": ["ai_research.pub"], "service": "strategy"},
    {"missing": ["trader.pub"]},
    {"missing": ["strategy.pub"]},
    {"hmac": b"secret"},
    {"hmac": None},
], ids=["foreign-private-key", "unneeded-pub", "own-pub-missing", "peer-pub-missing",
        "hmac-readable", "hmac-placeholder-missing"])
def test_any_deviation_fails(tmp_path, case):
    service = case.pop("service", "trader")
    code, out = _run(_container_view(tmp_path, service, **case))
    assert code == 1, out
    assert "secret" not in out


def test_scheduler_and_data_must_see_no_keys(tmp_path):
    code, out = _run(_container_view(tmp_path, "data", extra=["trader.pub"]))
    assert code == 1 and "trader.pub" in out


def test_unknown_service_is_refused(tmp_path):
    code, _out = _run(["check-mount", "keygen", "--keys-dir", str(tmp_path), "--hmac-file", str(tmp_path / "h")])
    assert code == 2


# --- PR #50 round 2: the gate checks the key pair itself, not only names ---

def _replace_with_stranger_pub(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


@pytest.mark.parametrize("service", ["trader", "strategy", "dashboard", "cli"])
def test_mismatched_own_pair_after_an_interrupted_rotation_fails(tmp_path, service):
    argv = _container_view(tmp_path, service)
    _replace_with_stranger_pub(tmp_path / "rpc" / f"{SERVICE_PRINCIPAL[service]}.pub")
    code, out = _run(argv)
    assert code == 1, out
    assert "does not match" in out


def test_real_rotation_crash_between_renames_fails_the_gate(tmp_path, monkeypatch):
    import os
    import shutil

    import trader.messaging.rpc_keys as rpc_keys
    from trader.messaging.rpc_keys import init_keys

    host = tmp_path / "host"
    init_keys(host)
    real_replace = os.replace
    calls = []

    def crash_on_second(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("crash")
        real_replace(src, dst)

    monkeypatch.setattr(rpc_keys.os, "replace", crash_on_second)
    with pytest.raises(OSError):
        init_keys(host, rotate="trader")
    monkeypatch.setattr(rpc_keys.os, "replace", real_replace)

    rpc = tmp_path / "rpc"
    rpc.mkdir()
    for name in rpc_files_for("trader"):
        shutil.copy2(host / name, rpc / name)
    hmac_file = tmp_path / "service_hmac.key"
    hmac_file.write_bytes(b"")
    code, out = _run(["check-mount", "trader", "--keys-dir", str(rpc), "--hmac-file", str(hmac_file)])
    assert code == 1 and "does not match" in out


def test_loose_private_key_mode_fails_like_service_startup(tmp_path):
    import os

    argv = _container_view(tmp_path, "strategy")
    os.chmod(tmp_path / "rpc" / "strategy.key", 0o644)
    code, out = _run(argv)
    assert code == 1 and "strategy.key" in out


def test_garbage_peer_public_key_fails(tmp_path):
    argv = _container_view(tmp_path, "dashboard")
    (tmp_path / "rpc" / "trader.pub").write_bytes(b"garbage")
    code, out = _run(argv)
    assert code == 1 and "trader.pub" in out
