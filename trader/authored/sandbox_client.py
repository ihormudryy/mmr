"""Synchronous client for the sandbox. One session per (strategy, run): a live session holds
every conid of one deployment, a research session one conid (ruling 26).

Error mapping (spec 7.6):
- SandboxUnavailable: SANDBOX_BUSY, SANDBOX_UNAVAILABLE (the sandbox itself failed, ruling 34), and a socket
  that is unreachable, slow, dropped or out of contract. The strategy is not to blame.
- SandboxFault: SANDBOX_ERROR and SANDBOX_DIGEST_MISMATCH. The strategy failed; the session is over.
- SandboxProtocolError: SANDBOX_BAD_REQUEST, and anything the client refuses to send (ruling 8).
  A refused bar never reaches the sandbox and never ends the session.

Requests are checked with the server's own parser before they are sent, so the two sides cannot
disagree about what is valid.
"""
from __future__ import annotations

import numbers
import socket
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from trader.authored.api import Decision
from trader.sandbox import protocol as proto

_HEADER = struct.Struct(">I")
_OHLCV_NAMES = ("open", "high", "low", "close", "volume")

# A timed-out BAR is answered only after the sandbox killed the child and wiped its slot (about 7 s).
DEFAULT_REPLY_TIMEOUT_S = 15.0


class SandboxUnavailable(Exception):
    """The sandbox is busy, unreachable, slow or out of contract. Not a fault of the strategy."""


class SandboxFault(Exception):
    """The strategy failed in the sandbox. ``code`` is SANDBOX_ERROR or SANDBOX_DIGEST_MISMATCH."""

    def __init__(self, detail: str, *, code: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __reduce__(self):
        # The default rebuilds an exception from its positional args only; a worker process must raise it whole.
        return _sandbox_fault, (self.detail, self.code)


def _sandbox_fault(detail: str, code: str) -> SandboxFault:
    return SandboxFault(detail, code=code)


class SandboxProtocolError(Exception):
    """The caller sent something out of contract (SANDBOX_BAD_REQUEST), or used a session that is over."""


@dataclass(frozen=True)
class SandboxInfo:
    image_digest: str
    boot_id: str
    pool: str
    free: int
    size: int
    posture: Mapping[str, Any]
    # Open live sessions. None means unknown (absent or not a count in PONG): a caller that needs
    # "no live session" (prove-isolation, Plan 3) must refuse on None, never read it as 0.
    live_open: Optional[int] = None


class SandboxClient:
    """``connect_timeout_s`` bounds connecting and a PING. ``reply_timeout_s`` bounds the whole reply
    to an OPEN, BAR or CLOSE, which can be slow: the sandbox answers a timed-out bar only after it
    killed the child and wiped its slot."""

    def __init__(self, socket_path: str, *, connect_timeout_s: float = 5.0,
                 reply_timeout_s: float = DEFAULT_REPLY_TIMEOUT_S):
        if connect_timeout_s <= 0 or reply_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        self._path = socket_path
        self._connect_timeout = connect_timeout_s
        self._reply_timeout = reply_timeout_s

    def ping(self) -> SandboxInfo:
        sock = self._connect()
        try:
            reply = _exchange(sock, {"v": proto.PROTOCOL_VERSION, "type": "PING"}, self._connect_timeout)
        finally:
            sock.close()
        _raise_for(reply)
        return _sandbox_info(reply)

    def open_session(self, digest: str, class_name: str, params: Mapping[str, float],
                     bar_size: str, conids: Sequence[int]) -> "SandboxSession":
        return self._open({"v": proto.PROTOCOL_VERSION, "type": "OPEN", "digest": digest,
                           "class": class_name, "params": dict(params), "bar_size": bar_size,
                           "conids": list(conids)})

    def open_probe(self, kind: str, name: str) -> "SandboxSession":
        return self._open({"v": proto.PROTOCOL_VERSION, "type": "OPEN_PROBE", "kind": kind, "name": name,
                           "conids": [proto.PROBE_CONID]})

    def _open(self, request: Mapping[str, Any]) -> "SandboxSession":
        _check_request(request)
        sock = self._connect()
        try:
            reply = _exchange(sock, request, self._reply_timeout)
            _raise_for(reply)
            session_id, image_digest = reply.get("session"), reply.get("image_digest")
            if (reply.get("type") != "OPENED" or not isinstance(image_digest, str)
                    or not isinstance(session_id, str) or not proto.SESSION_RE.fullmatch(session_id)):
                raise SandboxUnavailable(f"unexpected reply to OPEN: {reply!r}")
        except BaseException:
            sock.close()
            raise
        return SandboxSession(sock, session_id, tuple(request["conids"]), image_digest, self._reply_timeout)

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout)
        try:
            sock.connect(self._path)
        except OSError as exc:
            sock.close()
            raise SandboxUnavailable(f"cannot reach the sandbox at {self._path}: {exc}") from exc
        return sock


class SandboxSession:
    """Not thread-safe: the sandbox refuses a second bar in flight on one session (ruling 25).

    A session ends on ``close()``, on a fault, and on any transport failure; after that every call
    raises SandboxProtocolError. A refused bar (SandboxProtocolError) leaves it usable."""

    def __init__(self, sock: socket.socket, session_id: str, conids: tuple[int, ...],
                 image_digest: str, reply_timeout_s: float):
        self._sock = sock
        self._id = session_id
        self._reply_timeout = reply_timeout_s
        self._is_open = True
        self.conids = conids
        self.image_digest = image_digest     # the build this session runs on, from OPENED (ruling 29)

    def bar(self, conid: int, t: datetime, open: float, high: float, low: float, close: float,
            volume: float) -> Decision | None:
        if not self._is_open:
            raise SandboxProtocolError("session is closed")
        if conid not in self.conids:
            raise SandboxProtocolError(f"conid {conid} is not in this session")
        when = _utc(t)
        request = {"v": proto.PROTOCOL_VERSION, "type": "BAR", "session": self._id, "conid": conid,
                   "t": when.isoformat(), **_ohlcv(open=open, high=high, low=low, close=close, volume=volume)}
        _check_request(request)
        try:
            reply = _exchange(self._sock, request, self._reply_timeout)
        except SandboxProtocolError:   # refused before anything was sent
            raise
        except BaseException:   # also an interrupt: the stream may be half read, so the session is over
            self._end()
            raise
        if reply.get("type") == "SIGNAL":
            return self._decision(reply, conid, when)
        if not _leaves_session_usable(reply):
            self._end()
        _raise_for(reply)
        raise SandboxUnavailable(f"unexpected reply to BAR: {reply!r}")

    def close(self) -> None:
        """Best effort: whatever the sandbox answers, closing the socket ends the session there."""
        if not self._is_open:
            return
        try:
            _exchange(self._sock, {"v": proto.PROTOCOL_VERSION, "type": "CLOSE", "session": self._id},
                      self._reply_timeout)
        except SandboxUnavailable:
            pass
        finally:
            self._end()

    def __enter__(self) -> "SandboxSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _decision(self, reply: Mapping[str, Any], conid: int, when: datetime) -> Decision | None:
        try:
            return self._read_signal(reply, conid, when)
        except (SandboxFault, SandboxUnavailable):   # a reply we cannot trust ends the session
            self._end()
            raise

    def _read_signal(self, reply: Mapping[str, Any], conid: int, when: datetime) -> Decision | None:
        if reply.get("session") != self._id or reply.get("conid") != conid:
            raise SandboxUnavailable(f"signal is for another session or conid: {reply!r}")
        if not _is_instant(reply.get("t"), when):
            raise SandboxFault(f"signal is for another time than the bar sent at {when.isoformat()}",
                               code=proto.SANDBOX_ERROR)
        if "action" not in reply or "probability" not in reply:
            raise SandboxFault("signal has no action or probability", code=proto.SANDBOX_ERROR)
        try:
            proto.validate_signal(reply)
            if reply["action"] is None:
                return None
            return Decision(action=reply["action"], probability=float(reply["probability"]))
        except proto.FrameError as exc:
            raise SandboxFault(exc.detail, code=exc.code) from exc
        except ValueError as exc:
            raise SandboxFault(f"signal is not a decision: {exc}", code=proto.SANDBOX_ERROR) from exc

    def _end(self) -> None:
        self._is_open = False
        self._sock.close()


def _check_request(request: Mapping[str, Any]) -> None:
    try:
        proto.parse_request(request)
    except proto.FrameError as exc:
        raise SandboxProtocolError(exc.detail) from exc


def _utc(t: datetime) -> datetime:
    if not isinstance(t, datetime) or t.utcoffset() is None:
        raise SandboxProtocolError("t must be a timezone-aware datetime")
    try:
        return t.astimezone(timezone.utc)
    except OverflowError as exc:
        raise SandboxProtocolError("t is outside the supported time range") from exc


def _is_instant(text: Any, when: datetime) -> bool:
    """Does ``text`` (the time a SIGNAL echoes) name the same instant as ``when``?"""
    if not isinstance(text, str):
        return False
    try:
        echoed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return echoed.utcoffset() is not None and echoed == when


def _leaves_session_usable(reply: Mapping[str, Any]) -> bool:
    """Only a bar the sandbox refused as a caller mistake keeps the session, and not when the session is gone."""
    return reply.get("code") == proto.SANDBOX_BAD_REQUEST and reply.get("detail") != proto.UNKNOWN_SESSION_DETAIL


def _sandbox_info(reply: Mapping[str, Any]) -> SandboxInfo:
    if reply.get("type") != "PONG":
        raise SandboxUnavailable(f"unexpected reply to PING: {reply!r}")
    counts = {name: reply.get(name) for name in ("free", "size")}
    texts = {name: reply.get(name) for name in ("image_digest", "boot_id", "pool")}
    posture = reply.get("posture")
    if (any(type(value) is not int for value in counts.values())
            or any(not isinstance(value, str) for value in texts.values())
            or not isinstance(posture, dict)):
        raise SandboxUnavailable(f"PONG has a missing or mistyped field: {reply!r}")
    live_open = reply.get("live_open")
    return SandboxInfo(**texts, **counts, posture=posture,
                       live_open=live_open if type(live_open) is int and live_open >= 0 else None)


def _ohlcv(**values: Any) -> dict[str, float]:
    numbers_out = {}
    for name in _OHLCV_NAMES:
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise SandboxProtocolError(f"{name} must be a number")
        try:
            numbers_out[name] = float(value)
        except OverflowError as exc:
            raise SandboxProtocolError(f"{name} is too large") from exc
    return numbers_out


def _raise_for(reply: Mapping[str, Any]) -> None:
    if reply.get("type") != "ERROR":
        return
    code, detail = reply.get("code"), str(reply.get("detail", ""))
    if code == proto.SANDBOX_BUSY:
        raise SandboxUnavailable(f"sandbox busy: {detail}")
    if code == proto.SANDBOX_UNAVAILABLE:
        raise SandboxUnavailable(f"sandbox unavailable: {detail}")
    if code == proto.SANDBOX_BAD_REQUEST:
        raise SandboxProtocolError(detail)
    if code not in (proto.SANDBOX_ERROR, proto.SANDBOX_DIGEST_MISMATCH):
        raise SandboxUnavailable(f"unknown error code {code!r}: {detail}")
    raise SandboxFault(detail, code=code)


def _exchange(sock: socket.socket, request: Mapping[str, Any], timeout_s: float) -> dict:
    """Send one frame and read one reply, the whole round trip within ``timeout_s``.

    Any transport or framing failure is SandboxUnavailable; the connection is then unusable."""
    try:
        frame = proto.encode_frame(request)
    except proto.FrameError as exc:
        raise SandboxProtocolError(exc.detail) from exc
    deadline = time.monotonic() + timeout_s
    try:
        sock.settimeout(timeout_s)
        sock.sendall(frame)
        (length,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size, deadline))
        if length > proto.MAX_FRAME_BYTES:
            raise SandboxUnavailable(f"reply of {length} bytes is over the {proto.MAX_FRAME_BYTES} cap")
        reply = proto.decode_frame_body(_recv_exact(sock, length, deadline))
        proto.require_version(reply)
        return reply
    except TimeoutError as exc:   # socket.timeout is TimeoutError since 3.10
        raise SandboxUnavailable(f"no reply from the sandbox within {timeout_s}s") from exc
    except OSError as exc:
        raise SandboxUnavailable(f"sandbox connection failed: {exc}") from exc
    except proto.FrameError as exc:
        raise SandboxUnavailable(f"sandbox reply out of contract: {exc.detail}") from exc


def _recv_exact(sock: socket.socket, count: int, deadline: float) -> bytes:
    """One deadline for the whole frame: a peer that trickles bytes cannot stretch a per-recv timeout."""
    chunks = []
    while count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(remaining)
        chunk = sock.recv(count)
        if not chunk:
            raise ConnectionError("sandbox closed the connection")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)
