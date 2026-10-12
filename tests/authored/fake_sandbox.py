"""Tests only: Plan 2's SandboxClient interface, served in this process.

No supervisor, no uid switch, no socket. It keeps the protocol rules both callers depend on (spec 7.5 as amended,
Plan 2 ruling 26): one class per session and one strategy instance and history per conid of the session, BAR times
strictly increasing per conid, the history trimmed to LOOKBACK_BARS, a conid outside the session a protocol error,
a full pool refused at once, and any exception or bad output a SandboxFault(SANDBOX_ERROR) that ends the whole
session. ``params`` are exactly the OPEN params, as Plan 2's child passes them (no class defaults). It loads only
fixture sources that pass Plan 1's static check.

It is as strict as the real client (Plan 4 pre-flight F2): every OPEN and BAR frame goes through
``trader.sandbox.protocol.parse_request`` and a refusal is a SandboxProtocolError; a closed session raises
SandboxProtocolError; a file the store does not hold is SANDBOX_DIGEST_MISMATCH; a lost socket ends the session.
"""
from __future__ import annotations

import math
import numbers
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd

from trader.authored.api import MAX_LOOKBACK_BARS, MIN_LOOKBACK_BARS
from trader.authored.keys import source_digest
from trader.authored.sandbox_client import SandboxFault, SandboxInfo, SandboxProtocolError, SandboxUnavailable
from trader.authored.static_check import require_clean
from trader.sandbox.protocol import (PROTOCOL_VERSION, SANDBOX_BUSY, SANDBOX_DIGEST_MISMATCH, SANDBOX_ERROR,
                                     FrameError, parse_request)
from trader.sandbox.supervisor import LIVE_POOL_SIZE, MAX_CONIDS_PER_SESSION, RESEARCH_POOL_SIZE

CALLERS_DIR = Path(__file__).parent / "callers"
ORB_FIXTURE = Path(__file__).parent / "fixtures" / "orb_authored.py"     # Plan 1's ORB port (decision D8)
COLUMNS = ("open", "high", "low", "close", "volume")
IMAGE_DIGEST = "a1b2c3d-20261011T220000Z"            # a Plan 2 build id, as PONG reports it (decision D2)
_FAKE_SESSION_ID = "s1"


def caller_source(name: str) -> str:
    return (CALLERS_DIR / f"{name}.py").read_text()


def orb_fixture_source() -> str:
    return ORB_FIXTURE.read_text()


def fault(code: str) -> SandboxFault:
    return SandboxFault(f"fake sandbox: {code}", code=code)


def _check_frame(frame: Mapping[str, Any]) -> None:
    try:
        parse_request(frame)
    except FrameError as exc:
        raise SandboxProtocolError(exc.detail) from exc


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise SandboxProtocolError(f"{name} must be a number")
    return float(value)


def _valid(decision: Any) -> bool:
    probability = getattr(decision, "probability", None)
    return (getattr(decision, "action", None) in ("BUY", "SELL")
            and isinstance(probability, (int, float)) and not isinstance(probability, bool)
            and math.isfinite(probability) and 0.0 <= probability <= 1.0)


class _ConidBook:
    def __init__(self, strategy: Any):
        self.strategy = strategy
        self.rows: list[tuple] = []
        self.stamps: list[pd.Timestamp] = []


class FakeSession:
    def __init__(self, sandbox: "FakeSandbox", strategy_class: type, params: Mapping[str, float], digest: str,
                 conids: tuple[int, ...]):
        self._sandbox, self.digest, self.conids = sandbox, digest, conids
        self.image_digest = sandbox.image_digest          # the build OPENED reported (Plan 2 ruling 29)
        self._params = dict(params)
        self._lookback = max(MIN_LOOKBACK_BARS, min(MAX_LOOKBACK_BARS, int(strategy_class.LOOKBACK_BARS)))
        self._books = {conid: _ConidBook(strategy_class()) for conid in conids}
        self.stamps: list[pd.Timestamp] = []            # every bar sent, any conid, in send order
        self.first_close: Optional[float] = None
        self.closed = False

    def stamps_of(self, conid: int) -> list[pd.Timestamp]:
        return list(self._books[conid].stamps)

    def bar(self, conid: int, t: datetime, open: float, high: float, low: float, close: float, volume: float):
        if self.closed:
            raise SandboxProtocolError("session is closed")
        book = self._books.get(conid)
        if book is None:
            raise SandboxProtocolError(f"conid {conid} is not in this session")
        if not isinstance(t, datetime) or t.utcoffset() is None:
            raise SandboxProtocolError("t must be a timezone-aware datetime")
        stamp = pd.Timestamp(t).tz_convert("UTC")
        values = {name: _number(name, value) for name, value in
                  zip(COLUMNS, (open, high, low, close, volume))}
        _check_frame({"v": PROTOCOL_VERSION, "type": "BAR", "session": _FAKE_SESSION_ID, "conid": conid,
                      "t": stamp.isoformat(), **values})
        if self._sandbox.unavailable:                   # a lost socket ends the session, as in the client
            self.close()
            raise SandboxUnavailable("fake: the session is gone")
        if book.stamps and stamp <= book.stamps[-1]:
            raise SandboxProtocolError("bar time must strictly increase per conid")
        book.stamps.append(stamp)
        self.stamps.append(stamp)
        self.first_close = values["close"] if self.first_close is None else self.first_close
        book.rows = (book.rows + [tuple(values[name] for name in COLUMNS)])[-self._lookback:]
        history = pd.DataFrame(book.rows, columns=list(COLUMNS),
                               index=pd.DatetimeIndex(book.stamps[-len(book.rows):], name="date"))
        try:
            decision = book.strategy.on_bar(history, dict(self._params))
        except Exception as exc:
            self.close()
            raise fault(SANDBOX_ERROR) from exc
        if decision is not None and not _valid(decision):
            self.close()
            raise fault(SANDBOX_ERROR)
        return decision

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._sandbox.open_sessions.remove(self)


class FakeSandbox:
    def __init__(self, *, research: bool = True, pool: Optional[int] = None, image_digest: str = IMAGE_DIGEST,
                 max_conids: Optional[int] = None):
        """``research`` picks the real pool this fake stands for: its size and its conids per session. ``pool``
        and ``max_conids`` override them for a test that needs another size."""
        kind = "research" if research else "live"
        self.pool = pool if pool is not None else (RESEARCH_POOL_SIZE if research else LIVE_POOL_SIZE)
        self.max_conids = max_conids if max_conids is not None else MAX_CONIDS_PER_SESSION[kind]
        self.image_digest = image_digest
        self.sources: dict[str, str] = {}
        self.sessions: list[FakeSession] = []           # every session ever opened, in order
        self.open_sessions: list[FakeSession] = []
        self.most_open = 0
        self.unavailable = False                        # the socket is gone: every call raises SandboxUnavailable
        self.refuse_opens_after: Optional[int] = None   # after this many opens, every OPEN is unavailable
        self.tampered: set[str] = set()                 # stored files that no longer hash to their digest

    def add(self, source: str) -> str:
        digest = source_digest(source.encode())
        self.sources[digest] = source
        return digest

    def ping(self) -> SandboxInfo:
        if self.unavailable:
            raise SandboxUnavailable("fake: socket unreachable")
        return SandboxInfo(image_digest=self.image_digest, boot_id="fake", pool="fake",
                           free=self.pool - len(self.open_sessions), size=self.pool, posture={},
                           live_open=len(self.open_sessions))

    def open_session(self, digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
                     conids) -> FakeSession:
        _check_frame({"v": PROTOCOL_VERSION, "type": "OPEN", "digest": digest, "class": class_name,
                      "params": dict(params), "bar_size": bar_size, "conids": list(conids)})
        conids = tuple(int(conid) for conid in conids)
        if len(conids) > self.max_conids:
            raise SandboxProtocolError(f"a session takes at most {self.max_conids} conids")
        refused = self.refuse_opens_after is not None and len(self.sessions) >= self.refuse_opens_after
        if self.unavailable or refused:
            raise SandboxUnavailable("fake: socket unreachable")
        # The supervisor reads, hashes and checks the file before it reserves a slot (supervisor._open_store).
        source = self.sources.get(digest)
        if source is None or digest in self.tampered:
            raise fault(SANDBOX_DIGEST_MISMATCH)
        if require_clean(source) != class_name:
            raise fault(SANDBOX_DIGEST_MISMATCH)
        if len(self.open_sessions) >= self.pool:
            raise SandboxUnavailable(SANDBOX_BUSY)
        namespace: dict = {}
        exec(compile(source, f"<authored {digest}>", "exec"), namespace)   # fixture sources, test process only
        session = FakeSession(self, namespace[class_name], params, digest, conids)
        self.sessions.append(session)
        self.open_sessions.append(session)
        self.most_open = max(self.most_open, len(self.open_sessions))
        return session
