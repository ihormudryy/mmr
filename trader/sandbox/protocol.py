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

PROTOCOL_VERSION = 1

LIVE_SOCKET = "/run/sandbox-live/sandbox.sock"
RESEARCH_SOCKET = "/run/sandbox-research/sandbox.sock"
STORE_DIR = "/store"

SANDBOX_ERROR = "SANDBOX_ERROR"
SANDBOX_BUSY = "SANDBOX_BUSY"
SANDBOX_DIGEST_MISMATCH = "SANDBOX_DIGEST_MISMATCH"
SANDBOX_BAD_REQUEST = "SANDBOX_BAD_REQUEST"

MAX_FRAME_BYTES = 262144  # 256 KiB: a lookback of bars plus a 20 KB source, with headroom

# A live session serves one authored deployment: all of its conids in one child (ruling 26).
# Equals SP2c trader.automation.ai_deployments.MAX_CONIDS; a test pins the two together.
MAX_SESSION_CONIDS = 20
PROBE_CONID = 1           # the one conid of an OPEN_PROBE session (ruling 26)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PROBE_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# The build id PONG reports (ruling 1). Plan 3's IMAGE_ID wire pattern is the same text (one spelling, D2).
BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
SESSION_RE = re.compile(r"^s[0-9a-f]{1,32}$")
_PROBE_KINDS = frozenset({"probe", "escape"})


class FrameError(Exception):
    """A malformed or out-of-contract frame. ``code`` is the reply code to send."""

    def __init__(self, detail: str, *, code: str = SANDBOX_BAD_REQUEST):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def encode_frame(obj: Mapping[str, Any]) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def write_frame(writer: asyncio.StreamWriter, obj: Mapping[str, Any]) -> None:
    writer.write(encode_frame(obj))


async def read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {length} bytes over the {MAX_FRAME_BYTES} cap")
    body = await reader.readexactly(length)
    try:
        obj = json.loads(body)
    except (ValueError, RecursionError) as exc:  # bad UTF-8, bad JSON, or absurd nesting
        raise FrameError(f"frame is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameError("frame is not a JSON object")
    return obj


def _require_version(obj: Mapping[str, Any]) -> None:
    if obj.get("v") != PROTOCOL_VERSION:
        raise FrameError(f"unsupported protocol version {obj.get('v')!r}")


def _number(obj: Mapping[str, Any], key: str) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrameError(f"{key} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise FrameError(f"{key} is not finite")
    return out


def _conid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FrameError("conid must be a positive integer")
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
    out: dict[str, float] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not CLASS_RE.match(name):
            raise FrameError(f"bad param name {name!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FrameError(f"param {name} must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise FrameError(f"param {name} is not finite")
        out[name] = number
    return out


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
    _require_version(obj)
    kind = obj.get("type")
    if kind == "PING":
        return Request(type="PING")
    if kind == "OPEN":
        digest = obj.get("digest")
        class_name = obj.get("class")
        bar_size = obj.get("bar_size")
        if not isinstance(digest, str) or not DIGEST_RE.match(digest):
            raise FrameError("digest must be sha256:<64 hex>")
        if not isinstance(class_name, str) or not CLASS_RE.match(class_name):
            raise FrameError("class must be a Python identifier")
        if not isinstance(bar_size, str) or not bar_size.strip():
            raise FrameError("bar_size must be a non-empty string")
        return Request(type="OPEN", digest=digest, class_name=class_name,
                       params=_params(obj), bar_size=bar_size, conids=_conids(obj))
    if kind == "OPEN_PROBE":
        probe_kind = obj.get("kind")
        name = obj.get("name")
        if probe_kind not in _PROBE_KINDS:
            raise FrameError(f"probe kind must be one of {sorted(_PROBE_KINDS)}")
        if not isinstance(name, str) or not PROBE_NAME_RE.match(name):
            raise FrameError("probe name must match [a-z0-9_]{1,64}")
        return Request(type="OPEN_PROBE", kind=probe_kind, name=name, conids=_conids(obj))
    if kind == "BAR":
        session = obj.get("session")
        if not isinstance(session, str) or not SESSION_RE.match(session):
            raise FrameError("bad session id")
        conid = _conid(obj.get("conid"))
        t_raw = obj.get("t")
        if not isinstance(t_raw, str):
            raise FrameError("t must be an ISO-8601 string")
        try:
            when = datetime.fromisoformat(t_raw)
        except ValueError as exc:
            raise FrameError(f"t is not ISO-8601: {exc}") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ohlcv = {col: _number(obj, col) for col in ("open", "high", "low", "close", "volume")}
        return Request(type="BAR", session=session, conid=conid, t=when.astimezone(timezone.utc), ohlcv=ohlcv)
    if kind == "CLOSE":
        session = obj.get("session")
        if not isinstance(session, str) or not SESSION_RE.match(session):
            raise FrameError("bad session id")
        return Request(type="CLOSE", session=session)
    raise FrameError(f"unknown request type {kind!r}")


def validate_signal(obj: Mapping[str, Any]) -> None:
    """Raise FrameError when a SIGNAL payload is out of contract (ruling: a bad child reply is a fault)."""
    action = obj.get("action")
    probability = obj.get("probability")
    if action is None and probability is None:
        return
    if action not in ("BUY", "SELL"):
        raise FrameError(f"action must be BUY, SELL or null, got {action!r}", code=SANDBOX_ERROR)
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise FrameError("probability must be a number", code=SANDBOX_ERROR)
    number = float(probability)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise FrameError("probability must be within 0..1", code=SANDBOX_ERROR)
