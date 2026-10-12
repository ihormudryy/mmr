"""Slot directories and the startup capability drop (ruling 2).

The supervisor starts as uid 0 with CHOWN, SETUID, SETGID and KILL. It creates ``/tmp/s<uid>`` for each slot,
owned by the slot uid and group sbxchild, mode 0700 (that needs CAP_CHOWN), then drops CAP_CHOWN before it
serves anything. The serving effective set is exactly SETUID|SETGID|KILL (CapEff ``00000000000000e0``).
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys
from dataclasses import dataclass
from typing import Iterable, Protocol

from trader.sandbox.child import SBXCHILD_GID

CAP_CHOWN = 0
CAP_KILL = 5
CAP_SETGID = 6
CAP_SETUID = 7
SERVING_CAPS = (1 << CAP_KILL) | (1 << CAP_SETGID) | (1 << CAP_SETUID)   # 0xe0

SLOT_ROOT = "/tmp"
SLOT_DIR_MODE = 0o700

_STATUS_PATH = "/proc/self/status"
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_CAP_WORDS = 2          # version 3 carries 64 capability bits as two 32-bit words
_WORD_MASK = 0xFFFFFFFF


class CapabilityError(RuntimeError):
    """The capability state is not what serving needs, or cannot be read: the supervisor must not serve."""


class SlotDirError(RuntimeError):
    """A slot directory could not be made exactly as a session needs it."""


# ── slot directories ────────────────────────────────────────────────────────
def slot_dir(uid: int, root: str = SLOT_ROOT) -> str:
    return os.path.join(root, f"s{uid}")


def slot_dirs(uids: Iterable[int], root: str = SLOT_ROOT) -> list[str]:
    return [slot_dir(uid, root) for uid in uids]


def prepare_slot_dirs(uids: Iterable[int], gid: int = SBXCHILD_GID, *, root: str = SLOT_ROOT) -> None:
    """Make each slot's directory, owned by the slot uid and ``gid``, mode 0700. Needs CAP_CHOWN."""
    for uid in uids:
        _prepare_slot_dir(slot_dir(uid, root), uid, gid)


def _prepare_slot_dir(path: str, uid: int, gid: int) -> None:
    try:
        os.mkdir(path, SLOT_DIR_MODE)
    except FileExistsError:
        # The tmpfs is new at every container start. A dir (or link) already there may hold an earlier
        # session's files, which a new session must never read (ruling 13).
        raise SlotDirError(f"{path} already exists: refusing to reuse it") from None
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        # chmod first: once the slot uid owns the dir, root without CAP_FOWNER can no longer change its mode.
        os.fchmod(fd, SLOT_DIR_MODE)
        os.fchown(fd, uid, gid)
        info = os.fstat(fd)
    finally:
        os.close(fd)
    found = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
    if found != (uid, gid, SLOT_DIR_MODE):
        raise SlotDirError(f"{path} has owner, group and mode {found}, expected {(uid, gid, SLOT_DIR_MODE)}")


# ── reading the capability sets ─────────────────────────────────────────────
def has_effective_cap(bit: int) -> bool:
    return bool(_status_hex("CapEff") & (1 << bit))


def require_serving_caps() -> None:
    """The effective set must be exactly SETUID|SETGID|KILL before the first request is served."""
    effective = _status_hex("CapEff")
    if effective != SERVING_CAPS:
        raise CapabilityError(f"effective capabilities are {effective:016x}, serving needs exactly "
                              f"{SERVING_CAPS:016x} (SETUID, SETGID, KILL)")


def _status_field(name: str) -> str:
    """One field of the status file. Unreadable is an error, never a clean answer."""
    try:
        with open(_STATUS_PATH, encoding="utf-8") as status:
            for line in status:
                key, separator, value = line.partition(":")
                if separator and key == name:
                    return value.strip()
    except (OSError, ValueError) as exc:   # ValueError: bytes that are not UTF-8
        raise CapabilityError(f"cannot read {_STATUS_PATH}: {exc}") from exc
    raise CapabilityError(f"{_STATUS_PATH} has no {name} line")


def _status_hex(name: str) -> int:
    text = _status_field(name)
    try:
        return int(text, 16)
    except ValueError:
        raise CapabilityError(f"{_STATUS_PATH} {name} is not hex: {text[:40]!r}") from None


def _thread_count() -> int:
    text = _status_field("Threads")
    try:
        return int(text)
    except ValueError:
        raise CapabilityError(f"{_STATUS_PATH} Threads is not a number: {text[:40]!r}") from None


# ── dropping a capability ───────────────────────────────────────────────────
@dataclass(frozen=True)
class CapWord:
    """One 32-bit word of each capability set, as capget and capset carry them."""
    effective: int
    permitted: int
    inheritable: int

    def without(self, mask: int) -> CapWord:
        keep = ~mask & _WORD_MASK
        return CapWord(self.effective & keep, self.permitted & keep, self.inheritable & keep)


class CapSyscalls(Protocol):
    def get(self) -> list[CapWord]: ...

    def set(self, words: list[CapWord]) -> None: ...


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


class _LibcCapSyscalls:
    """capget/capset on the calling thread (pid 0), through libc. Linux only."""

    def __init__(self) -> None:
        if sys.platform != "linux":
            raise CapabilityError(f"dropping a capability needs Linux capset; refusing on {sys.platform}")
        self._libc = ctypes.CDLL(None, use_errno=True)

    def get(self) -> list[CapWord]:
        data = (_CapData * _CAP_WORDS)()
        if self._libc.capget(ctypes.byref(_header()), data) != 0:
            raise OSError(ctypes.get_errno(), "capget failed")
        return [CapWord(word.effective, word.permitted, word.inheritable) for word in data]

    def set(self, words: list[CapWord]) -> None:
        data = (_CapData * _CAP_WORDS)(*(_CapData(w.effective, w.permitted, w.inheritable) for w in words))
        if self._libc.capset(ctypes.byref(_header()), data) != 0:
            raise OSError(ctypes.get_errno(), "capset failed")


def _header() -> _CapHeader:
    return _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)


def drop_capability(bit: int, *, syscalls: CapSyscalls | None = None) -> None:
    """Clear one capability from the effective, permitted and inheritable sets, then confirm it is gone.

    Without it in the permitted set it cannot come back. The bounding set keeps it (dropping that needs
    CAP_SETPCAP); with no-new-privileges an exec cannot regain it either. capset changes only the calling
    thread, so it is refused while any other thread runs."""
    if type(bit) is not int or not 0 <= bit < 32 * _CAP_WORDS:
        raise ValueError(f"not a capability bit: {bit!r}")
    syscalls = syscalls if syscalls is not None else _LibcCapSyscalls()
    threads = _thread_count()
    if threads != 1:
        raise CapabilityError(f"cannot drop capability {bit} with {threads} threads: the others would keep it")
    words = syscalls.get()
    index, offset = divmod(bit, 32)
    words[index] = words[index].without(1 << offset)
    syscalls.set(words)
    if _status_hex("CapEff") & (1 << bit) or _status_hex("CapPrm") & (1 << bit):
        raise CapabilityError(f"capability {bit} is still effective or permitted after the drop")
