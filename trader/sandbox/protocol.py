"""The sandbox wire protocol: 4-byte length + JSON frames, version 1."""
from __future__ import annotations

import asyncio
import json
import math
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from trader.bar_size import BarSize

PROTOCOL_VERSION = 1

LIVE_SOCKET = "/run/sandbox-live/sandbox.sock"
RESEARCH_SOCKET = "/run/sandbox-research/sandbox.sock"
STORE_DIR = "/store"

# Who is to blame (spec 7.6, 8; ruling 34). Faults of the strategy: SANDBOX_ERROR (its load, on_bar, output or
# time ran wrong, or the static check refused it) and SANDBOX_DIGEST_MISMATCH. Availability, never the strategy:
# SANDBOX_BUSY (no free slot now) and SANDBOX_UNAVAILABLE (the sandbox itself failed: fork or socketpair, the
# child's privilege drop, rlimits or setup, an internal error). SANDBOX_BAD_REQUEST: the caller (ruling 8).
SANDBOX_ERROR = "SANDBOX_ERROR"
SANDBOX_BUSY = "SANDBOX_BUSY"
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
SANDBOX_DIGEST_MISMATCH = "SANDBOX_DIGEST_MISMATCH"
SANDBOX_BAD_REQUEST = "SANDBOX_BAD_REQUEST"

# The BAD_REQUEST detail for a session the connection does not hold; the one caller mistake that ends
# the client's session (it is gone on the server).
UNKNOWN_SESSION_DETAIL = "unknown session"

MAX_FRAME_BYTES = 262144  # 256 KiB: a lookback of bars plus a 20 KB source, with headroom

# A live session serves one authored deployment: all of its conids in one child (ruling 26).
# Equals SP2c trader.automation.ai_deployments.MAX_CONIDS; a test pins the two together.
MAX_SESSION_CONIDS = 20
PROBE_CONID = 1           # the one conid of an OPEN_PROBE session (ruling 26)
MAX_CONID = 2**31 - 1     # IB contract ids are signed 32-bit

# Bar times are UTC ISO-8601 (ruling 20); anything outside this window is a corrupt time.
_MIN_BAR_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_BAR_TIME = datetime(2100, 1, 1, tzinfo=timezone.utc)
_KNOWN_BAR_SIZES = frozenset(BarSize.bar_sizes())

# \A...\Z, never ^...$: "$" also matches before a trailing newline. Use .fullmatch.
DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
CLASS_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
PROBE_NAME_RE = re.compile(r"\A[a-z0-9_]{1,64}\Z")
# The build id PONG reports (ruling 1). Plan 3's IMAGE_ID wire pattern is the same text (one spelling, D2).
BUILD_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
SESSION_RE = re.compile(r"\As[0-9a-f]{1,32}\Z")
_PROBE_KINDS = frozenset({"probe", "escape"})


class FrameError(Exception):
    """A malformed or out-of-contract frame. ``code`` is the reply code to send."""

    def __init__(self, detail: str, *, code: str = SANDBOX_BAD_REQUEST):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def encode_frame(obj: Mapping[str, Any]) -> bytes:
    try:
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except ValueError as exc:
        raise FrameError(f"frame cannot be encoded: {exc}", code=SANDBOX_ERROR) from exc
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {len(body)} bytes over the {MAX_FRAME_BYTES} cap", code=SANDBOX_ERROR)
    return struct.pack(">I", len(body)) + body


def write_frame(writer: asyncio.StreamWriter, obj: Mapping[str, Any]) -> None:
    writer.write(encode_frame(obj))


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise FrameError(f"duplicate key {key!r}")
        out[key] = value
    return out


async def read_frame(reader: asyncio.StreamReader) -> dict:
    """Read one frame. Every failure is a FrameError.

    After an oversize-header FrameError the stream is out of step (the body was not read): the
    caller must close the connection, not read another frame from it.
    """
    try:
        header = await reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        if length > MAX_FRAME_BYTES:
            raise FrameError(f"frame of {length} bytes over the {MAX_FRAME_BYTES} cap")
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise FrameError(f"connection closed after {len(exc.partial)} of {exc.expected} frame bytes") from exc
    return decode_frame_body(body)


def decode_frame_body(body: bytes) -> dict:
    """The JSON object in one frame body (the bytes after the 4-byte length). Every failure is a FrameError."""
    try:
        obj = json.loads(body, object_pairs_hook=_object_without_duplicate_keys)
    except (ValueError, RecursionError) as exc:  # bad UTF-8, bad JSON, or absurd nesting
        raise FrameError(f"frame is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameError("frame is not a JSON object")
    return obj


def require_version(obj: Mapping[str, Any]) -> None:
    version = obj.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise FrameError(f"unsupported protocol version {version!r}")


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrameError(f"{label} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise FrameError(f"{label} is too large") from exc
    if not math.isfinite(number):
        raise FrameError(f"{label} is not finite")
    return number


def _text(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise FrameError(what)
    return value


def _conid(value: Any) -> int:
    if type(value) is not int or not 0 < value <= MAX_CONID:
        raise FrameError(f"conid must be an integer from 1 to {MAX_CONID}")
    return value


def _conids(obj: Mapping[str, Any]) -> tuple[int, ...]:
    raw = obj.get("conids")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SESSION_CONIDS:
        raise FrameError(f"conids must be a list of 1-{MAX_SESSION_CONIDS} conids")
    conids = tuple(_conid(value) for value in raw)
    if len(set(conids)) != len(conids):
        raise FrameError("conids must be distinct")
    return conids


def _params(obj: Mapping[str, Any]) -> dict[str, float]:
    raw = obj.get("params", {})
    if not isinstance(raw, dict):
        raise FrameError("params must be an object")
    return {_text(name, CLASS_RE, f"bad param name {name!r}"): _finite_float(value, f"param {name}")
            for name, value in raw.items()}


def _session(obj: Mapping[str, Any]) -> str:
    return _text(obj.get("session"), SESSION_RE, "bad session id")


def _bar_time(obj: Mapping[str, Any]) -> datetime:
    raw = obj.get("t")
    if not isinstance(raw, str):
        raise FrameError("t must be an ISO-8601 string")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FrameError(f"t is not ISO-8601: {exc}") from exc
    if when.utcoffset() is None:
        raise FrameError("t must carry a UTC offset (ISO-8601 with Z or +HH:MM)")
    try:
        when_utc = when.astimezone(timezone.utc)
    except OverflowError as exc:
        raise FrameError("t is outside the supported time range") from exc
    if not _MIN_BAR_TIME <= when_utc < _MAX_BAR_TIME:
        raise FrameError("t is outside 1970-01-01..2100-01-01 UTC")
    return when_utc


def _ohlcv(obj: Mapping[str, Any]) -> dict[str, float]:
    ohlcv = {col: _finite_float(obj.get(col), col) for col in ("open", "high", "low", "close", "volume")}
    if ohlcv["volume"] < 0:
        raise FrameError("volume must not be negative")
    if ohlcv["high"] < ohlcv["low"]:
        raise FrameError("high must not be below low")
    return ohlcv


@dataclass(frozen=True)
class Request:
    type: str
    digest: str = ""
    class_name: str = ""
    params: Mapping[str, float] = field(default_factory=dict)
    bar_size: str = ""
    kind: str = ""
    name: str = ""
    session: str = ""
    conids: tuple[int, ...] = ()
    conid: int = 0
    t: datetime | None = None
    ohlcv: Mapping[str, float] = field(default_factory=dict)


def parse_request(obj: Mapping[str, Any]) -> Request:
    require_version(obj)
    kind = obj.get("type")
    if kind == "PING":
        return Request(type="PING")
    if kind == "OPEN":
        bar_size = obj.get("bar_size")
        if not isinstance(bar_size, str) or bar_size not in _KNOWN_BAR_SIZES:
            raise FrameError(f"bar_size must be one of {sorted(_KNOWN_BAR_SIZES)}")
        return Request(type="OPEN",
                       digest=_text(obj.get("digest"), DIGEST_RE, "digest must be sha256:<64 hex>"),
                       class_name=_text(obj.get("class"), CLASS_RE, "class must be a Python identifier"),
                       params=_params(obj), bar_size=bar_size, conids=_conids(obj))
    if kind == "OPEN_PROBE":
        probe_kind = obj.get("kind")
        if not isinstance(probe_kind, str) or probe_kind not in _PROBE_KINDS:
            raise FrameError(f"probe kind must be one of {sorted(_PROBE_KINDS)}")
        name = _text(obj.get("name"), PROBE_NAME_RE, "probe name must match [a-z0-9_]{1,64}")
        conids = _conids(obj)
        if conids != (PROBE_CONID,):
            raise FrameError(f"a probe session holds exactly conid {PROBE_CONID}")
        return Request(type="OPEN_PROBE", kind=probe_kind, name=name, conids=conids)
    if kind == "BAR":
        return Request(type="BAR", session=_session(obj), conid=_conid(obj.get("conid")),
                       t=_bar_time(obj), ohlcv=_ohlcv(obj))
    if kind == "CLOSE":
        return Request(type="CLOSE", session=_session(obj))
    raise FrameError(f"unknown request type {kind!r}")


def validate_signal(obj: Mapping[str, Any]) -> None:
    """Raise FrameError when a SIGNAL payload is out of contract (ruling: a bad child reply is a fault)."""
    action = obj.get("action")
    probability = obj.get("probability")
    if action is None and probability is None:
        return
    if action not in ("BUY", "SELL"):
        raise FrameError(f"action must be BUY, SELL or null, got {action!r}", code=SANDBOX_ERROR)
    try:
        number = _finite_float(probability, "probability")
    except FrameError as exc:
        raise FrameError(exc.detail, code=SANDBOX_ERROR) from exc
    if not 0.0 <= number <= 1.0:
        raise FrameError("probability must be within 0..1", code=SANDBOX_ERROR)
