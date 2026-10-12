"""SP3 Plan 3 Task 4: the trader's writer of the content-addressed store (spec 7.3, 8; review focus 3)."""
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from tests.automation.authored_fixtures import VALID_SOURCE, digest_of
from trader.automation.authored_files import (
    DEFAULT_STORE_DIR, STORE_DIR_ENV, AuthoredFileStore, AuthoredStoreError, default_store_dir,
)

DATA = VALID_SOURCE.encode("utf-8")
DIGEST = digest_of(VALID_SOURCE)
HEX = DIGEST.removeprefix("sha256:")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_puts_the_source_and_then_its_metadata(tmp_path):
    store = AuthoredFileStore(tmp_path / "store")
    store.write(DIGEST, DATA, {"class_name": "RangeBreak"})
    assert store.source_path(DIGEST) == tmp_path / "store" / f"{HEX}.py"
    assert (tmp_path / "store" / f"{HEX}.py").read_bytes() == DATA
    assert json.loads((tmp_path / "store" / f"{HEX}.json").read_text()) == {"class_name": "RangeBreak"}
    assert mode(tmp_path / "store" / f"{HEX}.py") == mode(tmp_path / "store" / f"{HEX}.json") == 0o644
    assert not list((tmp_path / "store").glob(".*.tmp"))


def test_bytes_that_do_not_match_their_digest_are_refused(tmp_path):
    with pytest.raises(AuthoredStoreError):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA + b"\n", {})
    assert not (tmp_path / "store" / f"{HEX}.py").exists()


def test_a_file_with_other_bytes_under_the_digest_is_refused(tmp_path):                 # review focus 3
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / f"{HEX}.py").write_bytes(b"print('swapped')\n")
    with pytest.raises(AuthoredStoreError, match="other bytes"):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.py").read_bytes() == b"print('swapped')\n"


def test_writing_the_same_digest_again_keeps_the_file_and_replaces_the_metadata(tmp_path):
    store = AuthoredFileStore(tmp_path / "store")
    store.write(DIGEST, DATA, {"note": "first"})
    store.write(DIGEST, DATA, {"note": "second"})
    assert json.loads((tmp_path / "store" / f"{HEX}.json").read_text()) == {"note": "second"}


def test_a_crash_before_the_metadata_leaves_only_the_source(tmp_path, monkeypatch):     # spec 8
    store = AuthoredFileStore(tmp_path / "store")
    real_replace = AuthoredFileStore._replace

    def full_disk_on_metadata(self, path, data):
        if path.suffix == ".json":
            raise OSError("no space left on device")
        return real_replace(self, path, data)
    monkeypatch.setattr(AuthoredFileStore, "_replace", full_disk_on_metadata)
    with pytest.raises(OSError):
        store.write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.py").exists() and not (tmp_path / "store" / f"{HEX}.json").exists()
    monkeypatch.undo()
    store.write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.json").exists()


def test_a_symlinked_store_directory_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(AuthoredStoreError, match="not a real directory"):
        AuthoredFileStore(tmp_path / "link").write(DIGEST, DATA, {})


def test_the_default_directory_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "elsewhere"))
    assert default_store_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv(STORE_DIR_ENV)
    assert default_store_dir() == Path(DEFAULT_STORE_DIR).expanduser()


def test_an_existing_source_that_is_a_symlink_is_refused(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "elsewhere.py").write_bytes(DATA)
    (tmp_path / "store" / f"{HEX}.py").symlink_to(tmp_path / "elsewhere.py")
    with pytest.raises(AuthoredStoreError, match="not a regular file"):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.py").is_symlink()


def test_an_existing_source_with_the_wrong_mode_is_refused(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / f"{HEX}.py").write_bytes(DATA)
    os.chmod(tmp_path / "store" / f"{HEX}.py", 0o600)
    with pytest.raises(AuthoredStoreError, match="mode 0o600"):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})
    assert mode(tmp_path / "store" / f"{HEX}.py") == 0o600


def test_an_existing_orphan_source_with_the_right_bytes_and_mode_is_kept(tmp_path):         # spec 8
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / f"{HEX}.py").write_bytes(DATA)
    os.chmod(tmp_path / "store" / f"{HEX}.py", 0o644)
    inode = (tmp_path / "store" / f"{HEX}.py").stat().st_ino
    AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {"note": "n"})
    assert (tmp_path / "store" / f"{HEX}.py").stat().st_ino == inode


@pytest.mark.parametrize("problem", ["symlink", "mode", "not json", "not an object"])
def test_an_existing_metadata_file_that_is_not_ours_is_refused(tmp_path, problem):
    store = AuthoredFileStore(tmp_path / "store")
    store.write(DIGEST, DATA, {"note": "first"})
    meta = tmp_path / "store" / f"{HEX}.json"
    if problem == "symlink":
        meta.unlink()
        (tmp_path / "other.json").write_text("{}")
        meta.symlink_to(tmp_path / "other.json")
    elif problem == "mode":
        os.chmod(meta, 0o666)
    elif problem == "not json":
        meta.write_bytes(b"{not json")
    else:
        meta.write_bytes(b"[1, 2]")
    with pytest.raises(AuthoredStoreError):
        store.write(DIGEST, DATA, {"note": "second"})
    assert meta.is_symlink() if problem == "symlink" else "second" not in meta.read_text()


def test_an_existing_metadata_file_with_the_same_content_is_kept(tmp_path):
    store = AuthoredFileStore(tmp_path / "store")
    store.write(DIGEST, DATA, {"note": "same"})
    inode = (tmp_path / "store" / f"{HEX}.json").stat().st_ino
    store.write(DIGEST, DATA, {"note": "same"})
    assert (tmp_path / "store" / f"{HEX}.json").stat().st_ino == inode


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_read_only_store_directory_is_a_store_error(tmp_path):
    (tmp_path / "store").mkdir()
    os.chmod(tmp_path / "store", 0o555)
    try:
        with pytest.raises(AuthoredStoreError, match=f"errno {errno.EACCES}") as failed:
            AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})
    finally:
        os.chmod(tmp_path / "store", 0o755)
    assert "create" in str(failed.value) and str(tmp_path / "store") in str(failed.value)
    assert not list((tmp_path / "store").iterdir())


def test_a_store_root_that_is_a_file_is_a_store_error(tmp_path):
    (tmp_path / "store").write_text("not a directory")
    with pytest.raises(AuthoredStoreError):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})


def test_an_fsync_failure_is_a_store_error_and_leaves_no_temp_file(tmp_path, monkeypatch):
    def full_disk(descriptor):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(os, "fsync", full_disk)
    with pytest.raises(AuthoredStoreError, match=f"fsync .* errno {errno.ENOSPC}"):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})
    assert not list((tmp_path / "store").iterdir())


def test_each_file_has_mode_0644_before_it_is_flushed_and_renamed(tmp_path, monkeypatch):
    modes_at_fsync = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISREG(info.st_mode):
            modes_at_fsync.append(stat.S_IMODE(info.st_mode))
        return real_fsync(descriptor)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    previous = os.umask(0o077)
    try:
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {"note": "n"})
    finally:
        os.umask(previous)
    assert modes_at_fsync == [0o644, 0o644]
