"""The sandbox service entry. Always ForkSpawner (ruling 12): no option, env var or argument swaps it."""
import os
import sys

# Single-thread the math libraries before anything imports numpy (ruling 5): trader.sandbox.child
# imports pandas. Plain assignment, so a value from the environment can never win.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_var] = "1"

# Ruling 2: drop CAP_CHOWN before pandas loads, because pyarrow starts threads at import and capset changes only
# the calling thread. Only when started to serve: a --health run must not touch the slot dirs.
if __name__ == "__main__" and sys.argv[1:] == []:
    from trader.sandbox import caps as _startup_caps
    _startup_caps.prepare_slots_then_drop_chown()

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import logging  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402

from trader.sandbox import caps  # noqa: E402
from trader.sandbox import protocol as proto  # noqa: E402
from trader.sandbox.child import ChildError, ForkSpawner, recv_frame  # noqa: E402
from trader.sandbox.posture import read_posture  # noqa: E402  (Task 6)
from trader.sandbox.server import TRADER_GID, serve  # noqa: E402
from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE, Supervisor  # noqa: E402

log = logging.getLogger("mmr.sandbox")

BUILD_ID_FILE = "/etc/mmr-build-id"
HEALTH_TIMEOUT_S = 2.0
_USAGE = "usage: python -m trader.sandbox_service [--health]"
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def read_build_id(path: str = BUILD_ID_FILE) -> str:
    try:
        with open(path, encoding="utf-8") as stream:
            value = stream.read().strip()
    except OSError:
        value = ""
    if not value or value == "unset":
        raise SystemExit(f"sandbox refuses to start: {path} is unset (ruling 1)")
    if not proto.BUILD_ID_RE.fullmatch(value):
        raise SystemExit(f"sandbox refuses to start: {path} is malformed (ruling 1): {value[:40]!r}")
    return value


def build_supervisor() -> Supervisor:
    return Supervisor(
        spawners={"live": ForkSpawner(), "research": ForkSpawner()},
        pool_sizes={"live": LIVE_POOL_SIZE, "research": RESEARCH_POOL_SIZE},
        store_dir=proto.STORE_DIR,
        image_digest=read_build_id(),
        posture=read_posture(),
    )


def is_healthy(path: str = proto.LIVE_SOCKET, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    """True only when the service answers PING with PONG: a socket file alone may be stale."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect(path)
            sock.sendall(proto.encode_frame({"v": proto.PROTOCOL_VERSION, "type": "PING"}))
            reply = recv_frame(sock, timeout_s)
    except (OSError, ChildError):
        return False
    return reply.get("type") == "PONG"


def _join_trader_group() -> None:
    """Group trader lets the supervisor chgrp its sockets (ruling 15). Without it no caller can connect."""
    try:
        os.setgroups([TRADER_GID])
    except OSError as exc:
        raise SystemExit(f"sandbox refuses to start: cannot join group trader ({TRADER_GID}): {exc}") from exc


def _require_serving_caps() -> None:
    """The gate before serving, whatever path led here: the effective set must be exactly SETUID|SETGID|KILL."""
    try:
        caps.require_serving_caps()
    except caps.CapabilityError as exc:
        raise SystemExit(f"sandbox refuses to start: {exc}") from exc


@contextlib.contextmanager
def _stop_on_signals():
    """An event set by SIGTERM (docker stop, through docker-init: ruling 14) or SIGINT."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for stop_signal in _STOP_SIGNALS:
        loop.add_signal_handler(stop_signal, stop.set)
    try:
        yield stop
    finally:
        for stop_signal in _STOP_SIGNALS:
            loop.remove_signal_handler(stop_signal)


async def _amain() -> None:
    _join_trader_group()
    _require_serving_caps()           # CAP_CHOWN was dropped when the module started (ruling 2)
    supervisor = build_supervisor()   # reads the posture once, so only after the drop: PONG shows the serving caps
    with _stop_on_signals() as stop:
        server = await serve(supervisor, {"live": proto.LIVE_SOCKET, "research": proto.RESEARCH_SOCKET})
        log.info("sandbox serving live=%s research=%s", proto.LIVE_SOCKET, proto.RESEARCH_SOCKET)
        try:
            await stop.wait()
        finally:
            await server.close()   # stop listening, end every session and kill its child
    log.info("sandbox stopped")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--health"]:
        raise SystemExit(0 if is_healthy() else 1)
    if args:
        raise SystemExit(_USAGE)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
