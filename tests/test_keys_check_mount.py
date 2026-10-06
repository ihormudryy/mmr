"""PR #50 round 1, finding 4: the in-container key-mount check behind ./docker.sh -K."""
import io

import pytest

from trader.messaging.keys_cli import main
from trader.messaging.principals import SERVICE_PRINCIPAL, rpc_files_for


def _container_view(tmp_path, service, *, extra=(), missing=(), hmac=b""):
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    for name in (rpc_files_for(SERVICE_PRINCIPAL[service]) - set(missing)) | set(extra):
        (rpc / name).write_bytes(b"placeholder")
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
