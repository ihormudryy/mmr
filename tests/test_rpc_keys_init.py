import os
import stat
import sys

import pytest

from trader.messaging.principals import KNOWN_PRINCIPALS
from trader.messaging.rpc_keys import RESTART_ON_ROTATE, RpcKeyError, init_keys, load_identity_material


def _run_cli(argv, monkeypatch=None):
    import trader.mmr_cli as cli
    args = cli.build_parser().parse_args(argv)
    return cli._handle_keys(args)


@pytest.fixture(autouse=True)
def _on_the_host(monkeypatch):
    monkeypatch.setattr("trader.mmr_cli._running_in_container", lambda: False)


def test_init_creates_every_known_principal_with_strict_modes(tmp_path):
    rows = init_keys(tmp_path / "rpc")
    assert {r.principal for r in rows} == KNOWN_PRINCIPALS and {r.status for r in rows} == {"created"}
    for p in KNOWN_PRINCIPALS:
        assert stat.S_IMODE(os.lstat(tmp_path / "rpc" / f"{p}.key").st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(tmp_path / "rpc" / f"{p}.pub").st_mode) == 0o644
    assert stat.S_IMODE(os.lstat(tmp_path / "rpc").st_mode) == 0o700
    load_identity_material("trader", tmp_path / "rpc")
    assert not list((tmp_path / "rpc").glob(".*.tmp"))


def test_init_is_idempotent_and_never_overwrites(tmp_path):
    init_keys(tmp_path)
    before = (tmp_path / "cli.key").read_bytes()
    assert {r.status for r in init_keys(tmp_path)} == {"kept"}
    assert (tmp_path / "cli.key").read_bytes() == before


def test_half_pair_is_an_error_not_a_repair(tmp_path):
    init_keys(tmp_path)
    (tmp_path / "cli.pub").unlink()
    with pytest.raises(RpcKeyError, match="cli"):
        init_keys(tmp_path)
    assert not (tmp_path / "cli.pub").exists()


def test_mismatched_pair_is_an_error(tmp_path):
    init_keys(tmp_path)
    (tmp_path / "cli.pub").write_bytes((tmp_path / "trader.pub").read_bytes())
    with pytest.raises(RpcKeyError, match="does not match"):
        init_keys(tmp_path)


def test_rotate_replaces_one_pair_only(tmp_path):
    init_keys(tmp_path)
    old = {p: (tmp_path / f"{p}.key").read_bytes() for p in KNOWN_PRINCIPALS}
    rows = init_keys(tmp_path, rotate="dashboard")
    assert [r.principal for r in rows if r.status == "rotated"] == ["dashboard"]
    assert (tmp_path / "dashboard.key").read_bytes() != old["dashboard"]
    assert all((tmp_path / f"{p}.key").read_bytes() == old[p] for p in KNOWN_PRINCIPALS - {"dashboard"})
    assert stat.S_IMODE(os.lstat(tmp_path / "dashboard.key").st_mode) == 0o600
    load_identity_material("dashboard", tmp_path)


def test_rotate_unknown_principal_is_refused(tmp_path):
    with pytest.raises(RpcKeyError):
        init_keys(tmp_path, rotate="telegram_bridge")
    assert not any(tmp_path.iterdir())


def test_restart_list_follows_the_peers():
    assert RESTART_ON_ROTATE["strategy"] == ("dashboard", "strategy", "trader")
    assert RESTART_ON_ROTATE["cli"] == ("strategy", "trader")
    assert RESTART_ON_ROTATE["ai_research"] == ("trader",)


def test_cli_refuses_inside_an_ordinary_container(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("trader.mmr_cli._running_in_container", lambda: True)
    monkeypatch.delenv("MMR_KEYGEN_CONTAINER", raising=False)
    assert _run_cli(["keys", "init", "--keys-dir", str(tmp_path)]) != 0
    assert "docker.sh -k" in capsys.readouterr().out and not any(tmp_path.iterdir())


def test_cli_allows_keygen_in_the_one_shot_keygen_container(monkeypatch, tmp_path):
    monkeypatch.setattr("trader.mmr_cli._running_in_container", lambda: True)
    monkeypatch.setenv("MMR_KEYGEN_CONTAINER", "1")
    assert _run_cli(["keys", "init", "--keys-dir", str(tmp_path)]) == 0
    assert (tmp_path / "trader.key").exists()


def test_cli_prints_restart_list_on_rotate(tmp_path, capsys):
    _run_cli(["keys", "init", "--keys-dir", str(tmp_path)])
    _run_cli(["keys", "init", "--keys-dir", str(tmp_path), "--rotate", "strategy"])
    out = capsys.readouterr().out
    assert "strategy" in out and "trader" in out and "dashboard" in out
    assert "in-flight" in out


def test_cli_init_uses_the_default_keys_dir(isolated_rpc_keys_dir):
    assert _run_cli(["keys", "init"]) == 0
    assert (isolated_rpc_keys_dir / "cli.key").exists()


def test_keygen_entry_module_imports_no_service_code():
    import subprocess
    code = ("import sys, trader.messaging.keys_cli; "
            "bad=[m for m in sys.modules if m in ('trader.sdk','trader.container','trader.mmr_cli')]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
