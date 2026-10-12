import asyncio
import hashlib
import os
import shutil
import socket
import struct
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import pytest

from trader.authored.api import Decision
from trader.authored.sandbox_client import (
    SandboxClient, SandboxFault, SandboxInfo, SandboxProtocolError, SandboxUnavailable,
)
from trader.sandbox import protocol as p
from trader.sandbox.server import serve
from trader.sandbox.supervisor import Supervisor
from tests.sandbox.fakes import InProcessSpawner

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
C1, C2 = 101, 202
GOOD = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.6) if len(history) >= 2 else None
'''
FAILING = '''
from trader.authored.api import AuthoredStrategy, Decision


class Failing(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=len(history) / 0)
'''
BAR = (1.0, 2.0, 0.5, 1.5, 1.0)


@pytest.fixture
def socket_dir():
    # macOS caps a Unix socket path at 104 bytes; pytest's tmp_path there is longer.
    path = tempfile.mkdtemp(prefix="sbx", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


class _Stack:
    """Run the asyncio server in a background thread so a synchronous client can call it."""

    def __init__(self, store_dir, socket_dir, live=2, research=1, live_spawner=None):
        self.store_dir = store_dir
        self._live_spawner = live_spawner or InProcessSpawner()
        self.live = os.path.join(socket_dir, "live.sock")
        self.research = os.path.join(socket_dir, "research.sock")
        self._loop = asyncio.new_event_loop()
        self._server = None
        self._sizes = {"live": live, "research": research}
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        sup = Supervisor(spawners={"live": self._live_spawner, "research": InProcessSpawner()},
                         pool_sizes=self._sizes, store_dir=str(self.store_dir),
                         image_digest="build-x", posture={"caps": "00000000000000e0"})
        self._server = self._loop.run_until_complete(
            serve(sup, {"live": self.live, "research": self.research}, socket_gid=os.getgid()))
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def store(self, source):
        digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        (self.store_dir / f"{digest.split(':')[1]}.py").write_text(source)
        return digest

    def stop(self):
        asyncio.run_coroutine_threadsafe(self._server.close(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()


@pytest.fixture
def stack(tmp_path, socket_dir):
    s = _Stack(tmp_path, socket_dir)
    yield s
    s.stop()


class _FakeServer:
    """A scripted peer: ``script(conn)`` runs once per accepted connection, in its own thread."""

    def __init__(self, socket_dir, script):
        self.path = os.path.join(socket_dir, "fake.sock")
        self._script = script
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            self._script(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        self._listener.close()


@pytest.fixture
def fake(socket_dir):
    servers = []

    def make(script):
        server = _FakeServer(socket_dir, script)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def _read_request(conn):
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            raise OSError("closed")
        header += chunk
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        body += conn.recv(length - len(body))
    return p.decode_frame_body(body)


def _reply(conn, obj):
    conn.sendall(p.encode_frame(obj))


def _opened_then(script_after_open):
    """A fake that answers OPEN, then hands each later request to ``script_after_open(conn, request)``."""
    def script(conn):
        request = _read_request(conn)
        assert request["type"] == "OPEN"
        _reply(conn, {"type": "OPENED", "v": 1, "session": "s1", "image_digest": "build-x"})
        while True:
            request = _read_request(conn)
            if request["type"] == "CLOSE":
                _reply(conn, {"type": "CLOSED", "v": 1})
            else:
                script_after_open(conn, request)
    return script


def _open_fake_session(fake, script_after_open, **client_options):
    server = fake(_opened_then(script_after_open))
    return SandboxClient(server.path, **client_options).open_session(
        "sha256:" + "a" * 64, "Counter", {}, "1 min", [C1])


def test_ping_returns_the_info(stack):
    info = SandboxClient(stack.live).ping()
    assert isinstance(info, SandboxInfo)
    assert info.image_digest == "build-x" and info.posture["caps"] == "00000000000000e0"
    assert (info.pool, info.size, info.free, info.live_open) == ("live", 2, 2, 0)


def test_open_session_streams_bars_and_returns_decisions(stack):
    digest = stack.store(GOOD)
    client = SandboxClient(stack.live)
    session = client.open_session(digest, "Counter", {}, "1 min", [C1, C2])
    assert session.conids == (C1, C2) and session.image_digest == "build-x"
    assert client.ping().live_open == 1
    assert session.bar(C1, T0, *BAR) is None
    assert session.bar(C2, T0, *BAR) is None
    decision = session.bar(C1, T0 + timedelta(minutes=1), *BAR)
    assert isinstance(decision, Decision) and decision.action == "BUY"
    session.close()
    assert client.ping().live_open == 0


def test_close_frees_the_slot_and_is_idempotent(stack):
    digest = stack.store(GOOD)
    client = SandboxClient(stack.live)
    with client.open_session(digest, "Counter", {}, "1 min", [C1]) as session:
        assert client.ping().free == 1
    assert client.ping().free == 2
    session.close()


def test_a_closed_session_refuses_bars_loudly(stack):
    session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
    session.close()
    with pytest.raises(SandboxProtocolError):
        session.bar(C1, T0, *BAR)


def test_a_conid_the_session_does_not_hold_is_refused_before_it_is_sent(stack):
    session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
    with pytest.raises(SandboxProtocolError):
        session.bar(C2, T0, *BAR)
    assert session.bar(C1, T0, *BAR) is None
    session.close()


def test_a_research_session_with_two_conids_is_a_protocol_error(stack):
    with pytest.raises(SandboxProtocolError):
        SandboxClient(stack.research).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1, C2])


def test_an_unreachable_socket_is_unavailable(socket_dir):
    with pytest.raises(SandboxUnavailable):
        SandboxClient(os.path.join(socket_dir, "nope.sock"), connect_timeout_s=0.2).ping()


def test_a_busy_pool_is_unavailable(stack):
    digest = stack.store(GOOD)
    held = [SandboxClient(stack.live).open_session(digest, "Counter", {}, "1 min", [C1]) for _ in range(2)]
    with pytest.raises(SandboxUnavailable):
        SandboxClient(stack.live).open_session(digest, "Counter", {}, "1 min", [C1])
    for session in held:
        session.close()


def test_a_digest_mismatch_is_a_fault_with_its_code(stack):
    with pytest.raises(SandboxFault) as exc:
        SandboxClient(stack.live).open_session("sha256:" + "a" * 64, "Counter", {}, "1 min", [C1])
    assert exc.value.code == p.SANDBOX_DIGEST_MISMATCH


def test_a_static_rejection_is_a_sandbox_error_fault(stack):
    bad = "import os\nfrom trader.authored.api import AuthoredStrategy, Decision\n" \
          "class S(AuthoredStrategy):\n    LOOKBACK_BARS = 2\n" \
          "    def on_bar(self, history, params):\n        return None\n"
    with pytest.raises(SandboxFault) as exc:
        SandboxClient(stack.live).open_session(stack.store(bad), "S", {}, "1 min", [C1])
    assert exc.value.code == p.SANDBOX_ERROR


def test_a_non_increasing_bar_time_is_a_protocol_error(stack):
    session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
    session.bar(C1, T0, *BAR)
    with pytest.raises(SandboxProtocolError):   # ruling 8: a caller clock bug, child not killed
        session.bar(C1, T0, *BAR)
    # The refused bar was not counted: this is the second bar the strategy saw, so it signals.
    decision = session.bar(C1, T0 + timedelta(minutes=1), *BAR)
    assert decision == Decision(action="BUY", probability=0.6)
    session.close()


def test_a_strategy_fault_ends_the_session_and_frees_the_slot(stack):
    client = SandboxClient(stack.live)
    session = client.open_session(stack.store(FAILING), "Failing", {}, "1 min", [C1])
    with pytest.raises(SandboxFault) as exc:
        session.bar(C1, T0, *BAR)
    assert exc.value.code == p.SANDBOX_ERROR
    with pytest.raises(SandboxProtocolError):    # a dead session is an error, never "no signal"
        session.bar(C1, T0 + timedelta(minutes=1), *BAR)
    assert client.ping().free == 2


class _FailingSpawner:
    """Fails at spawn with ``failure``, or spawns a child whose bars raise ``bar_failure``."""

    def __init__(self, failure=None, bar_failure=None):
        self._failure = failure
        self._bar_failure = bar_failure

    def spawn(self, spec):
        if self._failure is not None:
            raise self._failure
        bar_failure = self._bar_failure

        class _Child:
            pid = -1

            def send_bar(self, conid, t, ohlcv):
                raise bar_failure

            def close(self):
                return None
        return _Child()


def _setup_error(detail):
    from trader.sandbox.child import ChildSetupError
    return ChildSetupError(detail)


@pytest.mark.parametrize("failure", [
    BlockingIOError(11, "Resource temporarily unavailable"),        # fork EAGAIN under the pids limit
    "PRIVILEGE_DROP_FAILED", "CHILD_LIMITS_FAILED", "CHILD_SETUP_FAILED",
])
def test_a_sandbox_side_open_failure_is_unavailable_not_a_fault(tmp_path, socket_dir, failure):   # ruling 34
    exc = _setup_error(failure) if isinstance(failure, str) else failure
    stack = _Stack(tmp_path, socket_dir, live_spawner=_FailingSpawner(failure=exc))
    try:
        with pytest.raises(SandboxUnavailable):
            SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
        assert SandboxClient(stack.live).ping().free == 2
    finally:
        stack.stop()


def test_a_sandbox_bug_at_a_bar_is_unavailable_and_ends_the_session(tmp_path, socket_dir):        # ruling 34
    stack = _Stack(tmp_path, socket_dir, live_spawner=_FailingSpawner(bar_failure=RuntimeError("bug")))
    try:
        session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
        with pytest.raises(SandboxUnavailable, match="internal sandbox error"):
            session.bar(C1, T0, *BAR)
        with pytest.raises(SandboxProtocolError):
            session.bar(C1, T0 + timedelta(minutes=1), *BAR)
    finally:
        stack.stop()


def test_a_strategy_fault_at_a_bar_stays_a_fault(tmp_path, socket_dir):
    from trader.sandbox.child import ChildError

    stack = _Stack(tmp_path, socket_dir,
                   live_spawner=_FailingSpawner(bar_failure=ChildError("on_bar raised ZeroDivisionError: x")))
    try:
        session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
        with pytest.raises(SandboxFault) as exc:
            session.bar(C1, T0, *BAR)
        assert exc.value.code == p.SANDBOX_ERROR
    finally:
        stack.stop()


def test_an_unavailable_reply_maps_to_sandbox_unavailable(fake):
    session = _open_fake_session(fake, lambda conn, request: _reply(
        conn, {"type": "ERROR", "v": 1, "code": p.SANDBOX_UNAVAILABLE, "detail": "fork failed"}))
    with pytest.raises(SandboxUnavailable, match="fork failed"):
        session.bar(C1, T0, *BAR)
    with pytest.raises(SandboxProtocolError):    # the sandbox ended the session
        session.bar(C1, T0 + timedelta(minutes=1), *BAR)


def test_a_probe_opens_on_the_research_socket_with_its_one_conid(stack):
    with SandboxClient(stack.research).open_probe("probe", "benign_orb") as session:
        assert session.conids == (p.PROBE_CONID,)
        assert session.bar(p.PROBE_CONID, T0, *BAR) is None


def test_an_unknown_probe_is_a_protocol_error(stack):
    with pytest.raises(SandboxProtocolError):
        SandboxClient(stack.research).open_probe("probe", "no_such_probe")


@pytest.mark.parametrize("call", [
    lambda c: c.open_session("not-a-digest", "Counter", {}, "1 min", [C1]),
    lambda c: c.open_session("sha256:" + "a" * 64, "Counter", {}, "7 fortnights", [C1]),
    lambda c: c.open_session("sha256:" + "a" * 64, "Counter", {}, "1 min", []),
    lambda c: c.open_session("sha256:" + "a" * 64, "Counter", {}, "1 min", [C1, C1]),
    lambda c: c.open_session("sha256:" + "a" * 64, "Counter", {"X": float("nan")}, "1 min", [C1]),
    lambda c: c.open_session("sha256:" + "a" * 64, "Counter", {}, "1 min", [True]),
    lambda c: c.open_probe("rm", "benign_orb"),
    lambda c: c.open_probe("probe", "../x"),
])
def test_a_bad_open_is_refused_before_any_connection_is_made(call, socket_dir):
    client = SandboxClient(os.path.join(socket_dir, "nope.sock"), connect_timeout_s=0.2)
    with pytest.raises(SandboxProtocolError):   # not SandboxUnavailable: the socket is never touched
        call(client)


@pytest.mark.parametrize("fields", [
    {"t": datetime(2024, 1, 2, 14, 30)},                                   # no timezone
    {"t": datetime(1969, 12, 31, tzinfo=timezone.utc)},
    {"open": float("nan")},
    {"close": float("inf")},
    {"volume": -1.0},
    {"high": 0.1},                                                         # high below low
    {"open": "abc"},
])
def test_a_bad_bar_is_refused_before_it_is_sent_and_the_session_survives(fields, stack):
    session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
    bar = {"t": T0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0, **fields}
    with pytest.raises(SandboxProtocolError):
        session.bar(C1, **bar)
    assert session.bar(C1, T0, *BAR) is None
    session.close()


def test_a_bar_time_in_another_zone_is_sent_as_the_same_instant(stack):
    session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
    new_york = timezone(timedelta(hours=-5))
    assert session.bar(C1, T0.astimezone(new_york), *BAR) is None
    with pytest.raises(SandboxProtocolError):    # the same instant again is not an increase
        session.bar(C1, T0, *BAR)
    session.close()


def test_a_slow_bar_reply_is_waited_for_beyond_the_connect_timeout(fake):
    def slow(conn, request):
        threading.Event().wait(0.4)
        _reply(conn, {"type": "SIGNAL", "v": 1, "session": "s1", "conid": C1, "t": request["t"],
                      "action": "SELL", "probability": 0.7})

    session = _open_fake_session(fake, slow, connect_timeout_s=0.1, reply_timeout_s=5.0)
    decision = session.bar(C1, T0, *BAR)
    assert decision == Decision(action="SELL", probability=0.7)
    session.close()


def test_a_reply_that_never_comes_is_unavailable_and_ends_the_session(fake):
    session = _open_fake_session(fake, lambda conn, request: threading.Event().wait(5), reply_timeout_s=0.3)
    with pytest.raises(SandboxUnavailable):
        session.bar(C1, T0, *BAR)
    with pytest.raises(SandboxProtocolError):
        session.bar(C1, T0 + timedelta(minutes=1), *BAR)


def test_a_reply_trickled_slowly_is_cut_off_by_one_deadline(fake):
    def trickle(conn, request):
        frame = p.encode_frame({"type": "SIGNAL", "v": 1, "session": "s1", "conid": C1, "t": request["t"],
                                "action": None, "probability": None})
        for byte in frame:
            conn.sendall(bytes([byte]))
            threading.Event().wait(0.05)

    session = _open_fake_session(fake, trickle, reply_timeout_s=0.3)
    with pytest.raises(SandboxUnavailable):
        session.bar(C1, T0, *BAR)


def test_a_connection_dropped_during_a_bar_is_unavailable(fake):
    def hang_up(conn, request):
        conn.close()
        raise OSError("hung up")

    session = _open_fake_session(fake, hang_up)
    with pytest.raises(SandboxUnavailable):
        session.bar(C1, T0, *BAR)
    session.close()


@pytest.mark.parametrize("wire", [
    struct.pack(">I", p.MAX_FRAME_BYTES + 1),
    struct.pack(">I", 5) + b"nope!",
    struct.pack(">I", 2) + b"[]",
])
def test_a_reply_out_of_contract_is_unavailable(fake, wire):
    session = _open_fake_session(fake, lambda conn, request: conn.sendall(wire))
    with pytest.raises(SandboxUnavailable):
        session.bar(C1, T0, *BAR)


def test_a_signal_for_another_conid_is_not_trusted(fake):
    def wrong_conid(conn, request):
        _reply(conn, {"type": "SIGNAL", "v": 1, "session": "s1", "conid": C2, "t": request["t"],
                      "action": "BUY", "probability": 0.9})

    session = _open_fake_session(fake, wrong_conid)
    with pytest.raises(SandboxUnavailable):
        session.bar(C1, T0, *BAR)


def test_a_signal_out_of_range_is_a_fault(fake):
    def too_sure(conn, request):
        _reply(conn, {"type": "SIGNAL", "v": 1, "session": "s1", "conid": C1, "t": request["t"],
                      "action": "BUY", "probability": 7.0})

    session = _open_fake_session(fake, too_sure)
    with pytest.raises(SandboxFault) as exc:
        session.bar(C1, T0, *BAR)
    assert exc.value.code == p.SANDBOX_ERROR


def test_ping_without_live_open_reports_none(fake):
    def pong(conn):
        _read_request(conn)
        _reply(conn, {"type": "PONG", "v": 1, "image_digest": "build-y", "boot_id": "b", "pool": "live",
                      "free": 1, "size": 3, "posture": {}})

    info = SandboxClient(fake(pong).path).ping()
    assert info.image_digest == "build-y" and info.live_open is None


def test_a_ping_answered_with_something_else_is_unavailable(fake):
    def not_a_pong(conn):
        _read_request(conn)
        _reply(conn, {"type": "CLOSED", "v": 1})

    with pytest.raises(SandboxUnavailable):
        SandboxClient(fake(not_a_pong).path).ping()


def test_an_open_that_never_answers_is_unavailable(fake):
    server = fake(lambda conn: threading.Event().wait(5))
    client = SandboxClient(server.path, reply_timeout_s=0.3)
    with pytest.raises(SandboxUnavailable):
        client.open_session("sha256:" + "a" * 64, "Counter", {}, "1 min", [C1])


def _signal(request, **overrides):
    return {"type": "SIGNAL", "v": 1, "session": "s1", "conid": C1, "t": request["t"],
            "action": "BUY", "probability": 0.9, **overrides}


def _assert_session_ended(session):
    with pytest.raises(SandboxProtocolError, match="closed"):
        session.bar(C1, T0 + timedelta(minutes=5), *BAR)


def test_a_signal_for_another_time_is_a_fault_and_ends_the_session(fake):
    session = _open_fake_session(
        fake, lambda conn, request: _reply(conn, _signal(request, t=(T0 - timedelta(minutes=1)).isoformat())))
    with pytest.raises(SandboxFault) as exc:
        session.bar(C1, T0, *BAR)
    assert exc.value.code == p.SANDBOX_ERROR
    _assert_session_ended(session)


def test_a_signal_for_the_same_instant_in_another_zone_is_accepted(fake):
    zone = timezone(timedelta(hours=2))
    session = _open_fake_session(
        fake, lambda conn, request: _reply(conn, _signal(request, t=T0.astimezone(zone).isoformat())))
    assert session.bar(C1, T0, *BAR) == Decision(action="BUY", probability=0.9)
    session.close()


@pytest.mark.parametrize("broken", [
    {"t": "not a time"}, {"t": None}, {"t": "2024-01-02T14:30:00"},     # unreadable or naive time
    {"action": "HOLD"}, {"probability": "high"},
])
def test_a_malformed_signal_is_a_fault_and_ends_the_session(fake, broken):
    session = _open_fake_session(fake, lambda conn, request: _reply(conn, _signal(request, **broken)))
    with pytest.raises(SandboxFault) as exc:
        session.bar(C1, T0, *BAR)
    assert exc.value.code == p.SANDBOX_ERROR
    _assert_session_ended(session)


@pytest.mark.parametrize("missing", ["action", "probability", "t", "conid", "session"])
def test_a_signal_missing_a_key_is_a_typed_error_not_a_key_error(fake, missing):
    def reply(conn, request):
        signal = _signal(request)
        del signal[missing]
        _reply(conn, signal)

    session = _open_fake_session(fake, reply)
    with pytest.raises((SandboxFault, SandboxUnavailable)):
        session.bar(C1, T0, *BAR)
    _assert_session_ended(session)


def test_an_interrupted_bar_ends_the_session(fake, monkeypatch):
    session = _open_fake_session(fake, lambda conn, request: threading.Event().wait(5))

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("trader.authored.sandbox_client._recv_exact", interrupted)
    with pytest.raises(KeyboardInterrupt):
        session.bar(C1, T0, *BAR)
    _assert_session_ended(session)


def test_a_reply_with_the_wrong_version_is_unavailable(fake):
    session = _open_fake_session(fake, lambda conn, request: _reply(conn, _signal(request, v=2)))
    with pytest.raises(SandboxUnavailable):
        session.bar(C1, T0, *BAR)
    _assert_session_ended(session)


def test_an_unknown_session_reply_ends_the_session_but_a_clock_error_does_not(fake):
    def refuse(conn, request):
        detail = p.UNKNOWN_SESSION_DETAIL if request["open"] == 9.0 else "bar time must strictly increase per conid"
        _reply(conn, {"type": "ERROR", "v": 1, "code": p.SANDBOX_BAD_REQUEST, "detail": detail})

    session = _open_fake_session(fake, refuse)
    for _ in range(2):   # a clock error leaves the session usable: the second bar reaches the server too
        with pytest.raises(SandboxProtocolError, match="strictly increase"):
            session.bar(C1, T0, *BAR)
    with pytest.raises(SandboxProtocolError, match="unknown session"):
        session.bar(C1, T0 + timedelta(minutes=1), 9.0, 10.0, 8.0, 9.5, 1.0)
    _assert_session_ended(session)


def test_the_servers_unknown_session_reply_ends_the_client_session(stack):
    session = SandboxClient(stack.live).open_session(stack.store(GOOD), "Counter", {}, "1 min", [C1])
    session._id = "s" + "0" * 16   # a session the server never opened
    with pytest.raises(SandboxProtocolError, match=p.UNKNOWN_SESSION_DETAIL):
        session.bar(C1, T0, *BAR)
    _assert_session_ended(session)


@pytest.mark.parametrize("field,value", [
    ("boot_id", 7), ("pool", None), ("free", "2"), ("free", True), ("size", 1.5), ("posture", [1]),
])
def test_a_pong_with_a_field_of_the_wrong_type_is_unavailable(fake, field, value):
    def pong(conn):
        _read_request(conn)
        _reply(conn, {"type": "PONG", "v": 1, "image_digest": "b", "boot_id": "b", "pool": "live",
                      "free": 1, "size": 3, "posture": {}, "live_open": 0, **{field: value}})

    with pytest.raises(SandboxUnavailable):
        SandboxClient(fake(pong).path).ping()


@pytest.mark.parametrize("live_open", ["0", True, -1, 1.0, None])
def test_a_live_open_that_is_not_a_count_is_unknown(fake, live_open):
    def pong(conn):
        _read_request(conn)
        _reply(conn, {"type": "PONG", "v": 1, "image_digest": "b", "boot_id": "b", "pool": "live",
                      "free": 1, "size": 3, "posture": {}, "live_open": live_open})

    assert SandboxClient(fake(pong).path).ping().live_open is None
