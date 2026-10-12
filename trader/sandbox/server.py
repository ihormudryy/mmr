"""Asyncio Unix-socket server: one connection, one session, one child (ruling 7).

Every request except PING runs in a worker thread, so the event loop never blocks; PING is
answered inline and lock-free, so health always responds (ruling 25). A BAR that overruns the
wall time ends its session: the child is killed and its slot reclaimed (rulings 6, 25). When a
connection ends, for any reason, its session ends too, without a restart (ruling 7).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import socket
import stat
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from trader.sandbox import protocol as proto
from trader.sandbox.supervisor import Supervisor

log = logging.getLogger("mmr.sandbox")

TRADER_GID = 1000          # group trader: the only callers (ruling 15)
SOCKET_MODE = 0o660
# One worker per possible session (3 live + 4 research), one per drop, plus slack: a slow bar in one
# session never queues another session's request (ruling 25).
_WORKERS = 16

_connection_ids = itertools.count(1)   # unique across servers: the supervisor keys sessions on it


def bind_socket(path: str, gid: int = TRADER_GID, mode: int = SOCKET_MODE) -> socket.socket:
    """Listen on ``path`` as group ``gid`` with ``mode``. A socket the callers cannot reach is a
    broken service, so a failed chgrp raises instead of serving."""
    _remove_stale_socket(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(path)
        os.chown(path, -1, gid)   # chgrp only: the supervisor is in group trader (ruling 15)
        os.chmod(path, mode)
        sock.listen(128)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def _remove_stale_socket(path: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"{path} exists and is not a socket: refusing to replace it")
    os.unlink(path)


@dataclass
class _Connection:
    conn_id: int
    pool: str
    in_flight: Optional[Future] = None   # the one request of this connection now in a worker, if any


class _StreamOutOfStep(Exception):
    """The connection cannot carry another frame: answer if possible, then hang up."""

    def __init__(self, reply: dict):
        super().__init__(reply["detail"])
        self.reply = reply


class Server:
    def __init__(self, supervisor: Supervisor, bar_timeout_s: float):
        self._supervisor = supervisor
        self._bar_timeout_s = bar_timeout_s
        self._executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="sandbox")
        self._listeners: list[asyncio.AbstractServer] = []
        self._handlers: set[asyncio.Task] = set()
        self._drops: set[asyncio.Task] = set()
        self._closed = False

    async def listen(self, pool_name: str, path: str, gid: int) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle_connection(pool_name, reader, writer)

        listener = await asyncio.start_unix_server(handle, sock=bind_socket(path, gid=gid))
        self._listeners.append(listener)

    async def close(self) -> None:
        """Stop listening, end every connection and its session, then stop the workers."""
        if self._closed:
            return
        self._closed = True
        for listener in self._listeners:
            listener.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await asyncio.gather(*self._drops, return_exceptions=True)
        for listener in self._listeners:
            await listener.wait_closed()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── one connection ──────────────────────────────────────────────────────
    async def _handle_connection(self, pool_name: str, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter) -> None:
        conn = _Connection(conn_id=next(_connection_ids), pool=pool_name)
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            while True:
                reply = await self._next_reply(conn, reader)
                _send(writer, reply)
                await writer.drain()
        except _StreamOutOfStep as out_of_step:
            _send(writer, out_of_step.reply)   # best effort: close() flushes it if the peer still reads
        except ConnectionError:
            pass
        except Exception:  # noqa: BLE001 — a supervisor bug: answer, log it, end this connection only
            log.exception("connection %s on the %s socket failed", conn.conn_id, conn.pool)
            _send(writer, _error(proto.SANDBOX_ERROR, "internal sandbox error"))
        finally:
            self._handlers.discard(task)
            writer.close()
            self._end_session_in_background(conn)

    async def _next_reply(self, conn: _Connection, reader: asyncio.StreamReader) -> dict:
        try:
            frame = await proto.read_frame(reader)
        except proto.FrameError as exc:   # closed, oversize header or not JSON (protocol.read_frame contract)
            raise _StreamOutOfStep(_error(exc.code, exc.detail)) from exc
        try:
            request = proto.parse_request(frame)
        except proto.FrameError as exc:   # the whole frame was read: the stream is still in step
            return _error(exc.code, exc.detail)
        if request.type == "PING":
            return self._supervisor.pong(conn.pool)   # inline and lock-free (ruling 25)
        conn.in_flight = self._submit(self._supervisor.handle, conn.pool, conn.conn_id, request)
        if request.type != "BAR":
            # OPEN, OPEN_PROBE and CLOSE: a class load is bounded by ForkSpawner's open timeout (ruling 6).
            return await asyncio.wrap_future(conn.in_flight)
        return await self._bar_reply(conn.conn_id, conn.in_flight)

    async def _bar_reply(self, conn_id: int, work: Future) -> dict:
        reply = asyncio.wrap_future(work)
        done, _ = await asyncio.wait({reply}, timeout=self._bar_timeout_s)
        if done:
            return reply.result()
        # Kill the overrunning child; the kill also ends the worker's blocked round-trip (ruling 25).
        await asyncio.wrap_future(self._submit(self._supervisor.drop_connection, conn_id))
        return _error(proto.SANDBOX_ERROR, f"bar timeout after {self._bar_timeout_s}s")

    def _end_session_in_background(self, conn: _Connection) -> None:
        """Drop the connection's session in a worker (a drop kills, reaps and wipes: never on the loop).

        Its own task, so cancelling the handler cannot skip it. An OPEN still in a worker now may take a
        slot after the first drop, so the connection is dropped again once that request returns."""
        # Read now, before the first drop starts: a request that finishes later may have taken a slot.
        pending = conn.in_flight if conn.in_flight is not None and not conn.in_flight.done() else None

        async def drop() -> None:
            try:
                await asyncio.wrap_future(self._submit(self._supervisor.drop_connection, conn.conn_id))
                if pending is not None:
                    await asyncio.gather(asyncio.wrap_future(pending), return_exceptions=True)
                    await asyncio.wrap_future(self._submit(self._supervisor.drop_connection, conn.conn_id))
            except Exception:  # noqa: BLE001 — a failed drop must be seen, not lost in a task
                log.exception("ending the session of connection %s failed", conn.conn_id)

        task = asyncio.get_running_loop().create_task(drop())
        self._drops.add(task)
        task.add_done_callback(self._drops.discard)

    def _submit(self, fn: Callable, *args) -> Future:
        return self._executor.submit(fn, *args)


async def serve(supervisor: Supervisor, sockets: Mapping[str, str], *, bar_timeout_s: float = 2.0,
                socket_gid: int = TRADER_GID) -> Server:
    """Listen on each pool's socket (``sockets`` maps a pool name to its path)."""
    server = Server(supervisor, bar_timeout_s)
    try:
        for pool_name, path in sockets.items():
            await server.listen(pool_name, path, socket_gid)
    except BaseException:
        await server.close()
        raise
    return server


def _send(writer: asyncio.StreamWriter, reply: dict) -> None:
    try:
        proto.write_frame(writer, reply)
    except proto.FrameError as exc:   # a reply that cannot be encoded is itself a sandbox fault
        proto.write_frame(writer, _error(exc.code, exc.detail))


def _error(code: str, detail: str) -> dict:
    return {"type": "ERROR", "v": proto.PROTOCOL_VERSION, "code": code, "detail": detail}
