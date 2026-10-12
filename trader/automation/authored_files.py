"""The trader's writer of the content-addressed store (SP3 spec 7.3, 8).

``<hex>.py`` holds the authored file's exact bytes, ``<hex>.json`` its metadata. Each goes to a fresh
temp file, is flushed to disk and renamed into place, ``.py`` before ``.json``; the caller writes the
journal record only after both. A crash leaves at most an orphan file, never a record without its file.
The sandbox mounts the same volume read-only, so files are 0644, and a ``.py`` is never rewritten with
other bytes: a mismatch is an error, not a repair.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Mapping

from trader.research.canonical import canonical_json_bytes

STORE_DIR_ENV = "MMR_AUTHORED_STORE_DIR"
DEFAULT_STORE_DIR = "~/.local/share/mmr/authored_strategies"
FILE_MODE = 0o644
DIR_MODE = 0o755
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class AuthoredStoreError(Exception):
    """The store directory or a stored file is unusable; the message names the path, never file bytes."""


def default_store_dir() -> Path:
    return Path(os.environ.get(STORE_DIR_ENV) or DEFAULT_STORE_DIR).expanduser()


def _hex(digest: str) -> str:
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if match is None:
        raise AuthoredStoreError(f"{digest!r} is not sha256:<64 hex>")
    return match.group(1)


class AuthoredFileStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def source_path(self, digest: str) -> Path:
        return self._root / f"{_hex(digest)}.py"

    def write(self, digest: str, source: bytes, meta: Mapping) -> None:
        hex_digest = _hex(digest)
        if hashlib.sha256(source).hexdigest() != hex_digest:
            raise AuthoredStoreError("the source bytes do not match their digest")
        self._prepare_root()
        source_path = self._root / f"{hex_digest}.py"
        if not self._holds(source_path, source):
            self._replace(source_path, source)
        self._replace(self._root / f"{hex_digest}.json", canonical_json_bytes(dict(meta)))

    def _prepare_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        info = os.lstat(self._root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AuthoredStoreError(f"{self._root} is not a real directory")

    @staticmethod
    def _holds(path: Path, data: bytes) -> bool:
        """True when ``path`` already holds exactly ``data`` (an orphan of an earlier crash)."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise AuthoredStoreError(f"{path} is not a regular file")
        if path.read_bytes() != data:
            raise AuthoredStoreError(f"{path} holds other bytes than its digest names")
        return True

    def _replace(self, path: Path, data: bytes) -> None:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, FILE_MODE)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        self._sync_root()

    def _sync_root(self) -> None:
        descriptor = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
