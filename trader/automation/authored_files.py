"""The trader's writer of the content-addressed store (SP3 spec 7.3, 8).

``<hex>.py`` holds the authored file's exact bytes, ``<hex>.json`` its metadata. Each goes to a fresh
temp file, is flushed to disk and renamed into place, ``.py`` before ``.json``; the caller writes the
journal record only after both. A crash leaves at most an orphan file, never a record without its file.
The sandbox mounts the same volume read-only and opens files with ``O_NOFOLLOW``, so every store file
must be a regular 0644 file. A file already in place is checked before it is trusted: anything but a
regular 0644 file, or a ``.py`` whose bytes do not hash to its digest, is an error, never a repair.
Every failing file-system call is an ``AuthoredStoreError`` that names the operation, the path and errno.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Iterator, Mapping, Optional

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


@contextlib.contextmanager
def _os_step(operation: str, path: Path) -> Iterator[None]:
    try:
        yield
    except OSError as error:
        raise AuthoredStoreError(
            f"{operation} {path} failed: errno {error.errno} ({error.strerror})") from error


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
        if not self._holds_source(source_path, hex_digest):
            self._replace(source_path, source)
        meta_path = self._root / f"{hex_digest}.json"
        meta_bytes = canonical_json_bytes(dict(meta))
        if not self._holds_metadata(meta_path, meta_bytes):
            self._replace(meta_path, meta_bytes)

    def _prepare_root(self) -> None:
        with _os_step("create", self._root):
            self._root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        with _os_step("inspect", self._root):
            info = os.lstat(self._root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AuthoredStoreError(f"{self._root} is not a real directory")

    def _holds_source(self, path: Path, hex_digest: str) -> bool:
        """True when ``path`` already holds the digest's bytes (an orphan of an earlier crash)."""
        data = self._existing(path)
        if data is None:
            return False
        if hashlib.sha256(data).hexdigest() != hex_digest:
            raise AuthoredStoreError(f"{path} holds other bytes than its digest names")
        return True

    def _holds_metadata(self, path: Path, wanted: bytes) -> bool:
        """True when ``path`` already holds ``wanted``.

        Callers write only for a digest without a journal record, so a different but valid ``.json``
        is an orphan of an attempt whose record never committed, and is replaced.
        """
        data = self._existing(path)
        if data is None:
            return False
        try:
            parsed = json.loads(data)
        except ValueError as error:
            raise AuthoredStoreError(f"{path} is not valid JSON") from error
        if not isinstance(parsed, dict):
            raise AuthoredStoreError(f"{path} is not a JSON object")
        return data == wanted

    @staticmethod
    def _existing(path: Path) -> Optional[bytes]:
        """The bytes of a regular 0644 file at ``path``, None when there is none; anything else is refused."""
        with _os_step("inspect", path):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                return None
        if not stat.S_ISREG(info.st_mode):
            raise AuthoredStoreError(f"{path} is not a regular file")
        if stat.S_IMODE(info.st_mode) != FILE_MODE:
            raise AuthoredStoreError(f"{path} has mode {oct(stat.S_IMODE(info.st_mode))}, not {oct(FILE_MODE)}")
        with _os_step("read", path):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise AuthoredStoreError(f"{path} changed while it was checked")
                return handle.read()

    def _replace(self, path: Path, data: bytes) -> None:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        with _os_step("create", temp):
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        try:
            with _os_step("write", temp), os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                with _os_step("chmod", temp):
                    os.fchmod(handle.fileno(), FILE_MODE)
                with _os_step("fsync", temp):
                    os.fsync(handle.fileno())
            with _os_step("rename", path):
                os.replace(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):         # never hide the real failure behind the cleanup's
                temp.unlink(missing_ok=True)
            raise
        self._sync_root()

    def _sync_root(self) -> None:
        with _os_step("fsync", self._root):
            descriptor = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
