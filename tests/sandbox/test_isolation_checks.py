import asyncio
import os
import shutil
import tempfile
import threading

import pytest

from trader.authored.sandbox_client import (SandboxClient, SandboxFault, SandboxProtocolError,
                                            SandboxUnavailable)
from trader.sandbox import isolation_checks as ic
from trader.sandbox.child import ChildError, ChildRuntime
from trader.sandbox.server import serve
from trader.sandbox.supervisor import ESCAPE_DIR, Supervisor
from tests.sandbox.fakes import InProcessSpawner


def _bar_fault(name):
    """The detail the real child gives when this escape is contained at BAR (ic.EXPECTED_FAULT_MARKERS)."""
    return f"on_bar raised {ic.EXPECTED_FAULT_MARKERS[name][0]}: fake"


class _FakeSession:
    def __init__(self, fault=None):
        self.fault = fault             # None: the bar answers; else the SandboxFault detail to raise
        self.bars = 0

    def bar(self, conid, *a, **k):
        assert conid == 1      # a probe session holds PROBE_CONID only
        if self.fault is not None:
            raise SandboxFault(self.fault, code="SANDBOX_ERROR")
        self.bars += 1
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


class _FakeInfo:
    image_digest = "build-x"
    boot_id = "b"
    pool = "research"
    free = 4
    size = 4
    live_open = 0
    posture = {"cap_eff": ic.EXPECTED_CAP_EFF, "seccomp": 2, "no_new_privs": 1, "uid": 0,
               "interfaces": ["lo"], "forbidden_env": [], "home_mounts": [],
               "root_read_only": True, "pids_max": "64"}


class _FakeClient:
    """Benign probes run; every hostile escape opens and faults at BAR for its own reason; posture is clean."""

    def __init__(self):
        self.pings = 0

    def ping(self):
        self.pings += 1
        return _FakeInfo()

    def open_probe(self, kind, name):
        return _FakeSession(None if kind == "probe" else _bar_fault(name))


def check_of(result, name):
    return next(c for c in result.checks if c.name == name)


def test_a_clean_service_passes_every_check():
    result = ic.run_isolation_checks(_FakeClient())
    assert result.passed is True and result.image_digest == "build-x"
    assert {c.name for c in result.checks} >= set(ic.HOSTILE_ESCAPES) | {
        "benign_session", "cap_eff", "seccomp", "network_none", "read_only_root", "pids_limit"}
    assert set(ic.EXPECTED_FAULT_MARKERS) == set(ic.HOSTILE_ESCAPES)


def test_the_hostile_names_are_plan_1s_files():
    from tests.authored.test_escape_corpus import ESCAPE_DIR, SANDBOX_HOSTILE

    assert ic.HOSTILE_ESCAPES == SANDBOX_HOSTILE
    assert all((ESCAPE_DIR / f"{name}.py").is_file() for name in ic.HOSTILE_ESCAPES)


def test_an_open_time_fault_is_never_containment():                                     # review r1 #3
    class NeverOpens(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "escape":
                raise SandboxFault("load failed: NameError: __build_class__ not found", code="SANDBOX_ERROR")
            return _FakeSession()

    result = ic.run_isolation_checks(NeverOpens())
    assert result.passed is False
    assert all(check_of(result, name).detail.startswith("never OPENED") for name in ic.HOSTILE_ESCAPES)


@pytest.mark.parametrize("detail", ["load failed: SyntaxError", "constructor failed: TypeError: x",
                                    "on_bar raised KeyError: 'close'"])
def test_a_bar_fault_that_the_escape_does_not_explain_is_not_containment(detail):
    class Unexplained(_FakeClient):
        def open_probe(self, kind, name):
            return _FakeSession(None if kind == "probe" else detail)

    result = ic.run_isolation_checks(Unexplained())
    assert result.passed is False
    assert not any(check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_an_escape_that_kills_the_neighbour_fails():
    class KillsNeighbour(_FakeClient):
        def __init__(self):
            super().__init__()
            self.benign = []

        def open_probe(self, kind, name):
            if kind == "probe":
                session = _FakeSession()
                self.benign.append(session)
                return session
            for held in self.benign:
                held.fault = "child pipe closed"           # the escape killed the neighbour
            return _FakeSession(_bar_fault(name))

    result = ic.run_isolation_checks(KillsNeighbour())
    check = check_of(result, "escape_kill_other_child")
    assert check.passed is False and "neighbour" in check.detail


def test_an_escape_after_which_ping_fails_fails():
    class DiesAfterEscape(_FakeClient):
        def ping(self):
            self.pings += 1
            if self.pings > 1:
                raise SandboxUnavailable("supervisor gone")
            return _FakeInfo()

    result = ic.run_isolation_checks(DiesAfterEscape())
    assert result.passed is False
    assert "PING" in check_of(result, ic.HOSTILE_ESCAPES[0]).detail


def test_a_hostile_escape_that_does_not_fault_fails_the_run():
    class Leaky(_FakeClient):
        def open_probe(self, kind, name):
            return _FakeSession()                # nothing faults: the sandbox did not contain it

    result = ic.run_isolation_checks(Leaky())
    assert result.passed is False
    assert all(check_of(result, name).detail == "ran to completion: NOT contained" for name in ic.HOSTILE_ESCAPES)


def test_bad_posture_fails_the_run():
    class BadCaps(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.posture = dict(info.posture, cap_eff="0000000000000fff")   # too many caps
            return info

    result = ic.run_isolation_checks(BadCaps())
    assert result.passed is False
    assert any(c.name == "cap_eff" and not c.passed for c in result.checks)


@pytest.mark.parametrize("key,value,check", [("root_read_only", False, "read_only_root"),
                                             ("pids_max", "max", "pids_limit"),
                                             ("pids_max", "", "pids_limit")])
def test_a_writable_root_or_a_missing_pids_limit_fails_the_run(key, value, check):
    class Loose(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.posture = dict(info.posture, **{key: value})
            return info

    result = ic.run_isolation_checks(Loose())
    assert result.passed is False
    assert any(c.name == check and not c.passed for c in result.checks)


# ── fail closed: a missing, unknown or odd posture value is a failed check ──

_DROP = object()   # remove the key from the posture


def _with_posture(**changes):
    class Posture(_FakeClient):
        def ping(self):
            self.pings += 1
            info = _FakeInfo()
            info.posture = {**_FakeInfo.posture, **changes}
            for key, value in changes.items():
                if value is _DROP:
                    del info.posture[key]
            return info
    return Posture()


@pytest.mark.parametrize("key,value,check", [
    ("cap_eff", _DROP, "cap_eff"),
    ("cap_eff", "00000000000000e1", "cap_eff"),          # CAP_CHOWN not dropped (ruling 2)
    ("cap_eff", "1000000000000000e0", "cap_eff"),        # a bit past 64 must not hide
    ("cap_eff", "0xe0", "cap_eff"),
    ("seccomp", _DROP, "seccomp"),
    ("seccomp", None, "seccomp"),
    ("seccomp", 0, "seccomp"),
    ("seccomp", 2.0, "seccomp"),
    ("no_new_privs", _DROP, "no_new_privs"),
    ("no_new_privs", True, "no_new_privs"),
    ("interfaces", _DROP, "network_none"),
    ("interfaces", [], "network_none"),                  # nothing read is not "only lo"
    ("interfaces", ["<unreadable>"], "network_none"),
    ("interfaces", ["eth0", "lo"], "network_none"),
    ("interfaces", "lo", "network_none"),
    ("forbidden_env", _DROP, "no_forbidden_env"),
    ("forbidden_env", None, "no_forbidden_env"),
    ("forbidden_env", ["ALPACA_API_KEY_ID"], "no_forbidden_env"),
    ("home_mounts", _DROP, "no_home_mounts"),
    ("home_mounts", ["/home/trader/.config"], "no_home_mounts"),
    ("home_mounts", ["<unreadable>"], "no_home_mounts"),
    ("root_read_only", _DROP, "read_only_root"),
    ("root_read_only", 1, "read_only_root"),
    ("pids_max", _DROP, "pids_limit"),
    ("pids_max", 64, "pids_limit"),
])
def test_a_missing_or_unknown_posture_value_fails_its_check(key, value, check):
    result = ic.run_isolation_checks(_with_posture(**{key: value}))
    assert result.passed is False
    assert check_of(result, check).passed is False


_DOWN = {"operstate": "down", "flags": 0x80, "ipv4": False, "ipv6": False}
_LO = {"operstate": "unknown", "flags": 0x9, "ipv4": True, "ipv6": True}


def test_only_lo_passes_the_network_check():
    result = ic.run_isolation_checks(_with_posture(interfaces=["lo"], interface_state={"lo": _LO}))
    assert check_of(result, "network_none").passed is True


def test_lo_and_down_fallback_tunnels_without_addresses_pass():                         # ruling 33
    result = ic.run_isolation_checks(_with_posture(
        interfaces=["ip6tnl0", "lo", "tunl0"],
        interface_state={"lo": _LO, "tunl0": _DOWN, "ip6tnl0": dict(_DOWN)}))
    assert check_of(result, "network_none").passed is True and result.passed is True


@pytest.mark.parametrize("interfaces,state", [
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "operstate": "up"}}),           # tunnel up
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "flags": 0x81}}),               # IFF_UP set
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv4": True}}),                # has an IPv4 address
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv6": True}}),                # has an IPv6 address
    (["eth0", "lo"], {"eth0": _DOWN}),                                    # not a fallback tunnel
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "operstate": None}}),           # unreadable state
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "flags": None}}),
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv4": None}}),
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "ipv6": None}}),
    (["lo", "tunl0"], {"tunl0": {**_DOWN, "flags": True}}),
    (["lo", "tunl0"], {}),                                                # no state for the device
    (["lo", "tunl0"], _DROP),                                             # no state at all
    (["lo", "tunl0"], "down"),
    (["tunl0"], {"tunl0": _DOWN}),                                        # no lo
    (["lo", ["tunl0"]], {"tunl0": _DOWN}),                                # not a name
])
def test_any_other_interface_or_an_up_addressed_or_unknown_tunnel_fails(interfaces, state):
    result = ic.run_isolation_checks(_with_posture(interfaces=interfaces, interface_state=state))
    assert result.passed is False and check_of(result, "network_none").passed is False


def test_the_store_mount_is_the_only_mount_allowed():
    result = ic.run_isolation_checks(_with_posture(home_mounts=["/store"]))
    assert result.passed is True and check_of(result, "no_home_mounts").passed


@pytest.mark.parametrize("build_id", ["", "unset sha", "-leading-dash"])
def test_a_malformed_build_id_fails_the_run(build_id):
    class OddBuild(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.image_digest = build_id
            return info

    result = ic.run_isolation_checks(OddBuild())
    assert result.passed is False and check_of(result, "build_id").passed is False


@pytest.mark.parametrize("boot_id", ["", None, 7])
def test_a_baseline_without_a_boot_id_fails_the_run(boot_id):
    # Without a boot id, a supervisor restarted by an escape could not be told apart.
    class NoBootId(_FakeClient):
        def ping(self):
            info = _FakeInfo()
            info.boot_id = boot_id
            return info

    result = ic.run_isolation_checks(NoBootId())
    assert result.passed is False and check_of(result, "boot_id").passed is False


def test_open_socket_counts_only_as_network_unreachable():
    assert ic.EXPECTED_FAULT_MARKERS["escape_open_socket"] == ("Network is unreachable",)

    class OtherOSError(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            if name == "escape_open_socket":
                return _FakeSession("on_bar raised OSError: [Errno 9] Bad file descriptor")
            return _FakeSession(_bar_fault(name))

    result = ic.run_isolation_checks(OtherOSError())
    assert result.passed is False and check_of(result, "escape_open_socket").passed is False


# ── live sessions: no probe ever runs beside a live child (rulings 21, 29) ──

@pytest.mark.parametrize("live_open", [None, 1])
def test_an_open_or_unknown_live_session_runs_no_probe(live_open):
    class LiveBusy(_FakeClient):
        def __init__(self):
            super().__init__()
            self.opened = []

        def ping(self):
            info = _FakeInfo()
            info.live_open = live_open
            return info

        def open_probe(self, kind, name):
            self.opened.append((kind, name))
            return super().open_probe(kind, name)

    client = LiveBusy()
    result = ic.run_isolation_checks(client)
    assert result.passed is False and client.opened == []
    assert check_of(result, "live_sessions_closed").passed is False
    assert not any(c.name in ic.HOSTILE_ESCAPES for c in result.checks)


def test_a_live_session_that_opens_during_the_run_fails_the_escape_it_overlapped():
    class LiveOpensLater(_FakeClient):
        def ping(self):
            self.pings += 1
            info = _FakeInfo()
            info.live_open = 0 if self.pings == 1 else 1
            return info

    result = ic.run_isolation_checks(LiveOpensLater())
    assert result.passed is False
    assert "live" in check_of(result, ic.HOSTILE_ESCAPES[0]).detail


def test_a_supervisor_that_restarted_during_an_escape_fails_it():
    class Restarts(_FakeClient):
        def ping(self):
            self.pings += 1
            info = _FakeInfo()
            info.boot_id = "b" if self.pings == 1 else "b-new"   # killed, and docker started a new one
            return info

    result = ic.run_isolation_checks(Restarts())
    assert result.passed is False
    assert all("restarted" in check_of(result, name).detail for name in ic.HOSTILE_ESCAPES)


# ── an unexpected error anywhere is a failed check, never a crash or a pass ──

def test_an_unexpected_error_in_a_hostile_check_is_a_failed_check():
    class Broken(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "escape":
                raise RuntimeError("bug")
            return _FakeSession()

    result = ic.run_isolation_checks(Broken())
    assert result.passed is False
    assert all(not check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_an_unexpected_error_while_attributing_the_fault_is_a_failed_check():
    class OddFault(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            session = _FakeSession()
            session.bar = lambda *a, **k: (_ for _ in ()).throw(ValueError("odd"))
            return session

    result = ic.run_isolation_checks(OddFault())
    assert result.passed is False
    assert all(check_of(result, name).detail.startswith("no fault at BAR") for name in ic.HOSTILE_ESCAPES)


def test_a_refused_benign_bar_fails_the_benign_check():
    class Refuses(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                raise SandboxProtocolError("probes are research-only")
            return super().open_probe(kind, name)

    result = ic.run_isolation_checks(Refuses())
    assert result.passed is False and check_of(result, "benign_session").passed is False


def test_a_fault_with_another_code_is_not_containment():
    class Mismatch(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            session = _FakeSession()

            def bar(*a, **k):
                raise SandboxFault(_bar_fault(name), code="SANDBOX_DIGEST_MISMATCH")
            session.bar = bar
            return session

    result = ic.run_isolation_checks(Mismatch())
    assert not any(check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_a_supervisor_side_error_naming_the_marker_is_not_containment():
    # The supervisor answers "child failed: <Type>" for its own bugs; that is not the escape faulting.
    class SupervisorBug(_FakeClient):
        def open_probe(self, kind, name):
            if kind == "probe":
                return _FakeSession()
            return _FakeSession(f"child failed: {ic.EXPECTED_FAULT_MARKERS[name][0]}")

    result = ic.run_isolation_checks(SupervisorBug())
    assert not any(check_of(result, name).passed for name in ic.HOSTILE_ESCAPES)


def test_an_unknown_escape_name_is_a_failed_check():
    check = ic.hostile_check(_FakeClient(), "escape_not_in_the_list")
    assert check.passed is False


# ── over the real wire: client -> asyncio server -> supervisor ─────────────

@pytest.fixture
def socket_dir():
    path = tempfile.mkdtemp(prefix="sbx", dir="/tmp")   # macOS caps a socket path at 104 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


# What each escape's fault looks like when the real forked child stops it (rulings 3, 5; seccomp; network none).
_CONTAINED_DETAIL = {
    "escape_read_config_keys": "on_bar raised FileNotFoundError: [Errno 2] No such file or directory: "
                               "'/home/trader/.config/mmr/keys/rpc/trader.key'",
    "escape_write_outside_tmpfs": "on_bar raised OSError: [Errno 30] Read-only file system: '/store/escape.csv'",
    "escape_fork_bomb": "on_bar raised BlockingIOError: [Errno 11] Resource temporarily unavailable",
    "escape_allocate_past_limit": "on_bar raised _ArrayMemoryError: Unable to allocate 64.0 GiB for an array",
    "escape_loop_forever": None,   # hangs: the server's wall timeout ends it
    "escape_pd_io_common_oskill": "on_bar raised PermissionError: [Errno 1] Operation not permitted",
    "escape_kill_other_child": "on_bar raised PermissionError: [Errno 1] Operation not permitted",
    "escape_sys_modules_lookup": "on_bar raised PermissionError: [Errno 1] Operation not permitted",
    "escape_open_socket": "on_bar raised OSError: [Errno 101] Network is unreachable",
}


class _ScriptedEscape:
    """Stands in for a forked child running an escape. It never runs the escape's code: it only gives the
    fault the real child gives, or hangs until it is killed."""
    pid = -1

    def __init__(self, detail):
        self._detail = detail
        self._killed = threading.Event()

    def send_bar(self, conid, t, ohlcv):
        if self._detail is None:
            self._killed.wait(10)
            raise ChildError("child pipe closed")
        raise ChildError(self._detail)

    def close(self):
        self._killed.set()


class _BenignChild:
    pid = -1

    def __init__(self, spec):
        self._runtime = ChildRuntime(source=spec.source, class_name=spec.class_name, params=spec.params,
                                     lookback=spec.lookback, conids=spec.conids, skip_static_check=False)
        self._runtime.load()

    def send_bar(self, conid, t, ohlcv):
        return self._runtime.on_bar(conid, t, ohlcv)

    def close(self):
        return None


class _ScriptedSpawner:
    def __init__(self):
        self._escapes = {(ESCAPE_DIR / f"{name}.py").read_text(encoding="utf-8"): name
                         for name in ic.HOSTILE_ESCAPES}

    def spawn(self, spec):
        name = self._escapes.get(spec.source)
        if name is None:
            return _BenignChild(spec)   # the static check still runs: only a clean probe loads here
        return _ScriptedEscape(_CONTAINED_DETAIL[name])


class _Stack:
    def __init__(self, socket_dir, research_spawner, posture):
        self.research = os.path.join(socket_dir, "research.sock")
        live = os.path.join(socket_dir, "live.sock")
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        sup = Supervisor(spawners={"live": InProcessSpawner(), "research": research_spawner},
                         pool_sizes={"live": 1, "research": 2}, store_dir=socket_dir,
                         image_digest="build-x", posture=posture, wiper=lambda slot: True)

        def run():
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(
                serve(sup, {"live": live, "research": self.research}, bar_timeout_s=1.0,
                      socket_gid=os.getgid()))
            self._loop.call_soon(ready.set)
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self._server.close(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()


def test_the_real_wire_passes_a_contained_service(socket_dir):
    stack = _Stack(socket_dir, _ScriptedSpawner(), dict(_FakeInfo.posture))
    try:
        result = ic.run_isolation_checks(SandboxClient(stack.research))
    finally:
        stack.stop()
    assert [c for c in result.checks if not c.passed] == []
    assert result.passed is True and result.image_digest == "build-x"
    assert "bar timeout" in check_of(result, "escape_loop_forever").detail


def test_the_real_wire_with_the_in_process_spawner_proves_nothing(socket_dir):
    # The in-process spawner never skips the static check, so every escape is refused at OPEN: not containment.
    stack = _Stack(socket_dir, InProcessSpawner(), dict(_FakeInfo.posture))
    try:
        result = ic.run_isolation_checks(SandboxClient(stack.research))
    finally:
        stack.stop()
    assert result.passed is False and check_of(result, "benign_session").passed is True
    assert all(check_of(result, name).detail.startswith("never OPENED") for name in ic.HOSTILE_ESCAPES)
