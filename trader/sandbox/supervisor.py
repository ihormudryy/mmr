"""The sandbox supervisor: pools of slots, one child per session, store re-hash before load.

Transport-free: ``handle`` takes a parsed ``Request`` and returns a reply dict. The asyncio
server (sandbox_service) wires sockets to it and reports a dropped connection.
"""
from __future__ import annotations

import ast
import logging
import os
import resource
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from trader.authored.keys import source_digest
from trader.authored.static_check import (MAX_SOURCE_BYTES, AuthoredStaticRejected, _parse_quietly,
                                          require_clean)
from trader.sandbox import protocol as proto
from trader.sandbox.caps import SLOT_DIR_MODE, slot_dir
from trader.sandbox.child import (SBXCHILD_GID, SLOT_UIDS, ChildError, ChildHandle, SpawnSpec, Spawner,
                                  _kill_and_reap, close_fds_except, reset_signals_after_fork)

log = logging.getLogger("mmr.sandbox")

PROBE_DIR = Path(__file__).resolve().parent / "probes"
ESCAPE_DIR = Path(__file__).resolve().parents[2] / "tests" / "authored" / "escapes"

# Pools (ruling 26, spec 7.5 as amended): live has one session per authored deployment, and the
# trader caps deployed authored strategies at max_active <= 3; research has 4 one-conid sessions.
LIVE_POOL_SIZE = 3
RESEARCH_POOL_SIZE = 4
MAX_CONIDS_PER_SESSION = {"live": proto.MAX_SESSION_CONIDS, "research": 1}
LOOKBACK_CEILING = 2000   # the child narrows it to the loaded class's LOOKBACK_BARS (spec 9)

# Slot uids 20001..20007: live gets the first 3, research the next 4 (spec 9 as amended).
_POOL_UIDS = {"live": tuple(SLOT_UIDS[:LIVE_POOL_SIZE]),
              "research": tuple(SLOT_UIDS[LIVE_POOL_SIZE:LIVE_POOL_SIZE + RESEARCH_POOL_SIZE])}
if LIVE_POOL_SIZE + RESEARCH_POOL_SIZE != len(SLOT_UIDS):
    raise RuntimeError("the pools must use exactly the seven slot uids")

__all__ = ["ESCAPE_DIR", "LIVE_POOL_SIZE", "MAX_CONIDS_PER_SESSION", "PROBE_DIR", "Pool", "RESEARCH_POOL_SIZE",
           "SBXCHILD_GID", "Slot", "Supervisor", "wipe_slot_as_owner"]


class _OpenRefused(Exception):
    """An OPEN answered with an ERROR reply before any slot was taken."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.reply = _error(code, detail)


@dataclass
class Slot:
    uid: int
    directory: str


@dataclass
class _Session:
    session_id: str
    pool: str
    conn_id: int
    slot: Slot
    handle: Optional[ChildHandle] = None    # None while the child is being spawned (OPEN in progress)
    conids: tuple[int, ...] = ()
    last_t: dict = field(default_factory=dict)   # conid -> datetime of its last accepted bar
    busy: bool = False     # a bar round-trip is in flight (ruling 25)


@dataclass
class Pool:
    name: str
    free_slots: list[Slot]
    quarantined: list[Slot] = field(default_factory=list)
    size: int = 0


class Supervisor:
    def __init__(self, *, spawners: Mapping[str, Spawner], pool_sizes: Mapping[str, int],
                 store_dir: str, image_digest: str, posture: Mapping[str, object],
                 child_mem_bytes: int = 256 * 1024 * 1024,
                 wiper: Optional[Callable[["Slot"], bool]] = None):
        self._spawners = dict(spawners)
        self._wiper = wiper or wipe_slot_as_owner     # ruling 28: the slot uid empties its own directory
        self._store = Path(store_dir)
        self._image_digest = image_digest
        self._posture = dict(posture)
        self._child_mem = child_mem_bytes
        self._boot_id = secrets.token_hex(8)
        self._lock = threading.Lock()   # the server offloads BAR to a thread; guard shared state (ruling 25)
        self._sessions: dict[str, _Session] = {}
        self._pools = {name: _build_pool(name, size, self._spawners) for name, size in pool_sizes.items()}

    # ── requests ──────────────────────────────────────────────────────────
    # Threading (ruling 25): the server offloads every request except PING to a thread, so the
    # event loop never blocks. The lock below is held only for O(1) bookkeeping (slot pop/append,
    # session insert/remove), never across a fork, a class load or a bar round-trip. So a slow
    # research bar never holds the lock, and live and research stay independent.
    def handle(self, pool_name: str, conn_id: int, request: proto.Request) -> dict:
        if request.type == "PING":
            return self.pong(pool_name)
        if request.type == "OPEN":
            return self._open_store(pool_name, conn_id, request)
        if request.type == "OPEN_PROBE":
            return self._open_probe(pool_name, conn_id, request)
        if request.type == "BAR":
            return self._bar(conn_id, request)
        if request.type == "CLOSE":
            return self._close(conn_id, request)
        return _error(proto.SANDBOX_BAD_REQUEST, f"unknown type {request.type!r}")

    def pong(self, pool_name: str) -> dict:
        """Lock-free, so health always answers (ruling 25)."""
        pool = self._pools.get(pool_name)
        return {"type": "PONG", "v": proto.PROTOCOL_VERSION, "image_digest": self._image_digest,
                "boot_id": self._boot_id, "pid": os.getpid(), "pool": pool_name,
                "free": len(pool.free_slots) if pool else 0,
                "size": pool.size if pool else 0, "posture": self._posture,
                # Owner ruling: prove-isolation refuses while a live session is open (ruling 29).
                "live_open": sum(1 for session in list(self._sessions.values()) if session.pool == "live")}

    def drop_connection(self, conn_id: int) -> None:
        with self._lock:
            ended = [self._sessions.pop(s.session_id) for s in list(self._sessions.values())
                     if s.conn_id == conn_id]
        for session in ended:
            if session.handle is not None:   # a session still opening is finished by its opener
                self._finish(session)

    # ── open: the store path ────────────────────────────────────────────────
    def _open_store(self, pool_name: str, conn_id: int, request: proto.Request) -> dict:
        try:
            self._check_open(pool_name, conn_id, request.conids)
            source = self._read_store_source(request.digest)
        except _OpenRefused as refused:
            return refused.reply
        try:
            found = require_clean(source)
        except AuthoredStaticRejected as exc:
            return _static_rejected(";".join(v.rule for v in exc.violations))
        if found != request.class_name:
            return _error(proto.SANDBOX_DIGEST_MISMATCH, "class does not match the stored file")
        return self._spawn(pool_name, conn_id, source, request.class_name,
                           request.params, request.conids, skip_static_check=False)

    def _read_store_source(self, digest: str) -> str:
        """The stored bytes, re-hashed against the digest. Exactly those bytes go to the checker and the child."""
        path = self._store / f"{digest.split(':')[1]}.py"
        raw = _read_regular_file(path, MAX_SOURCE_BYTES)
        if source_digest(raw) != digest:
            raise _OpenRefused(proto.SANDBOX_DIGEST_MISMATCH, "stored bytes do not match the digest")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Never errors="replace": the checker and the child must see the text that was hashed.
            raise _OpenRefused(proto.SANDBOX_ERROR, "AUTHORED_STATIC_REJECTED:unparseable") from exc

    # ── open: a probe (ruling 11) ───────────────────────────────────────────
    def _open_probe(self, pool_name: str, conn_id: int, request: proto.Request) -> dict:
        if pool_name != "research":   # ruling 32: hostile probes never share the live pool
            return _error(proto.SANDBOX_BAD_REQUEST, "probes are research-only")
        try:
            self._check_open(pool_name, conn_id, request.conids)
            source = _read_probe_source(request.kind, request.name)
            class_name = _only_class(source)
        except _OpenRefused as refused:
            return refused.reply
        return self._spawn(pool_name, conn_id, source, class_name, {}, request.conids,
                           skip_static_check=True)

    # ── open: both paths ────────────────────────────────────────────────────
    def _check_open(self, pool_name: str, conn_id: int, conids: tuple[int, ...]) -> None:
        """Refusals that need no slot. ``_reserve`` re-checks the connection under the lock."""
        if pool_name not in self._pools:
            raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, f"unknown pool {pool_name!r}")
        limit = MAX_CONIDS_PER_SESSION[pool_name]
        if len(conids) > limit:
            raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, f"a {pool_name} session takes at most {limit} conids")
        if self._connection_holds_session(conn_id):
            raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, "this connection already holds a session")

    def _connection_holds_session(self, conn_id: int) -> bool:
        with self._lock:
            return any(session.conn_id == conn_id for session in self._sessions.values())

    def _spawn(self, pool_name, conn_id, source, class_name, params, conids, *, skip_static_check) -> dict:
        reserved = self._reserve(pool_name, conn_id, conids, is_probe=skip_static_check)
        if isinstance(reserved, dict):
            return reserved
        # Fork and load OUTSIDE the lock (a hanging load must not block other pools).
        spec = SpawnSpec(source=source, class_name=class_name, params=params, lookback=LOOKBACK_CEILING,
                         conids=tuple(conids), slot_uid=reserved.slot.uid, slot_dir=reserved.slot.directory,
                         child_mem_bytes=self._child_mem, skip_static_check=skip_static_check)
        try:
            handle = self._spawners[pool_name].spawn(spec)
        except Exception as exc:  # noqa: BLE001 — any spawn failure must give the slot back
            self._forget(reserved)
            self._release(pool_name, reserved.slot)
            detail = exc.detail if isinstance(exc, ChildError) else f"spawn failed: {type(exc).__name__}"
            if not isinstance(exc, ChildError):
                log.error("spawning a %s child failed", pool_name, exc_info=True)
            return _error(proto.SANDBOX_ERROR, detail)
        with self._lock:
            still_open = self._sessions.get(reserved.session_id) is reserved
            if still_open:
                reserved.handle = handle
        if not still_open:   # the connection dropped while the child loaded (ruling 7)
            self._finish_with(handle, reserved)
            return _error(proto.SANDBOX_ERROR, "connection dropped during open")
        # The build id travels with OPENED, so a caller knows which image this very session runs on (ruling 29).
        return {"type": "OPENED", "v": proto.PROTOCOL_VERSION, "session": reserved.session_id,
                "image_digest": self._image_digest}

    def _reserve(self, pool_name: str, conn_id: int, conids: tuple[int, ...], *,
                 is_probe: bool) -> _Session | dict:
        """Take a slot and enter the session (without a child yet) in one step, so a drop during the
        spawn is seen and one connection never holds two sessions (ruling 7)."""
        with self._lock:
            if any(session.conn_id == conn_id for session in self._sessions.values()):
                return _error(proto.SANDBOX_BAD_REQUEST, "this connection already holds a session")
            # Ruling 29: a probe never starts beside a live child, open or still opening (it is in the table).
            if is_probe and any(session.pool == "live" for session in self._sessions.values()):
                return _error(proto.SANDBOX_BUSY, "live sessions open")
            pool = self._pools[pool_name]
            if not pool.free_slots:
                return _error(proto.SANDBOX_BUSY, "no free slot")
            session = _Session(session_id="s" + secrets.token_hex(8), pool=pool_name, conn_id=conn_id,
                               slot=pool.free_slots.pop(), conids=tuple(conids))
            self._sessions[session.session_id] = session
        return session

    def _forget(self, session: _Session) -> None:
        with self._lock:
            if self._sessions.get(session.session_id) is session:
                del self._sessions[session.session_id]

    # ── bars and close ──────────────────────────────────────────────────────
    def _bar(self, conn_id: int, request: proto.Request) -> dict:
        with self._lock:
            session = self._sessions.get(request.session)
            if session is None or session.conn_id != conn_id or session.handle is None:
                return _error(proto.SANDBOX_BAD_REQUEST, proto.UNKNOWN_SESSION_DETAIL)
            if session.busy:
                return _error(proto.SANDBOX_BAD_REQUEST, "a bar is already in flight")
            # Caller errors are checked here, before the child sees the bar, and never end the session (ruling 8).
            if request.conid not in session.conids:
                return _error(proto.SANDBOX_BAD_REQUEST, f"conid {request.conid} is not in this session")
            last_t = session.last_t.get(request.conid)
            if last_t is not None and request.t <= last_t:
                return _error(proto.SANDBOX_BAD_REQUEST, "bar time must strictly increase per conid")
            session.busy = True
            handle = session.handle
        # The child round-trip runs OUTSIDE the lock; a kill (drop_connection) can interrupt it.
        # Any failure on any conid ends the whole session (ruling 26) and closes the child.
        try:
            payload = handle.send_bar(request.conid, request.t, request.ohlcv)
            proto.validate_signal(payload)
        except Exception as exc:  # noqa: BLE001 — a fault, a dead pipe or a bug: the child is done
            self._end(session)
            if isinstance(exc, (ChildError, proto.FrameError)):
                return _error(proto.SANDBOX_ERROR, exc.detail)
            log.error("bar on session %s failed", session.session_id, exc_info=True)
            return _error(proto.SANDBOX_ERROR, f"child failed: {type(exc).__name__}")
        with self._lock:
            if self._sessions.get(request.session) is not session:   # dropped or killed during the call
                return _error(proto.SANDBOX_ERROR, "session ended during the bar")
            session.last_t[request.conid] = request.t
            session.busy = False
        return {"type": "SIGNAL", "v": proto.PROTOCOL_VERSION, "session": request.session,
                "conid": request.conid, "t": request.t.isoformat(), "action": payload["action"],
                "probability": payload["probability"]}

    def _close(self, conn_id: int, request: proto.Request) -> dict:
        with self._lock:
            session = self._sessions.get(request.session)
            if session is None or session.conn_id != conn_id or session.handle is None:
                return _error(proto.SANDBOX_BAD_REQUEST, proto.UNKNOWN_SESSION_DETAIL)
            del self._sessions[session.session_id]
        self._finish(session)
        return {"type": "CLOSED", "v": proto.PROTOCOL_VERSION}

    def _end(self, session: _Session) -> None:
        """Take the session out of the table and finish it, unless a drop or close already did."""
        with self._lock:
            ours = self._sessions.get(session.session_id) is session
            if ours:
                del self._sessions[session.session_id]
        if ours:
            self._finish(session)

    def _finish(self, session: _Session) -> None:
        """The session already left the table (so no bar reaches it). Outside the lock (ruling 25)."""
        self._finish_with(session.handle, session)

    def _finish_with(self, handle: ChildHandle, session: _Session) -> None:
        """Kill the child first, then wipe and free its slot: a wipe must never race a live child."""
        try:
            handle.close()   # SIGKILL + reap; also unblocks an in-flight send_bar
        except Exception:  # noqa: BLE001 — closing a dead child must not raise
            log.warning("closing session %s raised", session.session_id, exc_info=True)
        self._release(session.pool, session.slot)

    def _release(self, pool_name: str, slot: Slot) -> None:
        try:
            clean = self._wiper(slot) is True   # may fork a helper: never under the lock (rulings 25, 28)
        except Exception:  # noqa: BLE001 — a wiper that fails leaves a dirty slot
            log.error("wiping slot %s raised", slot.uid, exc_info=True)
            clean = False
        with self._lock:
            pool = self._pools[pool_name]
            if clean:
                pool.free_slots.append(slot)
            else:
                pool.quarantined.append(slot)   # ruling 13: never reuse a slot we could not wipe
        if not clean:
            log.error("quarantined slot %s: wipe failed", slot.uid)


def _build_pool(name: str, size: int, spawners: Mapping[str, Spawner]) -> Pool:
    if name not in _POOL_UIDS:
        raise ValueError(f"unknown pool {name!r}; pools are {sorted(_POOL_UIDS)}")
    if name not in spawners:
        raise ValueError(f"pool {name!r} has no spawner")
    uids = _POOL_UIDS[name]
    if type(size) is not int or not 0 <= size <= len(uids):
        raise ValueError(f"pool {name!r} has {len(uids)} slot uids, cannot size it {size!r}")
    slots = [Slot(uid=uid, directory=slot_dir(uid)) for uid in uids[:size]]
    return Pool(name=name, free_slots=slots, size=size)


def _error(code: str, detail: str) -> dict:
    return {"type": "ERROR", "v": proto.PROTOCOL_VERSION, "code": code, "detail": detail}


def _static_rejected(rules: str) -> dict:
    return _error(proto.SANDBOX_ERROR, f"AUTHORED_STATIC_REJECTED:{rules}")


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    """Read a store file the trader wrote: a regular file only (no symlink, no FIFO that would block), and
    no more than ``max_bytes``. A missing or wrong-kind file is a digest mismatch (ruling 9)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise _OpenRefused(proto.SANDBOX_DIGEST_MISMATCH, "store file not found or not a regular file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _OpenRefused(proto.SANDBOX_DIGEST_MISMATCH, "store file is not a regular file")
        if info.st_size > max_bytes:
            raise _OpenRefused(proto.SANDBOX_ERROR, "AUTHORED_STATIC_REJECTED:oversize")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(max_bytes + 1)
    except OSError:
        raise _OpenRefused(proto.SANDBOX_DIGEST_MISMATCH, "store file could not be read") from None
    finally:
        os.close(fd)
    if len(raw) > max_bytes:   # grew after the fstat
        raise _OpenRefused(proto.SANDBOX_ERROR, "AUTHORED_STATIC_REJECTED:oversize")
    return raw


def _read_probe_source(kind: str, name: str) -> str:
    """A probe resolves only inside the two directories shipped in the image (ruling 11)."""
    base = (PROBE_DIR if kind == "probe" else ESCAPE_DIR).resolve()
    path = (base / f"{name}.py").resolve()
    if path.parent != base:
        raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, "probe name escapes its directory")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, f"probe {name!r} not found") from None


def _only_class(source: str) -> str:
    """The probe's one top-level class. Parsed, never run here."""
    try:
        tree = _parse_quietly(source)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, "probe does not parse") from None
    names = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(names) != 1:
        raise _OpenRefused(proto.SANDBOX_BAD_REQUEST, f"a probe needs exactly one class, found {len(names)}")
    return names[0]


_WIPE_CPU_SECONDS = 5
_WIPE_TIMEOUT_S = 5.0
_WIPE_POLL_S = 0.01


def wipe_slot_as_owner(slot: Slot, *, timeout_s: float = _WIPE_TIMEOUT_S) -> bool:
    """Empty a slot directory as its own uid (ruling 28, review r1 #2).

    The serving supervisor has only SETUID, SETGID and KILL: no CAP_CHOWN or CAP_DAC_*, so it cannot read a
    0700 directory its child owns. A short-lived helper forked and dropped to the slot uid can. It runs only
    chmod (on dirs it owns), unlink and rmdir, then exits; its exit status is the answer. A failure quarantines the slot (ruling 13)."""
    if not Path(slot.directory).exists():
        return True
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            reset_signals_after_fork()
            resource.setrlimit(resource.RLIMIT_CPU, (_WIPE_CPU_SECONDS, _WIPE_CPU_SECONDS))
            # As the slot uid it must hold no listening socket and no other session's pipe.
            close_fds_except(set())
            os.setgroups([])
            os.setresgid(SBXCHILD_GID, SBXCHILD_GID, SBXCHILD_GID)
            os.setresuid(slot.uid, slot.uid, slot.uid)
            _unlock_tree(slot.directory)
            _empty(slot.directory)
            code = 0
        except BaseException:  # noqa: BLE001 — any failure is a dirty slot
            code = 1
        finally:
            os._exit(code)
    return _helper_succeeded(pid, timeout_s)


def _helper_succeeded(pid: int, timeout_s: float) -> bool:
    """Wait for the wipe helper at most ``timeout_s``. A helper that has not exited by then is killed and the
    slot counts as dirty: the supervisor never blocks forever on a wipe."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return False
        if done == pid:
            return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        if time.monotonic() >= deadline:
            log.error("wipe helper %s did not finish in %ss: killed", pid, timeout_s)
            _kill_and_reap(pid)
            return False
        time.sleep(_WIPE_POLL_S)


def _unlock_tree(directory: str) -> None:
    """Give the slot dir and every dir under it mode 0700 again, top-down, so the wipe can list and empty them.

    The child owns its slot and may chmod it to 000 or set the sticky bit; the helper is the same owner, so this
    needs no capability. A dir another uid owns cannot be changed: that raises, and the slot is quarantined."""
    def fail(error: OSError) -> None:
        raise error

    os.chmod(directory, SLOT_DIR_MODE)
    for root, dirs, _files in os.walk(directory, onerror=fail, followlinks=False):
        for name in dirs:   # before os.walk lists them
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, SLOT_DIR_MODE)


def _empty(directory: str) -> None:
    """Remove everything under ``directory`` (not the directory itself), bottom-up, never following a symlink.
    os.walk is iterative in 3.12, so a deep tree cannot exhaust the stack."""
    def fail(error: OSError) -> None:
        raise error

    for root, dirs, files in os.walk(directory, topdown=False, onerror=fail, followlinks=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            path = os.path.join(root, name)
            if os.path.islink(path):
                os.unlink(path)
            else:
                os.rmdir(path)
    if os.listdir(directory):
        raise OSError(f"{directory} is not empty after the wipe")
