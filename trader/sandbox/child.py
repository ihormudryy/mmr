"""The code that runs one authored strategy: load it, feed it bars, validate its output.

``ChildRuntime`` is pure (no fork, no privilege drop) so it is unit-tested in process.
``ForkSpawner`` forks a child, drops it to its slot uid, sets rlimits and runs the loop.
"""
from __future__ import annotations

import builtins as _builtins
import json
import math
import os
import resource
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping, Protocol

import pandas as pd

from trader.authored.allowed_api import ALLOWED_BUILTINS, ALLOWED_FROM_IMPORT_NAMES, ALLOWED_IMPORTS
from trader.authored.api import ACTIONS, AuthoredStrategy, Decision
from trader.authored.static_check import AuthoredStaticRejected, require_clean
from trader.sandbox.protocol import MAX_FRAME_BYTES, FrameError, encode_frame, validate_signal

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
SBXCHILD_GID = 20000                      # group sbxchild
SLOT_UIDS = range(20001, 20008)           # seven slots: live 3 + research 4 (ruling 26)
_EXIT_DROP_FAILED = 71                    # EX_OSERR: the privilege drop did not take
_EXIT_CHILD_CRASHED = 70                  # EX_SOFTWARE: any other failure in the child
MAX_CHILD_DETAIL_CHARS = 300              # a child's fault text is untrusted: bounded before it reaches a log


class ChildError(Exception):
    """Any fault in the authored code or its I/O: a wrong return, an exception, a blocked import."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# ── the import guard and the reduced built-ins (ruling 31) ──────────────

def _import_is_allowed(name: str, fromlist: tuple[str, ...]) -> bool:
    """Plan 1's lists, exactly (ruling 31). A dotted plain import would bind the root package."""
    if not fromlist:
        return name in ALLOWED_IMPORTS and "." not in name
    names = ALLOWED_FROM_IMPORT_NAMES.get(name, frozenset())
    return all(item in names for item in fromlist)


def _plain_fromlist(fromlist: Any) -> tuple[str, ...] | None:
    """The fromlist as a tuple of exact str, or None when it is anything else.

    Exact types only: a str subclass can hash and compare like an allowed name while the real
    import reads its true text."""
    if fromlist is None:
        return ()
    if type(fromlist) not in (tuple, list):
        return None
    items = tuple(fromlist)
    return items if all(type(item) is str for item in items) else None


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Exact module names only (review r1 #1). ``trader.authored.api`` passes only as ``from ... import``, so the
    authored module gets the API module itself, never the ``trader`` package. A from-import gets back only the
    names it asked for, never the module."""
    names = _plain_fromlist(fromlist)
    if (type(name) is not str or names is None or type(level) is not int or level != 0
            or not _import_is_allowed(name, names)):
        raise ChildError(f"import of {name!r} is not allowed")
    module = _builtins.__import__(name, None, None, names, 0)
    if not names:
        return module
    try:
        return SimpleNamespace(**{item: getattr(module, item) for item in names})
    except AttributeError as exc:
        raise ChildError(f"import of {names!r} from {name!r} failed: {exc}") from exc


def build_guarded_builtins() -> dict:
    guarded = {name: getattr(_builtins, name) for name in ALLOWED_BUILTINS}
    guarded["__import__"] = guarded_import
    # A class statement calls __build_class__ (review r1 #1). Authored source can never name it: the static
    # checker refuses every name that starts with "_".
    guarded["__build_class__"] = _builtins.__build_class__
    return guarded


def load_authored_class(source: str, class_name: str, *, skip_static_check: bool = False) -> type:
    if not skip_static_check:
        try:
            found = require_clean(source)
        except AuthoredStaticRejected as exc:
            raise ChildError(f"AUTHORED_STATIC_REJECTED:{';'.join(v.rule for v in exc.violations)}") from exc
        if found != class_name:
            raise ChildError(f"class {class_name!r} does not match the file's class {found!r}")
    module_globals: dict[str, Any] = {"__builtins__": build_guarded_builtins(), "__name__": "authored"}
    try:
        # dont_inherit: this module's `from __future__ import annotations` must not change the authored code.
        code = compile(source, "<authored>", "exec", dont_inherit=True)
        exec(code, module_globals)  # noqa: S102 — guarded builtins, no real __import__
    except ChildError:
        raise
    except Exception as exc:  # noqa: BLE001 — any load failure is a child fault
        raise ChildError(f"load failed: {type(exc).__name__}: {exc}") from exc
    authored_class = module_globals.get(class_name)
    if (not isinstance(authored_class, type) or not issubclass(authored_class, AuthoredStrategy)
            or authored_class is AuthoredStrategy):
        raise ChildError(f"{class_name!r} is not an AuthoredStrategy subclass")
    return authored_class


# ── the pure runtime ─────────────────────────────────────────────────────

def _utc_bar_time(t: Any) -> datetime:
    if not isinstance(t, datetime) or t.utcoffset() is None:
        raise ChildError("bar time must be a datetime with a UTC offset")
    return t.astimezone(timezone.utc)


def _bar_values(ohlcv: Any) -> dict[str, float]:
    if not isinstance(ohlcv, Mapping):
        raise ChildError("a bar must be a mapping of open, high, low, close, volume")
    values = {}
    for column in OHLCV_COLUMNS:
        value = ohlcv.get(column)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ChildError(f"bar {column} must be a number, got {type(value).__name__}")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ChildError(f"bar {column} is too large") from exc
        if not math.isfinite(number):
            raise ChildError(f"bar {column} must be finite, got {number!r}")
        values[column] = number
    return values


class _ConidBook:
    """One conid's strategy instance and its trimmed history."""

    def __init__(self, strategy: AuthoredStrategy):
        self.strategy = strategy
        self.rows: list[dict[str, float]] = []
        self.times: list[datetime] = []

    def add(self, when: datetime, bar: dict[str, float], lookback: int) -> pd.DataFrame:
        if self.times and when <= self.times[-1]:
            raise ChildError("bar time must strictly increase")
        self.times.append(when)
        self.rows.append(bar)
        if len(self.rows) > lookback:
            self.rows = self.rows[-lookback:]
            self.times = self.times[-lookback:]
        index = pd.DatetimeIndex(self.times, name="date")
        return pd.DataFrame(self.rows, index=index, columns=list(OHLCV_COLUMNS))


class ChildRuntime:
    """One session: one strategy instance and one trimmed history per conid (ruling 26).

    All conids of a session run the same digest, so they share one child and one uid. A fault on
    any conid ends the whole session."""

    def __init__(self, *, source: str, class_name: str, params: Mapping[str, float],
                 lookback: int, conids: tuple[int, ...], skip_static_check: bool = False):
        if not conids:
            raise ChildError("a session needs at least one conid")
        self._source = source
        self._class_name = class_name
        # Read-only, so a strategy cannot change what a later bar or another conid sees.
        self._params = MappingProxyType(dict(params))
        self._lookback = max(1, int(lookback))
        self._conids = tuple(conids)
        self._skip = skip_static_check
        self._books: dict[int, _ConidBook] = {}

    def load(self) -> None:
        cls = load_authored_class(self._source, self._class_name, skip_static_check=self._skip)
        try:
            self._books = {conid: _ConidBook(cls()) for conid in self._conids}
        except Exception as exc:  # noqa: BLE001
            raise ChildError(f"constructor failed: {type(exc).__name__}: {exc}") from exc

    def on_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        if not self._books:
            raise ChildError("session not loaded")
        book = self._books.get(conid)
        if book is None:
            raise ChildError(f"conid {conid} is not in this session")
        history = book.add(_utc_bar_time(t), _bar_values(ohlcv), self._lookback)
        try:
            decision = book.strategy.on_bar(history, self._params)
        except ChildError:
            raise
        except Exception as exc:  # noqa: BLE001 — authored code failed
            raise ChildError(f"on_bar raised {type(exc).__name__}: {exc}") from exc
        return _signal_payload(decision)


def _signal_payload(decision: Any) -> dict:
    if decision is None:
        return {"action": None, "probability": None}
    if type(decision) is not Decision:
        raise ChildError(f"on_bar must return a Decision or None, got {type(decision).__name__}")
    action = decision.action
    if type(action) is not str or action not in ACTIONS:
        raise ChildError(f"action must be BUY or SELL, got {action!r}")
    raw_probability = decision.probability
    if isinstance(raw_probability, bool) or not isinstance(raw_probability, (int, float)):
        raise ChildError("probability is not a number")
    probability = float(raw_probability)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ChildError("probability must be within 0..1")
    return {"action": action, "probability": probability}


# ── the frames between the supervisor and its child ─────────────────────

_FRAME_HEADER = struct.Struct(">I")


def send_frame(sock: socket.socket, obj: Mapping[str, Any]) -> None:
    try:
        sock.sendall(encode_frame(obj))
    except FrameError as exc:
        raise ChildError(exc.detail) from exc


def recv_frame(sock: socket.socket, timeout_s: float | None = None) -> dict:
    """One frame from the other end, which may be hostile: capped, and always a JSON object.

    ``timeout_s`` bounds the whole frame with one deadline (ruling 6): a peer that trickles one byte
    at a time cannot stretch it, as it could a per-recv socket timeout."""
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    try:
        (length,) = _FRAME_HEADER.unpack(_recv_exact(sock, _FRAME_HEADER.size, deadline))
        if length > MAX_FRAME_BYTES:
            raise ChildError(f"child frame of {length} bytes over the {MAX_FRAME_BYTES} cap")
        body = _recv_exact(sock, length, deadline)
    finally:
        if deadline is not None:
            sock.settimeout(None)
    try:
        obj = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise ChildError(f"child frame is not JSON: {type(exc).__name__}") from exc
    if not isinstance(obj, dict):
        raise ChildError("child frame is not a JSON object")
    return obj


def _recv_exact(sock: socket.socket, count: int, deadline: float | None) -> bytes:
    chunks = []
    while count:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChildError("child reply timed out")
            sock.settimeout(remaining)
        try:
            chunk = sock.recv(count)
        except TimeoutError as exc:          # socket.timeout is TimeoutError since 3.10
            raise ChildError("child reply timed out") from exc
        if not chunk:
            raise ChildError("child pipe closed")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _raise_for_child_fault(reply: Mapping[str, Any], fallback: str) -> None:
    ok = reply.get("ok")
    if ok is True:
        return
    detail = reply.get("detail")
    if ok is False and isinstance(detail, str):
        raise ChildError(detail[:MAX_CHILD_DETAIL_CHARS])
    raise ChildError(fallback)


def signal_from_reply(reply: Mapping[str, Any]) -> dict:
    """The SIGNAL payload in a child's reply to a bar, checked against the protocol."""
    _raise_for_child_fault(reply, "child reply is out of contract")
    signal = reply.get("signal")
    if not isinstance(signal, dict) or set(signal) != {"action", "probability"}:
        raise ChildError("child reply carries no signal")
    try:
        validate_signal(signal)
    except FrameError as exc:
        raise ChildError(exc.detail) from exc
    probability = signal["probability"]
    return {"action": signal["action"], "probability": None if probability is None else float(probability)}


# ── the real forked child ────────────────────────────────────────────────

@dataclass(frozen=True)
class SpawnSpec:
    source: str          # the already-verified source bytes decoded to str
    class_name: str
    params: Mapping[str, float]
    lookback: int
    conids: tuple[int, ...]   # one strategy instance per conid in this child (ruling 26)
    slot_uid: int        # always a real slot uid (20001..20007)
    slot_dir: str        # /tmp/s<N>, owned by slot_uid
    child_mem_bytes: int
    skip_static_check: bool = False   # True only for a probe (ruling 11); the store path is always False


class ChildHandle(Protocol):
    """One session's child. Any ChildError from ``send_bar`` ends the session: the caller must ``close()``."""
    pid: int

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict: ...

    def close(self) -> None: ...


class Spawner(Protocol):
    def spawn(self, spec: SpawnSpec) -> ChildHandle: ...


class ForkSpawner:
    """Fork one child per session. Production always uses this (ruling 12)."""

    def __init__(self, open_timeout_s: float = 2.0, bar_timeout_s: float = 2.0):
        self._open_timeout = open_timeout_s   # ruling 6: class load is bounded too
        self._bar_timeout = bar_timeout_s     # backstop; the server's 2 s wall timeout also kills the child

    def spawn(self, spec: SpawnSpec) -> ChildHandle:
        parent_sock, child_sock = socket.socketpair()
        pid = os.fork()
        if pid == 0:
            _run_forked_child(spec, child_sock)   # never returns
        child_sock.close()
        try:   # a child that hangs or trickles in load must not block the server
            _raise_for_child_fault(recv_frame(parent_sock, self._open_timeout), "child load reply is out of contract")
        except (ChildError, OSError) as exc:
            _kill_and_reap(pid)
            parent_sock.close()
            raise ChildError(getattr(exc, "detail", f"child load failed: {exc}")) from exc
        return _ForkedChild(pid=pid, pipe=_ChildPipe(sock=parent_sock, reply_timeout_s=self._bar_timeout))


def _run_forked_child(spec: SpawnSpec, sock: socket.socket) -> None:
    """The child's whole life. It never returns into the supervisor's code or its event loop."""
    code = _EXIT_CHILD_CRASHED
    try:
        close_fds_except({sock.fileno()})
        try:
            _drop_privileges(spec)
        except Exception:  # noqa: BLE001 — any failure here leaves the child privileged
            _send_fault_quietly(sock, "PRIVILEGE_DROP_FAILED")
            code = _EXIT_DROP_FAILED
            return
        try:
            _set_child_rlimits(spec)
        except Exception:  # noqa: BLE001
            _send_fault_quietly(sock, "CHILD_LIMITS_FAILED")
            return
        code = _serve_session(spec, sock)
    except BaseException:  # noqa: BLE001 — a crashed child must not unwind into the supervisor
        code = _EXIT_CHILD_CRASHED
    finally:
        os._exit(code)


def _send_fault_quietly(sock: socket.socket, detail: str) -> None:
    """Tell the parent why the child stops, when the pipe still works. Only a fixed code, no other detail."""
    try:
        send_frame(sock, {"ok": False, "detail": detail})
    except (ChildError, OSError):
        pass


def close_fds_except(keep: set[int]) -> None:
    """Close every inherited fd (listening sockets, other sessions' pipes) except stdout, stderr and ``keep``.

    By range, not by listing /proc/self/fd, so nothing stays open when /proc cannot be read. An unbounded
    fd limit (sysconf gives -1) would make the last closerange a silent no-op, so it is refused first."""
    fd_limit = os.sysconf("SC_OPEN_MAX")
    if fd_limit <= 0:
        raise RuntimeError(f"cannot close inherited fds: SC_OPEN_MAX is {fd_limit}")
    keep = keep | {1, 2}
    low = 0
    for fd in sorted(keep):
        os.closerange(low, fd)
        low = fd + 1
    os.closerange(low, fd_limit)


class _PrivilegeDropFailed(Exception):
    pass


class _ProcStatusUnreadable(Exception):
    pass


def _drop_privileges(spec: SpawnSpec) -> None:
    if type(spec.slot_uid) is not int or spec.slot_uid not in SLOT_UIDS:
        raise _PrivilegeDropFailed(f"{spec.slot_uid!r} is not a slot uid")
    os.setgroups([])
    os.setresgid(SBXCHILD_GID, SBXCHILD_GID, SBXCHILD_GID)
    os.setresuid(spec.slot_uid, spec.slot_uid, spec.slot_uid)
    if os.getresuid() != (spec.slot_uid,) * 3 or os.getresgid() != (SBXCHILD_GID,) * 3 or os.getgroups():
        raise _PrivilegeDropFailed("uid, gid or groups did not change")
    if int(_status_field("CapEff"), 16) != 0:   # the child must keep no capability (spec 7.5)
        raise _PrivilegeDropFailed("a capability survived the drop")
    # Only after the drop: the 0700 slot dir belongs to the slot uid, and the supervisor has no DAC caps.
    os.chdir(spec.slot_dir)
    os.environ["HOME"] = spec.slot_dir
    os.environ["TMPDIR"] = spec.slot_dir


def _set_child_rlimits(spec: SpawnSpec) -> None:
    as_limit = _mapped_bytes() + spec.child_mem_bytes          # ruling 3: mapped at fork + 256 MiB
    resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (4 * 3600, 4 * 3600))   # ruling 4: backstop, soft = hard
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))              # ruling 5: no thread, no fork
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _mapped_bytes() -> int:
    """VmSize from /proc. No guess: a child that cannot read it exits instead of running with a wrong limit."""
    return int(_status_field("VmSize").split()[0]) * 1024


def _status_field(name: str) -> str:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith(f"{name}:"):
                return line.split(":", 1)[1].strip()
    raise _ProcStatusUnreadable(f"/proc/self/status has no {name}")


def _serve_session(spec: SpawnSpec, sock: socket.socket) -> int:
    """Load, then answer bars until CLOSE or the parent goes away. Returns the child's exit code:
    0 for a normal end, non-zero after a load or bar fault."""
    try:
        runtime = ChildRuntime(source=spec.source, class_name=spec.class_name,
                               params=spec.params, lookback=spec.lookback, conids=spec.conids,
                               skip_static_check=spec.skip_static_check)
        runtime.load()
    except ChildError as exc:
        send_frame(sock, {"ok": False, "detail": exc.detail})
        return _EXIT_CHILD_CRASHED
    send_frame(sock, {"ok": True})
    while True:
        try:
            request = recv_frame(sock)
        except ChildError:
            return 0
        if request.get("type") == "CLOSE":
            return 0
        try:
            payload = runtime.on_bar(*_bar_request(request))
        except ChildError as exc:
            send_frame(sock, {"ok": False, "detail": exc.detail})
            return _EXIT_CHILD_CRASHED
        send_frame(sock, {"ok": True, "signal": payload})


def _bar_request(request: Mapping[str, Any]) -> tuple[int, datetime, Any]:
    try:
        return int(request["conid"]), datetime.fromisoformat(request["t"]), request["ohlcv"]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ChildError(f"malformed bar request: {type(exc).__name__}") from exc


def _require_child_pid(pid: Any) -> int:
    """One real child: kill(0) would signal the process group and kill(-1) every process the uid owns."""
    if type(pid) is not int or pid <= 0:
        raise ValueError(f"pid must be a positive int, got {pid!r}")
    return pid


def _kill_and_reap(pid: int) -> None:
    _require_child_pid(pid)
    try:
        os.kill(pid, 9)   # SIGKILL: this is what CAP_KILL is for (own children only)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@dataclass
class _ChildPipe:
    """The parent's end of one child's socket. After any failure it stays dead: a later bar fails fast."""
    sock: socket.socket
    reply_timeout_s: float
    dead: bool = False

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        if self.dead:
            raise ChildError("child is no longer usable after an earlier failure")
        try:
            send_frame(self.sock, {"type": "BAR", "conid": conid,
                                   "t": t.astimezone(timezone.utc).isoformat(), "ohlcv": dict(ohlcv)})
            return signal_from_reply(recv_frame(self.sock, self.reply_timeout_s))
        except ChildError:
            self.dead = True
            raise
        except OSError as exc:   # the child died or was killed mid-bar
            self.dead = True
            raise ChildError(f"child pipe failed: {exc}") from exc

    def close(self) -> None:
        self.dead = True
        try:
            self.sock.close()
        except OSError:
            pass


@dataclass
class _ForkedChild:
    """A forked child. On any ChildError from ``send_bar`` the caller must ``close()`` it: a fault, a timeout
    or a dead pipe all end the session, and only ``close()`` kills the process and frees the slot."""
    pid: int
    pipe: _ChildPipe
    reaped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _require_child_pid(self.pid)

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        return self.pipe.send_bar(conid, t, ohlcv)

    def close(self) -> None:
        """Idempotent. Once reaped, the pid is never signalled again: the kernel may have given it to another
        process, and the supervisor runs as root."""
        try:
            if not self.reaped:
                # Kill first: a child looping or ignoring CLOSE must die so its slot and pid free (CAP_KILL).
                _kill_and_reap(self.pid)
                self.reaped = True
        finally:
            self.pipe.close()
