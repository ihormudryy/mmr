"""Container checks of the real sandbox stanza (spec 7.5, 7.9, 10; SP3 Plan 2 Task 11).

Runs only inside the `fullstack-tests` container, against its own compose project, never the live stack:

  scripts/run_sandbox_fullstack.sh

Outside that container every test skips. Inside it, a sandbox that never answers FAILS the run; it never skips.

pytest runs this file top to bottom, and the order matters: the static posture first, then live sessions (each
closed before any probe, ruling 29), then probes and the hostile escapes, then the leftover-process checks, and
last the stop/start cycle, which restarts the sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket as _socket
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

docker = pytest.importorskip("docker", reason="docker-py not installed")

from trader.authored.sandbox_client import (  # noqa: E402
    SandboxClient, SandboxFault, SandboxProtocolError, SandboxUnavailable,
)
from trader.sandbox import protocol as proto  # noqa: E402
from trader.sandbox.caps import SBXCHILD_GID, SLOT_UIDS  # noqa: E402
from trader.sandbox.isolation_checks import (  # noqa: E402
    EXPECTED_CAP_EFF, EXPECTED_PIDS_MAX, FALLBACK_TUNNEL_DEVICES, HOSTILE_ESCAPES, hostile_check,
    run_isolation_checks,
)

SBXTEST_PROJECT_PREFIX = "mmr-sbxtest"
TRADER_GID = 1000
NO_CAPS = "0000000000000000"
READY_TIMEOUT_S = 120.0
RESTART_TIMEOUT_S = 120.0
SETTLE_TIMEOUT_S = 15.0
T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
BENIGN = '''
from trader.authored.api import AuthoredStrategy, Decision


class Edge(AuthoredStrategy):
    LOOKBACK_BARS = 10

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.5) if len(history) >= 3 else None
'''


def _skip_reason() -> str | None:
    if os.environ.get("MMR_FAKE_BROKER") != "1":
        return "runs only in the fullstack-tests container (scripts/run_sandbox_fullstack.sh sets MMR_FAKE_BROKER=1)"
    if not Path("/var/run/docker.sock").exists():
        return "requires the host Docker socket bind-mounted at /var/run/docker.sock"
    if not Path(proto.LIVE_SOCKET).parent.is_dir():
        return "the sandbox socket volumes are not mounted in this container"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


# ── fixtures and helpers ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    c = docker.from_env()
    yield c
    c.close()


@pytest.fixture(scope="module")
def sandbox(client):
    return _sandbox_container(client)


@pytest.fixture(scope="module", autouse=True)
def boot(sandbox):
    """The first PONG once both sockets answer. A sandbox that never answers fails the run (no skip)."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            SandboxClient(proto.LIVE_SOCKET).ping()
            return SandboxClient(proto.RESEARCH_SOCKET).ping()
        except SandboxUnavailable as exc:
            last_error = exc
            time.sleep(1.0)
    pytest.fail(f"the sandbox did not answer PING within {READY_TIMEOUT_S}s: {last_error}")


def _sandbox_container(client):
    me = client.containers.get(_socket.gethostname())
    project = me.labels["com.docker.compose.project"]
    if not project.startswith(SBXTEST_PROJECT_PREFIX):
        # Ruling 30: these tests write to the store and open sessions; never in a project that could be live.
        pytest.skip(f"compose project {project!r} is not {SBXTEST_PROJECT_PREFIX}*: "
                    "run scripts/run_sandbox_fullstack.sh")
    found = client.containers.list(filters={"label": [
        f"com.docker.compose.project={project}", "com.docker.compose.service=sandbox"]})
    assert len(found) == 1, "expected one sandbox container"
    return found[0]


def _exec(container, command: list[str], *, user: str = "") -> tuple[int, str]:
    result = container.exec_run(command, user=user, demux=False)
    return result.exit_code, (result.output or b"").decode("utf-8", errors="replace")


def _python(container, source: str, *, user: str = "") -> tuple[int, str]:
    return _exec(container, ["python", "-c", source], user=user)


# Every process in the container except the reader itself: pid, cmdline and its /proc status fields.
_PROCESS_TABLE = r'''
import json, os
rows = []
for pid in [name for name in os.listdir("/proc") if name.isdigit() and int(name) != os.getpid()]:
    try:
        with open(f"/proc/{pid}/status") as stream:
            status = dict(line.rstrip("\n").split(":", 1) for line in stream if ":" in line)
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            cmdline = stream.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        continue
    rows.append({"pid": int(pid), "cmdline": cmdline, "status": {k: v.strip() for k, v in status.items()}})
print(json.dumps(rows))
'''


def _processes(container) -> list[dict]:
    code, out = _python(container, _PROCESS_TABLE)
    assert code == 0, out
    return json.loads(out)


def _real_uid(process: dict) -> int:
    return int(process["status"]["Uid"].split()[0])


def _supervisor(container) -> dict:
    """docker-init's direct child (ruling 14), never `pgrep -f | first`: that also matches children and health runs."""
    found = [p for p in _processes(container)
             if p["status"].get("PPid") == "1" and "trader.sandbox_service" in p["cmdline"]
             and "--health" not in p["cmdline"]]
    assert len(found) == 1, found
    return found[0]


def _slot_processes(container) -> list[dict]:
    return [p for p in _processes(container) if _real_uid(p) in SLOT_UIDS]


def _wait_until_no_slot_process(container, timeout_s: float = SETTLE_TIMEOUT_S) -> list[dict]:
    """A finished session's child is killed and its wipe helper exits; poll briefly, then return what is left."""
    deadline = time.monotonic() + timeout_s
    left = _slot_processes(container)
    while left and time.monotonic() < deadline:
        time.sleep(0.25)
        left = _slot_processes(container)
    return left


def _limits(container, pid: int) -> dict[str, tuple[str, str]]:
    code, out = _exec(container, ["cat", f"/proc/{pid}/limits"])
    assert code == 0, out
    rows = {}
    for line in out.splitlines()[1:]:
        fields = re.split(r"\s{2,}", line.strip())
        if len(fields) >= 3:
            rows[fields[0]] = (fields[1], fields[2])
    return rows


def _store(source: str) -> str:
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    Path(proto.STORE_DIR, f"{digest.split(':')[1]}.py").write_text(source)
    return digest


def _bars(session, conid: int, count: int, start: int = 0) -> list:
    return [session.bar(conid, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)
            for i in range(start, start + count)]


def _seccomp_profile(sandbox) -> dict:
    options = sandbox.attrs["HostConfig"]["SecurityOpt"] or []
    profiles = [opt.split("=", 1)[1] for opt in options if opt.startswith("seccomp=")]
    assert len(profiles) == 1, options
    return json.loads(profiles[0])


# ── the stanza as Docker runs it ────────────────────────────────────────────

def test_sandbox_capabilities_are_exactly_the_four_at_boot(sandbox):
    host = sandbox.attrs["HostConfig"]
    assert {c.upper() for c in host["CapDrop"]} == {"ALL"}
    added = {c.upper().removeprefix("CAP_") for c in host["CapAdd"]}
    assert added == {"CHOWN", "SETUID", "SETGID", "KILL"}
    assert host["NetworkMode"] == "none"
    assert host["IpcMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Init"] is True
    assert host["PidsLimit"] == int(EXPECTED_PIDS_MAX)
    assert host["Privileged"] is False
    assert any("no-new-privileges" in opt for opt in host["SecurityOpt"])
    assert {u["Name"]: (u["Soft"], u["Hard"]) for u in host["Ulimits"]}["nofile"] == (1024, 1024)


def test_the_seccomp_option_is_the_repo_profile_without_the_inspection_syscalls(sandbox):
    # Ruling 17: compose resolved ./config_defaults/sandbox-seccomp.json from the repo root, and Docker loaded it.
    loaded = _seccomp_profile(sandbox)
    shipped = json.loads((Path(__file__).resolve().parents[2] / "config_defaults" / "sandbox-seccomp.json")
                         .read_text())
    assert loaded == shipped
    allowed = {name for rule in loaded["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" for name in rule["names"]}
    assert not {"ptrace", "process_vm_readv", "process_vm_writev", "kcmp", "pidfd_open"} & allowed


def test_the_entrypoint_is_cleared_and_docker_init_runs_the_service(sandbox):
    config = sandbox.attrs["Config"]
    assert not config.get("Entrypoint")                                    # entrypoint: [] (ruling 14)
    assert config["Cmd"] == ["python", "-m", "trader.sandbox_service"]
    processes = {p["pid"]: p for p in _processes(sandbox)}
    assert "docker-init" in processes[1]["cmdline"]
    assert not any("docker-entrypoint" in p["cmdline"] for p in processes.values())
    assert _supervisor(sandbox)["cmdline"] == "python -m trader.sandbox_service"


def test_sandbox_env_has_no_provider_or_broker_names(sandbox):
    env = sandbox.attrs["Config"]["Env"]
    names = [entry.split("=", 1)[0] for entry in env]
    for forbidden in ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_", "AWS_", "AZURE_",
                      "TRADING_MODE"):
        assert not [n for n in names if n.startswith(forbidden)], names


def test_the_serving_supervisor_holds_exactly_kill_setgid_setuid(sandbox):
    # Ruling 2: the real capset dropped CAP_CHOWN from the effective AND the permitted set before serving. The drop
    # refuses to run with more than one thread, so a serving supervisor is also the proof Threads was 1 then;
    # it is not 1 now (the server has its thread pool), so it is not read here.
    status = _supervisor(sandbox)["status"]
    assert status["CapEff"] == EXPECTED_CAP_EFF
    assert status["CapPrm"] == EXPECTED_CAP_EFF
    assert status["CapInh"] == NO_CAPS and status["CapAmb"] == NO_CAPS
    assert status["Seccomp"] == "2"
    assert status["NoNewPrivs"] == "1"
    assert status["Uid"].split() == ["0"] * 4
    assert status["Groups"].split() == [str(TRADER_GID)]                     # ruling 15


def test_every_slot_directory_is_owned_by_its_slot_uid(sandbox):
    for uid in SLOT_UIDS:
        code, out = _exec(sandbox, ["stat", "-c", "%u:%g %a", f"/tmp/s{uid}"])
        assert code == 0, out
        assert out.strip() == f"{uid}:{SBXCHILD_GID} 700"


def test_the_tmpfs_and_the_root_carry_their_mount_options(sandbox):
    code, mounts = _exec(sandbox, ["cat", "/proc/mounts"])
    assert code == 0, mounts
    tmp = [line.split() for line in mounts.splitlines() if line.split()[1:2] == ["/tmp"]]
    assert len(tmp) == 1 and tmp[0][2] == "tmpfs", tmp                   # ruling 23: the short form's options
    options = set(tmp[0][3].split(","))
    assert {"noexec", "nosuid", "nodev", "size=65536k", "mode=755"} <= options, options
    root = [line.split() for line in mounts.splitlines() if line.split()[1:2] == ["/"]]
    assert root and all("ro" in fields[3].split(",") for fields in root), root


def test_the_socket_directories_are_root_trader_0750_and_the_sockets_0660(boot):
    for socket_path in (proto.LIVE_SOCKET, proto.RESEARCH_SOCKET):
        directory = os.stat(Path(socket_path).parent)
        assert (directory.st_uid, directory.st_gid, stat.S_IMODE(directory.st_mode)) == (0, TRADER_GID, 0o750)
        sock = os.stat(socket_path)
        assert stat.S_ISSOCK(sock.st_mode)
        assert (sock.st_uid, sock.st_gid, stat.S_IMODE(sock.st_mode)) == (0, TRADER_GID, 0o660)


_DENIED_SYSCALLS = r'''
import ctypes, json, os, platform
libc = ctypes.CDLL(None, use_errno=True)
def errno_of(result):
    return ctypes.get_errno() if result == -1 else 0
found = {}
try:
    os.close(os.pidfd_open(os.getpid()))
    found["pidfd_open"] = 0
except OSError as exc:
    found["pidfd_open"] = exc.errno
found["ptrace"] = errno_of(libc.ptrace(0, 0, 0, 0))                        # PTRACE_TRACEME
found["process_vm_readv"] = errno_of(libc.process_vm_readv(os.getpid(), None, 0, None, 0, 0))
kcmp = {"x86_64": 312, "aarch64": 272}[platform.machine()]
found["kcmp"] = errno_of(libc.syscall(kcmp, os.getpid(), os.getpid(), 1, 0, 0))   # KCMP_VM
print(json.dumps(found))
'''


def test_the_custom_seccomp_profile_denies_the_inspection_syscalls(sandbox):
    # Docker's default profile ALLOWS these; only our derived profile (ruling 16) answers EPERM. A docker exec
    # runs under the container's filter, so this proves which profile is loaded, not merely that one is.
    code, out = _python(sandbox, _DENIED_SYSCALLS)
    assert code == 0, out
    assert json.loads(out) == {"pidfd_open": 1, "ptrace": 1, "process_vm_readv": 1, "kcmp": 1}


def test_a_slot_uid_cannot_read_the_trader_config_but_can_read_the_code(sandbox):
    uid = SLOT_UIDS[0]
    code, out = _python(sandbox, "import os; os.listdir('/home/trader/.config')", user=f"{uid}:{SBXCHILD_GID}")
    assert code != 0 and "PermissionError" in out, out
    code, out = _python(sandbox, (
        "import pandas, trader.sandbox.child, trader.authored.api; "
        "open('/home/trader/mmr/trader/sandbox/child.py').read(); print(pandas.__file__)"),
        user=f"{uid}:{SBXCHILD_GID}")
    assert code == 0, out


def test_pong_reports_the_real_serving_posture(sandbox, boot):
    posture = boot.posture
    assert boot.image_digest == sandbox.labels.get("mmr.build_id")
    code, baked = _exec(sandbox, ["cat", "/etc/mmr-build-id"])
    assert code == 0 and baked.strip() == boot.image_digest
    assert posture["cap_eff"] == EXPECTED_CAP_EFF
    assert posture["seccomp"] == 2 and posture["no_new_privs"] == 1 and posture["uid"] == 0
    assert posture["pid"] == _supervisor(sandbox)["pid"]
    assert posture["root_read_only"] is True and posture["pids_max"] == EXPECTED_PIDS_MAX
    assert posture["home_mounts"] == ["/store"] and posture["forbidden_env"] == []
    assert "lo" in posture["interfaces"]
    # Ruling 33 on this kernel: SIOCGIFADDR and /proc/net/if_inet6 really answered (False), not unknown (None).
    for name in posture["interfaces"]:
        if name == "lo":
            continue
        assert name in FALLBACK_TUNNEL_DEVICES, name
        state = posture["interface_state"][name]
        assert state["operstate"] == "down" and state["ipv4"] is False and state["ipv6"] is False, (name, state)
    assert boot.live_open == 0


# ── live sessions (all closed before any probe: ruling 29) ──────────────────

def test_a_benign_live_session_returns_decisions_for_each_of_its_conids(boot):
    digest = _store(BENIGN)
    conids = list(range(101, 101 + proto.MAX_SESSION_CONIDS))     # the most a deployment binds
    with SandboxClient(proto.LIVE_SOCKET).open_session(digest, "Edge", {}, "1 min", conids) as session:
        assert session.image_digest == boot.image_digest               # ruling 29: OPENED carries the build
        signals = {conid: _bars(session, conid, 5) for conid in conids}
    assert all(any(s is not None for s in per_conid) for per_conid in signals.values())


def test_a_real_child_runs_as_its_slot_uid_without_caps_and_with_its_rlimits(sandbox):
    supervisor_pid = _supervisor(sandbox)["pid"]
    with SandboxClient(proto.LIVE_SOCKET).open_session(_store(BENIGN), "Edge", {}, "1 min", [101]) as session:
        _bars(session, 101, 1)
        children = _slot_processes(sandbox)
        assert len(children) == 1, children
        child = children[0]
        limits = _limits(sandbox, child["pid"])
    status = child["status"]
    uid = _real_uid(child)
    assert status["Uid"].split() == [str(uid)] * 4
    assert status["Gid"].split() == [str(SBXCHILD_GID)] * 4
    assert status["Groups"] == ""                                       # setgroups([]) (ruling 15)
    for field in ("CapEff", "CapPrm", "CapInh", "CapAmb"):
        assert status[field] == NO_CAPS, field
    assert status["Seccomp"] == "2" and status["NoNewPrivs"] == "1"
    assert status["PPid"] == str(supervisor_pid)
    assert status["Threads"] == "1"
    assert limits["Max processes"] == ("1", "1")                        # ruling 5
    assert limits["Max file size"] == (str(4 * 1024 * 1024),) * 2
    assert limits["Max core file size"] == ("0", "0")
    assert limits["Max cpu time"] == (str(4 * 3600),) * 2               # ruling 4
    assert "unlimited" not in limits["Max address space"]               # ruling 3


def test_a_probe_is_refused_on_the_live_socket():
    with pytest.raises(SandboxProtocolError, match="probes are research-only"):   # ruling 32
        SandboxClient(proto.LIVE_SOCKET).open_probe("probe", "benign_orb")


def test_no_probe_starts_while_a_live_session_is_open():
    with SandboxClient(proto.LIVE_SOCKET).open_session(_store(BENIGN), "Edge", {}, "1 min", [101]) as live:
        _bars(live, 101, 1)
        assert SandboxClient(proto.RESEARCH_SOCKET).ping().live_open == 1
        with pytest.raises(SandboxUnavailable, match="live sessions open"):        # ruling 29
            SandboxClient(proto.RESEARCH_SOCKET).open_probe("probe", "benign_orb")
        _bars(live, 101, 1, start=1)                                     # the live session is untouched
    assert SandboxClient(proto.RESEARCH_SOCKET).ping().live_open == 0


# ── probes and hostile escapes (research pool) ──────────────────────────────

def test_a_closed_session_leaves_a_clean_slot_and_frees_it():
    # Review r1 #2, under the real serving capabilities (CHOWN already dropped): each session writes a file in
    # its slot; closing must wipe it as the slot uid (ruling 28), so the next session on that slot starts clean.
    client = SandboxClient(proto.RESEARCH_SOCKET)
    size = client.ping().size
    for i in range(2 * size + 1):
        with client.open_probe("probe", "writes_own_slot") as session:
            decision = session.bar(proto.PROBE_CONID, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)
        assert decision is not None and decision.action == "BUY", f"session {i} found a file left by an earlier one"
    info = client.ping()
    assert info.free == info.size                                    # no slot was quarantined


def test_the_benign_probe_also_runs():
    with SandboxClient(proto.RESEARCH_SOCKET).open_probe("probe", "benign_orb") as session:
        _bars(session, proto.PROBE_CONID, 5)


def test_a_real_child_holds_no_inherited_descriptor():
    # The fork path closes every inherited fd (close_fds_except). A neighbour session is open, so the supervisor
    # holds another child's pipe and its listening sockets: none of them may reach this child.
    client = SandboxClient(proto.RESEARCH_SOCKET)
    with client.open_probe("probe", "benign_orb") as neighbour:
        _bars(neighbour, proto.PROBE_CONID, 1)
        with client.open_probe("probe", "holds_only_its_pipe") as session:
            decision = session.bar(proto.PROBE_CONID, T0, 1.0, 2.0, 0.5, 1.5, 100.0)
    assert decision is not None and decision.action == "BUY", f"extra descriptors x/100: {decision}"


def test_ptrace_is_denied_inside_a_real_child():
    with SandboxClient(proto.RESEARCH_SOCKET).open_probe("probe", "ptrace_denied") as session:
        decision = session.bar(proto.PROBE_CONID, T0, 1.0, 2.0, 0.5, 1.5, 100.0)
    assert decision is not None and decision.action == "BUY"


@pytest.mark.parametrize("escape", list(HOSTILE_ESCAPES))
def test_every_hostile_escape_is_contained(escape, sandbox, boot):
    # Plan 1's escape corpus, loaded with the static check skipped on purpose (ruling 11). Each must OPEN, fault
    # at BAR with the marker its behaviour predicts, and leave a neighbouring benign session and the same
    # supervisor (boot id, build) answering (review r1 #3). Includes the exact
    # pd.io.common.os.kill(pd.io.common.os.getppid(), 9) path (escape_pd_io_common_oskill).
    check = hostile_check(SandboxClient(proto.RESEARCH_SOCKET), escape, baseline=boot)
    assert check.passed, check.detail
    # escape_fork_bomb above all: no process of any slot uid outlives its session.
    assert _wait_until_no_slot_process(sandbox) == []


def test_killing_another_sessions_child_fails_and_the_neighbour_lives():
    # escape_kill_other_child only acts when another slot child exists, so hold one open (ruling 27). The
    # neighbour is a research probe: no probe may start beside a live session (ruling 29).
    client = SandboxClient(proto.RESEARCH_SOCKET)
    with client.open_probe("probe", "benign_orb") as neighbour:
        _bars(neighbour, proto.PROBE_CONID, 1)
        with pytest.raises(SandboxFault, match="PermissionError"):
            with client.open_probe("escape", "escape_kill_other_child") as bad:
                _bars(bad, proto.PROBE_CONID, 3)
        _bars(neighbour, proto.PROBE_CONID, 1, start=1)                 # still answers: not killed


def test_a_parallel_benign_session_survives_a_hostile_one():
    client = SandboxClient(proto.RESEARCH_SOCKET)
    with client.open_probe("probe", "benign_orb") as good:
        _bars(good, proto.PROBE_CONID, 3)
        with pytest.raises(SandboxFault):
            with client.open_probe("escape", "escape_loop_forever") as bad:
                _bars(bad, proto.PROBE_CONID, 3)
        _bars(good, proto.PROBE_CONID, 3, start=3)                      # answers after the hostile child was killed


def test_run_isolation_checks_passes_on_the_running_service():
    result = run_isolation_checks(SandboxClient(proto.RESEARCH_SOCKET))
    failed = [c for c in result.checks if not c.passed]
    assert result.passed, failed
    names = {c.name for c in result.checks}
    assert set(HOSTILE_ESCAPES) <= names and {"benign_session", "network_none", "cap_eff", "seccomp"} <= names


def test_the_supervisor_and_ping_survive_every_hostile_session(sandbox, boot):
    for path in (proto.LIVE_SOCKET, proto.RESEARCH_SOCKET):
        info = SandboxClient(path).ping()
        assert info.boot_id == boot.boot_id and info.image_digest == boot.image_digest
        assert info.free == info.size, f"{info.pool}: a slot is quarantined or still held"
    assert _supervisor(sandbox)["pid"] == boot.posture["pid"]


def test_no_sandbox_child_process_or_zombie_remains(sandbox):
    # After every benign and hostile session above, each forked child (and wipe helper) was killed and reaped.
    assert _wait_until_no_slot_process(sandbox) == []
    zombies = [p for p in _processes(sandbox) if p["status"].get("State", "").startswith("Z")]
    assert zombies == []
    # Under docker-init only the supervisor is left (a health-check run is a docker exec: its PPid is 0).
    descendants = [p["pid"] for p in _processes(sandbox) if p["status"].get("PPid") not in ("0", None)]
    assert descendants == [_supervisor(sandbox)["pid"]]


# ── last: a stop/start cycle (restarts the sandbox) ─────────────────────────

def test_a_stop_and_start_gives_a_fresh_tmpfs_a_new_boot_and_a_clean_close(sandbox, boot):
    uid = SLOT_UIDS[0]
    code, out = _exec(sandbox, ["touch", f"/tmp/s{uid}/left-before-restart"], user=f"{uid}:{SBXCHILD_GID}")
    assert code == 0, out
    held = SandboxClient(proto.RESEARCH_SOCKET).open_probe("probe", "benign_orb")
    _bars(held, proto.PROBE_CONID, 1)
    since = int(time.time()) - 1
    # SIGTERM to docker-init from inside, as tests/fullstack/test_process_supervision.py does: not a "manual
    # stop", so restart: unless-stopped brings the same container back with a fresh tmpfs.
    _python(sandbox, "import os, signal; os.kill(1, signal.SIGTERM)")   # the image has no procps
    after = _wait_for_new_boot(boot.boot_id)
    assert after.image_digest == boot.image_digest
    with pytest.raises(SandboxUnavailable):
        _bars(held, proto.PROBE_CONID, 1, start=1)                      # the old session ended with its service
    held.close()
    sandbox.reload()
    logs = sandbox.logs(since=since).decode("utf-8", errors="replace")
    assert "sandbox stopped" in logs, logs[-2000:]                      # Server.close ran with a real child open
    code, out = _exec(sandbox, ["stat", f"/tmp/s{uid}/left-before-restart"])
    assert code != 0, out                                               # the tmpfs is fresh
    for slot in SLOT_UIDS:
        code, out = _exec(sandbox, ["stat", "-c", "%u:%g %a", f"/tmp/s{slot}"])
        assert code == 0 and out.strip() == f"{slot}:{SBXCHILD_GID} 700", out
    assert _supervisor(sandbox)["status"]["CapEff"] == EXPECTED_CAP_EFF


def _wait_for_new_boot(old_boot_id: str):
    deadline = time.monotonic() + RESTART_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            info = SandboxClient(proto.RESEARCH_SOCKET).ping()
            if info.boot_id != old_boot_id:
                return info
        except SandboxUnavailable:
            pass
        time.sleep(1.0)
    pytest.fail(f"the sandbox did not come back with a new boot id within {RESTART_TIMEOUT_S}s")
