import ast
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from trader.sandbox.child import ForkSpawner
from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE

REPO = Path(__file__).resolve().parents[2]
THREAD_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


@pytest.fixture
def service(monkeypatch):
    """Import the entry without leaking its thread-count settings into the rest of the test run."""
    for name in THREAD_VARS:
        monkeypatch.setenv(name, "1")   # what the entry sets on import; monkeypatch restores the original
    return importlib.import_module("trader.sandbox_service")


@pytest.fixture
def socket_dir():
    path = tempfile.mkdtemp(prefix="sbx", dir="/tmp")   # macOS: a socket path is at most 104 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_the_math_libraries_are_single_threaded_before_numpy_loads():    # ruling 5
    probe = textwrap.dedent('''
        import json, os, sys
        seen = {}

        class Spy:
            def find_spec(self, name, path=None, target=None):
                if name in ("numpy", "pandas") and name not in seen:
                    seen[name] = [os.environ.get(v) for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                                                            "MKL_NUM_THREADS")]
                return None

        sys.meta_path.insert(0, Spy())
        import trader.sandbox_service
        print(json.dumps(seen))
    ''')
    env = {**os.environ, "OPENBLAS_NUM_THREADS": "8", "OMP_NUM_THREADS": "8", "MKL_NUM_THREADS": "8"}
    out = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=env, capture_output=True,
                         text=True, timeout=60, check=True).stdout
    seen = json.loads(out)
    assert seen == {"numpy": ["1", "1", "1"], "pandas": ["1", "1", "1"]}


def test_production_always_forks(service, monkeypatch):    # ruling 12
    monkeypatch.setattr(service, "read_build_id", lambda: "build-x")
    monkeypatch.setattr(service, "read_posture", lambda: {"uid": 0})
    sup = service.build_supervisor()
    assert {name: type(spawner) for name, spawner in sup._spawners.items()} == {
        "live": ForkSpawner, "research": ForkSpawner}
    assert sup._pools["live"].size == LIVE_POOL_SIZE and sup._pools["research"].size == RESEARCH_POOL_SIZE
    assert sup.pong("live")["image_digest"] == "build-x"


def test_the_entry_reads_no_knob_that_could_swap_the_spawner():    # ruling 12
    source = (REPO / "trader" / "sandbox_service.py").read_text()
    tree = ast.parse(source)
    reads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr in ("getenv", "get", "environb")
             and isinstance(node.value, (ast.Name, ast.Attribute))
             and ast.unparse(node.value) in ("os", "os.environ")]
    assert reads == []
    assert "argparse" not in source and "InProcess" not in source


_DYNAMIC_IMPORTERS = ("import_module", "__import__")


def _imported_names(node: ast.AST) -> list[str]:
    """Module names a node imports: import statements and import_module / __import__ calls on a literal."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _DYNAMIC_IMPORTERS and isinstance(node.args[0].value, str):
            return [node.args[0].value]
    return []


def _is_tests(name: str) -> bool:
    return name == "tests" or name.startswith("tests.")


def test_the_import_scan_sees_dynamic_imports():
    tree = ast.parse("import importlib\nimportlib.import_module('tests.sandbox.fakes')\n__import__('tests')\n"
                     "from importlib import import_module\nimport_module('tests.x')\n")
    assert [name for node in ast.walk(tree) for name in _imported_names(node) if _is_tests(name)] == [
        "tests.sandbox.fakes", "tests", "tests.x"]


def test_no_module_under_trader_imports_tests():    # ruling 12
    offenders = []
    for path in sorted((REPO / "trader").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            offenders += [f"{path.relative_to(REPO)}: {name}" for name in _imported_names(node) if _is_tests(name)]
    assert offenders == []


@pytest.mark.parametrize("content", [None, "", "unset\n", "has space", "-leading-dash", "x" * 200])
def test_a_missing_or_bad_build_id_refuses_to_start(service, tmp_path, content):    # ruling 1
    path = tmp_path / "mmr-build-id"
    if content is not None:
        path.write_text(content)
    with pytest.raises(SystemExit, match="refuses to start"):
        service.read_build_id(str(path))


def test_a_good_build_id_is_read(service, tmp_path):
    path = tmp_path / "mmr-build-id"
    path.write_text("abc1234+20261012T120000Z\n")
    assert service.read_build_id(str(path)) == "abc1234+20261012T120000Z"


def test_failing_to_join_group_trader_refuses_to_start(service, monkeypatch):    # ruling 15
    def refuse(groups):
        raise PermissionError("not root")

    monkeypatch.setattr(os, "setgroups", refuse)
    with pytest.raises(SystemExit, match="group trader"):
        service._join_trader_group()


def test_an_unknown_argument_is_refused(service):
    with pytest.raises(SystemExit, match="usage"):
        service.main(["--fake-spawner"])


def test_health_fails_without_a_socket(service, socket_dir):
    assert service.is_healthy(os.path.join(socket_dir, "missing.sock")) is False


def test_health_fails_on_a_stale_socket_file(service, socket_dir):
    path = os.path.join(socket_dir, "stale.sock")
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(path)
    stale.close()   # the file stays, nobody listens
    assert service.is_healthy(path) is False


def test_health_fails_on_a_peer_that_does_not_answer_pong(service, socket_dir):
    path = os.path.join(socket_dir, "odd.sock")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path)
        listener.listen(1)
        # The kernel queues the connection and the PING; nothing ever replies, so the deadline ends it.
        assert service.is_healthy(path, timeout_s=0.2) is False


@pytest.mark.asyncio
async def test_health_passes_against_a_serving_sandbox(service, socket_dir, tmp_path):
    import asyncio

    from tests.sandbox.fakes import InProcessSpawner
    from trader.sandbox.server import serve
    from trader.sandbox.supervisor import Supervisor

    sup = Supervisor(spawners={"live": InProcessSpawner()}, pool_sizes={"live": 1}, store_dir=str(tmp_path),
                     image_digest="build-x", posture={}, wiper=lambda slot: True)
    path = os.path.join(socket_dir, "live.sock")
    server = await serve(sup, {"live": path}, socket_gid=os.getgid())
    try:
        assert await asyncio.to_thread(service.is_healthy, path) is True
    finally:
        await server.close()


def test_main_health_exits_by_the_answer(service, monkeypatch):
    monkeypatch.setattr(service, "is_healthy", lambda: True)
    with pytest.raises(SystemExit) as up:
        service.main(["--health"])
    assert up.value.code == 0
    monkeypatch.setattr(service, "is_healthy", lambda: False)
    with pytest.raises(SystemExit) as down:
        service.main(["--health"])
    assert down.value.code == 1


class _FakeServer:
    def __init__(self, calls):
        self.calls = calls

    async def close(self):
        self.calls.append("close")


@pytest.fixture
def startup(service, monkeypatch):
    """_amain with every privileged step replaced by a recorder (a test must never drop its own capabilities)."""
    import signal

    from trader.sandbox import caps

    calls = []
    monkeypatch.setattr(service, "_join_trader_group", lambda: calls.append("join"))
    monkeypatch.setattr(caps, "prepare_slot_dirs", lambda uids: calls.append(("prepare", tuple(uids))))
    monkeypatch.setattr(caps, "drop_capability", lambda bit: calls.append(("drop", bit)))
    monkeypatch.setattr(caps, "require_serving_caps", lambda: calls.append("require"))
    monkeypatch.setattr(service, "read_build_id", lambda: "build-x")
    monkeypatch.setattr(service, "read_posture", lambda: calls.append("posture") or {"uid": 0})

    async def fake_serve(supervisor, sockets):
        calls.append(("serve", tuple(sorted(sockets))))
        os.kill(os.getpid(), signal.SIGTERM)   # as docker stop sends it, once the sockets listen
        return _FakeServer(calls)

    monkeypatch.setattr(service, "serve", fake_serve)
    return calls


def _run_with_timeout(service):
    import asyncio

    async def bounded():
        await asyncio.wait_for(service._amain(), timeout=10)

    asyncio.run(bounded())


def test_chown_is_dropped_before_the_posture_is_read_and_sigterm_closes_the_server(service, startup):    # ruling 2
    # The posture is read once: read before the drop, every PONG would show CHOWN and every proof would fail.
    # SIGTERM (docker stop) must close the server: stop listening, end every session, kill its child.
    from trader.sandbox import caps
    from trader.sandbox.child import SLOT_UIDS

    _run_with_timeout(service)
    assert startup == ["join", ("prepare", tuple(SLOT_UIDS)), ("drop", caps.CAP_CHOWN), "require", "posture",
                       ("serve", ("live", "research")), "close"]


def test_sigint_also_closes_the_server(service, startup, monkeypatch):
    import signal

    async def serve_then_interrupt(supervisor, sockets):
        startup.append("serve")
        os.kill(os.getpid(), signal.SIGINT)
        return _FakeServer(startup)

    monkeypatch.setattr(service, "serve", serve_then_interrupt)
    _run_with_timeout(service)
    assert startup[-2:] == ["serve", "close"]


@pytest.mark.parametrize("failing_step", ["prepare_slot_dirs", "drop_capability", "require_serving_caps"])
def test_a_failed_slot_setup_or_drop_never_serves(service, startup, monkeypatch, failing_step):
    from trader.sandbox import caps

    def fail(*args):
        raise (OSError(1, "Operation not permitted") if failing_step == "prepare_slot_dirs"
               else caps.CapabilityError("capability 0 is still effective"))

    monkeypatch.setattr(caps, failing_step, fail)
    with pytest.raises(SystemExit, match="refuses to start"):
        _run_with_timeout(service)
    assert "posture" not in startup and not any(isinstance(c, tuple) and c[0] == "serve" for c in startup)


def test_nothing_that_runs_while_serving_needs_chown():    # carry-forward: CHOWN is gone once serving
    # The socket dirs and sockets are chgrp'd only (the owner, in group trader, needs no capability); the
    # supervisor and the child never chown. Only caps.prepare_slot_dirs does, before the drop.
    chowns = {}
    for name in ("server.py", "supervisor.py", "child.py"):
        source = (REPO / "trader" / "sandbox" / name).read_text()
        chowns[name] = [ast.unparse(node) for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.Call)
                        and ast.unparse(node.func) in ("os.chown", "os.fchown", "os.lchown")]
    assert chowns["supervisor.py"] == [] and chowns["child.py"] == []
    assert chowns["server.py"] and all(", -1, " in call for call in chowns["server.py"]), chowns
