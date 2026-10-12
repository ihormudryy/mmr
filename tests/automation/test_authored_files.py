"""SP3 Plan 3 Task 4: the trader's writer of the content-addressed store (spec 7.3, 8; review focus 3)."""
from __future__ import annotations

import json
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
