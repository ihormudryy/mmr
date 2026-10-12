import asyncio
import hashlib
import os
import shutil
import socket
import stat
import struct
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from trader.sandbox import protocol as p
from trader.sandbox import server as server_module
from trader.sandbox.server import bind_socket, serve
from trader.sandbox.supervisor import Supervisor
from tests.sandbox.fakes import InProcessSpawner

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
WAIT_S = 5.0   # upper bound for an event that should come at once; never a sleep
GOOD = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.6) if len(history) >= 2 else None
'''


def _store(tmp_path, source):
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    (tmp_path / f"{digest.split(':')[1]}.py").write_text(source)
    return digest


def _open(digest):
    return {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter", "params": {},
            "bar_size": "1 min", "conids": [101]}


def _bar(session, when=T0):
    return {"v": 1, "type": "BAR", "conid": 101, "session": session, "t": when.isoformat(),
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}


async def _client(path):
    reader, writer = await asyncio.open_unix_connection(path)
    return reader, writer


async def _call(reader, writer, obj):
    p.write_frame(writer, obj)
    await writer.drain()
    return await p.read_frame(reader)


async def _until(condition):
    """Poll a condition the server reaches on its own (a background drop); bounded, not a fixed sleep."""
    for _ in range(int(WAIT_S / 0.01)):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


async def _wait_event(event):
    assert await asyncio.to_thread(event.wait, WAIT_S)


@pytest.fixture
def socket_dir():
    # macOS caps a Unix socket path at 104 bytes; pytest's tmp_path there is longer.
    path = tempfile.mkdtemp(prefix="sbx", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest_asyncio.fixture
async def running(tmp_path, socket_dir):
    sup = Supervisor(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                     pool_sizes={"live": 2, "research": 1}, store_dir=str(tmp_path),
                     image_digest="build-x", posture={},
                     wiper=lambda slot: True)   # the real wiper would fork a helper against /tmp/s2000X
    live = os.path.join(socket_dir, "live.sock")
    research = os.path.join(socket_dir, "research.sock")
    server = await serve(sup, {"live": live, "research": research}, bar_timeout_s=0.5,
                         socket_gid=os.getgid())
    yield sup, live, research, tmp_path, server
    await server.close()


@pytest.mark.asyncio
async def test_ping_answers_pong(running):
    _, live, _, _, _ = running
    reader, writer = await _client(live)
    pong = await _call(reader, writer, {"v": 1, "type": "PING"})
    assert pong["type"] == "PONG" and pong["image_digest"] == "build-x" and pong["pool"] == "live"
    writer.close()


@pytest.mark.asyncio
async def test_each_socket_serves_its_own_pool(running):
    _, _, research, _, _ = running
    reader, writer = await _client(research)
    pong = await _call(reader, writer, {"v": 1, "type": "PING"})
    assert pong["pool"] == "research" and pong["free"] == 1
    writer.close()


@pytest.mark.asyncio
async def test_open_bar_close_over_the_socket(running):
    sup, live, _, tmp_path, _ = running
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, _open(digest))
    session = opened["session"]
    assert opened["type"] == "OPENED" and opened["image_digest"] == "build-x"
    await _call(reader, writer, _bar(session))
    sig = await _call(reader, writer, _bar(session, T0 + timedelta(minutes=1)))
    assert sig["action"] == "BUY"
    closed = await _call(reader, writer, {"v": 1, "type": "CLOSE", "session": session})
    assert closed["type"] == "CLOSED"
    assert len(sup._pools["live"].free_slots) == 2
    writer.close()


@pytest.mark.asyncio
async def test_a_dropped_connection_frees_the_slot(running):
    sup, live, _, tmp_path, _ = running
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    await _call(reader, writer, _open(digest))
    writer.close()
    await writer.wait_closed()
    assert await _until(lambda: len(sup._pools["live"].free_slots) == 2)
    assert sup._spawners["live"].closed == 1


@pytest.mark.asyncio
async def test_a_full_pool_answers_busy_at_once(running):
    _, live, _, tmp_path, _ = running
    digest = _store(tmp_path, GOOD)
    held = []
    for _ in range(2):
        reader, writer = await _client(live)
        await _call(reader, writer, _open(digest))
        held.append((reader, writer))
    reader, writer = await _client(live)
    reply = await _call(reader, writer, _open(digest))
    assert reply["code"] == p.SANDBOX_BUSY
    for _, w in held:
        w.close()
    writer.close()


@pytest.mark.asyncio
async def test_a_forged_child_reply_is_a_fault(running):
    sup, live, _, tmp_path, _ = running

    class BadChild:
        pid = -1

        def send_bar(self, conid, t, ohlcv):
            return {"action": "SIDEWAYS", "probability": 9.0}   # out of contract

        def close(self):
            return None

    class BadSpawner:
        def spawn(self, spec):
            return BadChild()

    sup._spawners["live"] = BadSpawner()
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, _open(digest))
    reply = await _call(reader, writer, _bar(opened["session"]))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_ERROR
    assert len(sup._pools["live"].free_slots) == 2
    writer.close()


class _GatedChild:
    """A child whose bar blocks until the test releases it or the supervisor kills it."""
    pid = -1

    def __init__(self):
        self.in_bar = threading.Event()
        self.release = threading.Event()
        self.killed = threading.Event()

    def send_bar(self, conid, t, ohlcv):
        self.in_bar.set()
        self.release.wait(WAIT_S)
        if self.killed.is_set():
            raise OSError("killed")
        return {"action": None, "probability": None}

    def close(self):
        self.killed.set()
        self.release.set()   # a kill ends the in-flight round-trip, as it does for a forked child


class _GatedChildSpawner:
    def __init__(self):
        self.children = []

    def spawn(self, spec):
        self.children.append(_GatedChild())
        return self.children[-1]


@pytest.mark.asyncio
async def test_a_slow_bar_hits_the_wall_timeout(running):
    sup, live, _, tmp_path, _ = running
    spawner = _GatedChildSpawner()
    sup._spawners["live"] = spawner
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, _open(digest))
    reply = await _call(reader, writer, _bar(opened["session"]))
    assert reply["code"] == p.SANDBOX_ERROR and "timeout" in reply["detail"].lower()
    assert spawner.children[0].killed.is_set()          # the overrunning child was killed
    assert len(sup._pools["live"].free_slots) == 2      # and its slot came back
    assert sup._sessions == {}
    writer.close()


@pytest.mark.asyncio
async def test_ping_answers_while_a_bar_is_in_flight(running):    # ruling 25
    sup, live, research, tmp_path, _ = running
    spawner = _GatedChildSpawner()
    sup._spawners["live"] = spawner
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, _open(digest))
    p.write_frame(writer, _bar(opened["session"]))
    await writer.drain()
    await _wait_event(spawner.children[0].in_bar)
    for path in (live, research):
        ping_reader, ping_writer = await _client(path)
        assert (await _call(ping_reader, ping_writer, {"v": 1, "type": "PING"}))["type"] == "PONG"
        ping_writer.close()
    spawner.children[0].release.set()
    assert (await p.read_frame(reader))["type"] == "SIGNAL"
    writer.close()


@pytest.mark.asyncio
async def test_ping_answers_while_a_slow_drop_is_in_progress(running, monkeypatch):
    sup, live, _, tmp_path, _ = running
    in_wipe, release_wipe = threading.Event(), threading.Event()

    def slow_wiper(slot):
        in_wipe.set()
        release_wipe.wait(WAIT_S)
        return True

    monkeypatch.setattr(sup, "_wiper", slow_wiper)
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    await _call(reader, writer, _open(digest))
    writer.close()
    await _wait_event(in_wipe)    # the drop is blocked in its wipe
    ping_reader, ping_writer = await _client(live)
    pong = await _call(ping_reader, ping_writer, {"v": 1, "type": "PING"})
    assert pong["type"] == "PONG" and pong["free"] == 1
    assert not release_wipe.is_set()
    release_wipe.set()
    assert await _until(lambda: len(sup._pools["live"].free_slots) == 2)
    ping_writer.close()


@pytest.mark.asyncio
async def test_a_failed_wipe_quarantines_the_slot(running, monkeypatch):
    sup, live, _, tmp_path, _ = running
    monkeypatch.setattr(sup, "_wiper", lambda slot: False)
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    await _call(reader, writer, _open(digest))
    writer.close()
    await writer.wait_closed()
    assert await _until(lambda: sup._pools["live"].quarantined)
    assert len(sup._pools["live"].quarantined) == 1
    assert sup.pong("live")["free"] == 1   # one slot lost


def _opened_and_ended(sup):
    spawner = sup._spawners["live"]
    return (spawner.spawned == spawner.closed == 1 and not sup._sessions
            and len(sup._pools["live"].free_slots) == 2)


def _gate_reserve(sup, monkeypatch):
    """Hold OPEN in its worker thread just before it takes a slot (the window of carry-forward c)."""
    entered, release = threading.Event(), threading.Event()
    reserve = sup._reserve

    def gated(*args):
        entered.set()
        release.wait(WAIT_S)
        return reserve(*args)

    monkeypatch.setattr(sup, "_reserve", gated)
    return entered, release


@pytest.mark.asyncio
async def test_a_connection_closed_with_an_open_in_flight_leaks_no_slot(running, monkeypatch):
    sup, live, _, tmp_path, _ = running
    entered, release = _gate_reserve(sup, monkeypatch)
    reader, writer = await _client(live)
    p.write_frame(writer, _open(_store(tmp_path, GOOD)))
    await writer.drain()
    await _wait_event(entered)
    writer.close()
    await writer.wait_closed()
    release.set()
    assert await _until(lambda: _opened_and_ended(sup))


@pytest.mark.asyncio
async def test_a_cancelled_connection_with_an_open_in_flight_leaks_no_slot(running, monkeypatch):
    sup, live, _, tmp_path, server = running
    entered, release = _gate_reserve(sup, monkeypatch)
    reader, writer = await _client(live)
    p.write_frame(writer, _open(_store(tmp_path, GOOD)))
    await writer.drain()
    await _wait_event(entered)
    for task in list(server._handlers):
        task.cancel()     # the handler's finally runs before the OPEN reaches _reserve
    await asyncio.gather(*server._handlers, return_exceptions=True)
    release.set()
    assert await _until(lambda: _opened_and_ended(sup))
    writer.close()


@pytest.mark.asyncio
async def test_closing_the_server_with_an_open_in_flight_leaks_no_slot(running, monkeypatch):
    sup, live, _, tmp_path, server = running
    entered, release = _gate_reserve(sup, monkeypatch)
    reader, writer = await _client(live)
    p.write_frame(writer, _open(_store(tmp_path, GOOD)))
    await writer.drain()
    await _wait_event(entered)
    closing = asyncio.create_task(server.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, WAIT_S)
    assert _opened_and_ended(sup)
    writer.close()


@pytest.mark.asyncio
async def test_closing_the_server_ends_every_open_session(running):
    sup, live, _, tmp_path, server = running
    reader, writer = await _client(live)
    await _call(reader, writer, _open(_store(tmp_path, GOOD)))
    await asyncio.wait_for(server.close(), WAIT_S)
    assert len(sup._pools["live"].free_slots) == 2 and not sup._sessions
    with pytest.raises(p.FrameError):
        await p.read_frame(reader)   # the server closed the connection
    writer.close()


@pytest.mark.asyncio
async def test_a_malformed_request_is_a_bad_request_and_the_connection_stays_open(running):
    _, live, _, _, _ = running
    reader, writer = await _client(live)
    reply = await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": "nope"})
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_BAD_REQUEST
    assert (await _call(reader, writer, {"v": 1, "type": "PING"}))["type"] == "PONG"
    writer.close()


@pytest.mark.asyncio
async def test_a_frame_that_is_not_json_closes_the_connection(running):
    _, live, _, _, _ = running
    reader, writer = await _client(live)
    writer.write(struct.pack(">I", 3) + b"{x}")
    await writer.drain()
    reply = await p.read_frame(reader)
    assert reply["code"] == p.SANDBOX_BAD_REQUEST
    with pytest.raises(p.FrameError):
        await p.read_frame(reader)
    writer.close()


@pytest.mark.asyncio
async def test_an_oversize_frame_header_closes_the_connection(running):
    sup, live, _, tmp_path, _ = running
    reader, writer = await _client(live)
    await _call(reader, writer, _open(_store(tmp_path, GOOD)))
    writer.write(struct.pack(">I", p.MAX_FRAME_BYTES + 1))
    await writer.drain()
    reply = await p.read_frame(reader)
    assert reply["code"] == p.SANDBOX_BAD_REQUEST and "cap" in reply["detail"]
    with pytest.raises(p.FrameError):
        await p.read_frame(reader)      # the stream is out of step: the server hung up
    assert await _until(lambda: len(sup._pools["live"].free_slots) == 2)
    writer.close()


@pytest.mark.asyncio
async def test_a_peer_that_hung_up_is_sent_nothing(running, monkeypatch):
    sup, live, _, tmp_path, server = running
    sent = []
    send = server_module._send
    monkeypatch.setattr(server_module, "_send", lambda writer, reply: (sent.append(reply), send(writer, reply)))
    reader, writer = await _client(live)
    await _call(reader, writer, _open(_store(tmp_path, GOOD)))
    assert [reply["type"] for reply in sent] == ["OPENED"]
    writer.close()
    await writer.wait_closed()
    assert await _until(lambda: not server._handlers and len(sup._pools["live"].free_slots) == 2)
    assert [reply["type"] for reply in sent] == ["OPENED"]


@pytest.mark.asyncio
async def test_serving_locks_each_socket_directory_to_the_group(running):    # ruling 15
    _, live, research, _, _ = running
    for path in (live, research):
        info = os.stat(os.path.dirname(path))
        assert stat.S_IMODE(info.st_mode) == 0o750 and info.st_gid == os.getgid()


@pytest.mark.asyncio
async def test_an_unexpected_supervisor_error_answers_and_closes(running, monkeypatch, caplog):
    sup, live, _, _, _ = running

    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(sup, "handle", broken)
    reader, writer = await _client(live)
    reply = await _call(reader, writer, {"v": 1, "type": "CLOSE", "session": "s1"})
    assert reply["code"] == p.SANDBOX_ERROR
    with pytest.raises(p.FrameError):
        await p.read_frame(reader)
    assert "boom" in caplog.text
    writer.close()


def test_bind_socket_sets_the_mode_and_group(socket_dir):
    path = os.path.join(socket_dir, "a.sock")
    sock = bind_socket(path, gid=os.getgid())
    try:
        info = os.stat(path)
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660 and info.st_gid == os.getgid()
    finally:
        sock.close()


def test_bind_socket_locks_its_directory_to_the_group(socket_dir):    # ruling 15
    os.chmod(socket_dir, 0o777)
    bind_socket(os.path.join(socket_dir, "a.sock"), gid=os.getgid()).close()
    info = os.stat(socket_dir)
    assert stat.S_IMODE(info.st_mode) == 0o750 and info.st_gid == os.getgid()


def test_bind_socket_refuses_a_directory_it_does_not_own(socket_dir, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(socket_dir).st_uid + 1)
    path = os.path.join(socket_dir, "a.sock")
    with pytest.raises(RuntimeError, match="not owned"):
        bind_socket(path, gid=os.getgid())
    assert not os.path.lexists(path)


def test_bind_socket_fails_loudly_when_it_cannot_set_the_directory_group(socket_dir, monkeypatch):
    chown = os.chown

    def refuse_the_directory(path, uid, gid):
        if path == socket_dir:
            raise PermissionError("not in group")
        chown(path, uid, gid)

    monkeypatch.setattr(os, "chown", refuse_the_directory)
    path = os.path.join(socket_dir, "a.sock")
    with pytest.raises(PermissionError):
        bind_socket(path, gid=os.getgid())
    assert not os.path.lexists(path)


def test_bind_socket_replaces_a_stale_socket(socket_dir):
    path = os.path.join(socket_dir, "a.sock")
    bind_socket(path, gid=os.getgid()).close()   # the file stays behind, as after a crash
    sock = bind_socket(path, gid=os.getgid())
    sock.close()


def test_bind_socket_refuses_to_replace_a_file_that_is_not_a_socket(socket_dir):
    path = os.path.join(socket_dir, "a.sock")
    with open(path, "w") as stream:
        stream.write("keep me")
    with pytest.raises(RuntimeError, match="not a socket"):
        bind_socket(path, gid=os.getgid())
    assert open(path).read() == "keep me"


def test_bind_socket_fails_loudly_when_it_cannot_set_the_group(socket_dir, monkeypatch):
    def refuse(*args):
        raise PermissionError("not in group")

    monkeypatch.setattr(os, "chown", refuse)
    path = os.path.join(socket_dir, "a.sock")
    with pytest.raises(PermissionError):
        bind_socket(path, gid=1000)
    with socket.socket(socket.AF_UNIX) as client, pytest.raises(OSError):
        client.connect(path)   # nothing is left listening
