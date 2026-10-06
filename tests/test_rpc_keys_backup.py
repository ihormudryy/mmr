import io
import logging
import os
import shutil
import stat
import subprocess
import tarfile

import pytest

from trader.messaging import keys_cli
from trader.messaging.principals import KNOWN_PRINCIPALS
from trader.messaging.rpc_keys import RpcKeyError, backup_keys, init_keys, restore_keys

IDENTITY = b"AGE-SECRET-KEY-TEST"
ALL_FILES = {f"{p}.{e}" for p in KNOWN_PRINCIPALS for e in ("key", "pub")}


class FakeAge:
    """Stands in for the age binary: 'encrypts' by storing the plaintext in memory."""

    def __init__(self):
        self.calls = []
        self.store = {}

    def __call__(self, argv, stdin_bytes, *, identity=None):
        self.calls.append((list(argv), identity))
        if argv[:2] == ["age", "-R"]:
            token = f"CIPHERTEXT-{len(self.store)}".encode()
            self.store[token] = stdin_bytes
            return token
        if argv[:2] == ["age", "-d"]:
            assert identity.strip() == IDENTITY
            return self.store[stdin_bytes]
        raise AssertionError(argv)


def _all_bytes_under(root):
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_backup_pipes_only_rpc_keys_into_the_encryptor_and_writes_no_plaintext(tmp_path):
    keys = tmp_path / "config/keys"
    init_keys(keys / "rpc")
    (keys / "verify").mkdir()
    (keys / "verify/paper.pem").write_text("bundle")
    (keys.parent / "service_hmac.key").write_text("old")
    seen = {}

    def fake_run(argv, stdin_bytes, *, identity=None):
        seen["argv"], seen["tar"] = argv, stdin_bytes
        return b"CIPHERTEXT"

    recipient = tmp_path / "recipient.txt"
    out = backup_keys(keys / "rpc", tmp_path / "b/rpc_keys.tar.age", recipient, run=fake_run)
    names = set(tarfile.open(fileobj=io.BytesIO(seen["tar"])).getnames())
    assert names == ALL_FILES
    assert seen["argv"] == ["age", "-R", str(recipient)]
    assert out.read_bytes() == b"CIPHERTEXT" and stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    assert not list(tmp_path.rglob("*.tar"))


def test_backup_fails_loudly_without_the_encryptor(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(RpcKeyError, match="age"):
        backup_keys(tmp_path, tmp_path / "x.age", tmp_path / "r.txt")


def test_backup_never_overwrites_an_existing_archive(tmp_path):
    init_keys(tmp_path / "rpc")
    fake = FakeAge()
    out = tmp_path / "b/x.tar.age"
    backup_keys(tmp_path / "rpc", out, tmp_path / "r.txt", run=fake)
    with pytest.raises(FileExistsError):
        backup_keys(tmp_path / "rpc", out, tmp_path / "r.txt", run=fake)


def test_backup_and_restore_never_log_print_or_persist_the_identity(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    init_keys(tmp_path / "rpc")
    fake = FakeAge()
    archive = backup_keys(tmp_path / "rpc", tmp_path / "b/x.tar.age", tmp_path / "r.txt", run=fake)
    restore_keys(archive, tmp_path / "restored", IDENTITY, run=fake)
    captured = capsys.readouterr()
    assert IDENTITY.decode() not in captured.out + captured.err + caplog.text
    assert IDENTITY not in _all_bytes_under(tmp_path)
    assert all(IDENTITY.decode() not in " ".join(argv) for argv, _ in fake.calls)


def test_restore_refuses_a_non_empty_keys_dir_and_revalidates_modes(tmp_path):
    init_keys(tmp_path / "rpc")
    fake = FakeAge()
    archive = backup_keys(tmp_path / "rpc", tmp_path / "b/x.tar.age", tmp_path / "r.txt", run=fake)
    restored = tmp_path / "restored"
    assert restore_keys(archive, restored, IDENTITY, run=fake) == sorted(KNOWN_PRINCIPALS)
    assert {p.name for p in restored.iterdir()} == ALL_FILES
    for p in KNOWN_PRINCIPALS:
        assert stat.S_IMODE(os.lstat(restored / f"{p}.key").st_mode) == 0o600
        assert (restored / f"{p}.key").read_bytes() == (tmp_path / "rpc" / f"{p}.key").read_bytes()
    before = (tmp_path / "rpc" / "cli.key").read_bytes()
    with pytest.raises(RpcKeyError, match="empty"):
        restore_keys(archive, tmp_path / "rpc", IDENTITY, run=fake)
    assert (tmp_path / "rpc" / "cli.key").read_bytes() == before


def test_restore_refuses_an_archive_with_foreign_entries(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("../evil.key")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    fake = FakeAge()
    fake.store[b"C"] = buffer.getvalue()
    (tmp_path / "a.age").write_bytes(b"C")
    with pytest.raises(RpcKeyError, match="unexpected"):
        restore_keys(tmp_path / "a.age", tmp_path / "out", IDENTITY, run=fake)
    assert not list((tmp_path).rglob("evil*"))


def test_restore_reads_the_identity_from_stdin_and_backup_never_needs_it(tmp_path, monkeypatch, capsys):
    init_keys(tmp_path / "rpc")
    fake = FakeAge()
    monkeypatch.setattr("trader.messaging.rpc_keys._require_age", lambda run: fake)
    parser = keys_cli.argparse.ArgumentParser()
    keys_cli.add_keys_parser(parser.add_subparsers(dest="command"))
    out = tmp_path / "b/x.tar.age"
    backup_args = parser.parse_args(["keys", "backup", "--recipient", str(tmp_path / "r.txt"),
                                     "--out", str(out), "--keys-dir", str(tmp_path / "rpc")])
    assert keys_cli.run_keys_command(backup_args, in_container=False) == 0
    assert all(identity is None for _, identity in fake.calls)

    restore_args = parser.parse_args(["keys", "restore", str(out), "--identity-stdin",
                                      "--keys-dir", str(tmp_path / "restored")])
    stdin = io.TextIOWrapper(io.BytesIO(IDENTITY + b"\n"))
    assert keys_cli.run_keys_command(restore_args, in_container=False, stdin=stdin) == 0
    assert fake.calls[-1][1] == IDENTITY + b"\n"
    assert IDENTITY.decode() not in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("age") is None or shutil.which("age-keygen") is None,
                    reason="age not installed")
def test_backup_restore_round_trip_with_real_age(tmp_path):
    identity_file = tmp_path / "id.txt"
    subprocess.run(["age-keygen", "-o", str(identity_file)], check=True, capture_output=True)
    recipient = subprocess.run(["age-keygen", "-y", str(identity_file)], check=True,
                               capture_output=True).stdout
    (tmp_path / "r.txt").write_bytes(recipient)
    init_keys(tmp_path / "rpc")
    archive = backup_keys(tmp_path / "rpc", tmp_path / "b/x.tar.age", tmp_path / "r.txt")
    restore_keys(archive, tmp_path / "restored", identity_file.read_bytes())
    assert (tmp_path / "restored/cli.key").read_bytes() == (tmp_path / "rpc/cli.key").read_bytes()
