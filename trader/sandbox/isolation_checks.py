"""The hostile and posture checks that `mmr ai-author prove-isolation` (Plan 3) runs.

Each check runs against a live service through the client. Hostile source lives in Plan 1's
escape corpus (loaded as probes, ruling 11); this module only names and streams them.

Fail closed: a missing or unknown posture value, an unexpected error, or a fault the escape does not
explain is a failed check, never a pass. A false pass would let AI-written code deploy unsafely.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from trader.authored.sandbox_client import SandboxClient, SandboxFault, SandboxInfo
from trader.sandbox.posture import decode_cap_eff
from trader.sandbox.protocol import BUILD_ID_RE, PROBE_CONID, SANDBOX_ERROR

EXPECTED_CAP_EFF = "00000000000000e0"   # exactly KILL + SETGID + SETUID (ruling 2)
EXPECTED_PIDS_MAX = "64"                # compose pids_limit (spec 7.5)
BENIGN_PROBE = "benign_orb"
# Ruling 33: devices some kernels (Docker Desktop's LinuxKit) create in every network namespace. Allowed beside
# lo only while down and without an address.
FALLBACK_TUNNEL_DEVICES = frozenset({"tunl0", "sit0", "ip6tnl0", "ip6gre0", "gre0", "gretap0", "erspan0",
                                     "ip_vti0", "ip6_vti0"})
_IFF_UP = 0x1
_ALLOWED_MOUNT = "/store"               # the authored store (ruling 22); nothing under /home or /root

# Plan 1's escape files, one per spec section 10 hostile behaviour (see the cross-plan note, D9).
HOSTILE_ESCAPES: tuple[str, ...] = (
    "escape_read_config_keys", "escape_write_outside_tmpfs", "escape_fork_bomb",
    "escape_allocate_past_limit", "escape_loop_forever", "escape_pd_io_common_oskill",
    "escape_kill_other_child", "escape_sys_modules_lookup", "escape_open_socket",
)
# Review r1 #3: what the real child reports when each escape is stopped at BAR. A fault without its marker
# (a load error, an unrelated exception) is not containment: the hostile operation may never have run.
EXPECTED_FAULT_MARKERS: dict[str, tuple[str, ...]] = {
    "escape_read_config_keys": ("PermissionError", "FileNotFoundError"),
    "escape_write_outside_tmpfs": ("PermissionError", "Read-only file system"),
    "escape_fork_bomb": ("BlockingIOError", "Resource temporarily unavailable"),
    "escape_allocate_past_limit": ("MemoryError", "Unable to allocate"),
    # The server's wall timeout, or the ForkSpawner backstop when it fires first (both 2 s).
    "escape_loop_forever": ("bar timeout", "child reply timed out"),
    "escape_pd_io_common_oskill": ("PermissionError",),
    "escape_kill_other_child": ("PermissionError",),
    "escape_sys_modules_lookup": ("PermissionError",),
    "escape_open_socket": ("OSError", "Network is unreachable"),
}
LOAD_FAULT_PREFIXES = ("load failed", "constructor failed", "AUTHORED_STATIC_REJECTED", "import of",
                       "child load failed", "session not loaded")
# How a fault raised while the bar ran begins: the authored on_bar raised, or the wall time ran out.
# Anything else at BAR (a dead pipe, "child failed: <Type>" from a supervisor bug) is not the escape.
BAR_FAULT_PREFIXES = ("on_bar raised ", "bar timeout after ", "child reply timed out")

_DETAIL_CHARS = 200
_T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class IsolationResult:
    passed: bool
    image_digest: str
    checks: tuple[CheckResult, ...]


def run_isolation_checks(client: SandboxClient) -> IsolationResult:
    """Posture first; then, only while no live session is open, the benign and the hostile sessions."""
    info = client.ping()
    checks = [_build_id_check(info.image_digest), *_posture_checks(info.posture)]
    live_gate = _live_sessions_check(info.live_open)
    checks.append(live_gate)
    if live_gate.passed:   # rulings 21, 29: no probe ever runs beside a live strategy's child
        checks.append(_benign_check(client))
        checks.extend(hostile_check(client, name, baseline=info) for name in HOSTILE_ESCAPES)
    return IsolationResult(passed=all(check.passed for check in checks), image_digest=info.image_digest,
                           checks=tuple(checks))


# ── posture (rulings 2, 10, 27) ─────────────────────────────────────────────

def _build_id_check(image_digest: Any) -> CheckResult:
    well_formed = isinstance(image_digest, str) and BUILD_ID_RE.fullmatch(image_digest) is not None
    return CheckResult("build_id", well_formed, f"image_digest={_short(image_digest)}")


def _posture_checks(posture: Mapping[str, Any]) -> list[CheckResult]:
    cap_eff = posture.get("cap_eff")
    interfaces = posture.get("interfaces")
    forbidden_env = posture.get("forbidden_env")
    mounts = posture.get("home_mounts")
    caps = decode_cap_eff(cap_eff) if isinstance(cap_eff, str) else []
    return [
        CheckResult("cap_eff", cap_eff == EXPECTED_CAP_EFF, f"cap_eff={_short(cap_eff)} caps={caps}"),
        CheckResult("seccomp", _is_int(posture.get("seccomp"), 2), f"seccomp={_short(posture.get('seccomp'))}"),
        CheckResult("no_new_privs", _is_int(posture.get("no_new_privs"), 1),
                    f"no_new_privs={_short(posture.get('no_new_privs'))}"),
        CheckResult("network_none", _network_is_none(interfaces, posture.get("interface_state")),
                    f"interfaces={_short(interfaces)} state={_short(posture.get('interface_state'))}"),
        CheckResult("no_forbidden_env", forbidden_env == [], f"forbidden_env={_short(forbidden_env)}"),
        CheckResult("no_home_mounts", _only_the_store_is_mounted(mounts), f"home_mounts={_short(mounts)}"),
        CheckResult("read_only_root", posture.get("root_read_only") is True,
                    f"root_read_only={_short(posture.get('root_read_only'))}"),
        CheckResult("pids_limit", posture.get("pids_max") == EXPECTED_PIDS_MAX,
                    f"pids_max={_short(posture.get('pids_max'))}"),
    ]


def _is_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected   # True is not 1, 2.0 is not 2


def _network_is_none(interfaces: Any, states: Any) -> bool:
    """lo, plus only fallback tunnels that are down and hold no address (ruling 33). Unknown state fails."""
    if (not isinstance(interfaces, list) or not all(isinstance(name, str) for name in interfaces)
            or "lo" not in interfaces):
        return False
    others = [name for name in interfaces if name != "lo"]
    if not others:
        return True
    if not isinstance(states, Mapping):
        return False
    return all(name in FALLBACK_TUNNEL_DEVICES and _tunnel_is_down(states.get(name)) for name in others)


def _tunnel_is_down(state: Any) -> bool:
    if not isinstance(state, Mapping):
        return False
    flags = state.get("flags")
    return (state.get("operstate") == "down" and type(flags) is int and not flags & _IFF_UP
            and state.get("ipv4") is False and state.get("ipv6") is False)


def _only_the_store_is_mounted(mounts: Any) -> bool:
    return isinstance(mounts, list) and all(
        isinstance(target, str) and (target == _ALLOWED_MOUNT or target.startswith(_ALLOWED_MOUNT + "/"))
        for target in mounts)


def _live_sessions_check(live_open: Optional[int]) -> CheckResult:
    """``None`` means the sandbox did not say: that is not "no live session"."""
    return CheckResult("live_sessions_closed", _is_int(live_open, 0), f"live_open={live_open!r}")


# ── sessions ─────────────────────────────────────────────────────────────────

def _bars(session, count: int = 5, start: int = 0) -> None:
    for i in range(start, start + count):
        session.bar(PROBE_CONID, _T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)


def _benign_check(client: SandboxClient) -> CheckResult:
    try:
        with client.open_probe("probe", BENIGN_PROBE) as session:
            _bars(session, 5)
    except Exception as exc:  # noqa: BLE001 — any failure is a failed check
        return CheckResult("benign_session", False, f"benign probe failed: {_describe(exc)}")
    return CheckResult("benign_session", True, "ran five bars and closed")


def hostile_check(client: SandboxClient, name: str, *, baseline: Optional[SandboxInfo] = None) -> CheckResult:
    """Review r1 #3: contained only if the escape OPENED, faulted at BAR for the reason its behaviour predicts,
    and both a neighbouring benign session and PING still answer afterwards.

    With ``baseline`` (the PING before the run), the PING afterwards must also show the same supervisor
    (boot id and build) and no open live session."""
    if name not in EXPECTED_FAULT_MARKERS:
        return CheckResult(name, False, "not a known hostile escape")
    try:
        return _hostile_check(client, name, baseline)
    except Exception as exc:  # noqa: BLE001 — an unexpected error proves nothing
        return CheckResult(name, False, f"unexpected error, not proven: {_describe(exc)}")


def _hostile_check(client: SandboxClient, name: str, baseline: Optional[SandboxInfo]) -> CheckResult:
    try:
        neighbour = client.open_probe("probe", BENIGN_PROBE)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"no neighbour session, not proven: {_describe(exc)}")
    # The neighbour is open before the escape runs, so escape_kill_other_child has a child to aim at.
    with neighbour:
        try:
            _bars(neighbour, 2)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(name, False, f"the neighbour failed before the escape ran: {_describe(exc)}")
        verdict = _contained_at_bar(client, name)
        if not verdict.passed:
            return verdict
        try:
            _bars(neighbour, 2, start=2)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(name, False, f"the neighbour session died: {_describe(exc)}")
    try:
        after = client.ping()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"PING failed after the escape: {_describe(exc)}")
    changed = _supervisor_change(baseline, after)
    if changed:
        return CheckResult(name, False, changed)
    return verdict


def _supervisor_change(baseline: Optional[SandboxInfo], after: SandboxInfo) -> str:
    """Why the PING after an escape does not show the same, quiet supervisor ("" when it does)."""
    if not _is_int(after.live_open, 0):
        return f"PING after the escape reports live_open={after.live_open!r}: a live session was open, not proven"
    if baseline is None:
        return ""
    if after.boot_id != baseline.boot_id:
        return "PING after the escape shows a new boot id: the supervisor restarted, not contained"
    if after.image_digest != baseline.image_digest:
        return "PING after the escape shows another build: not proven"
    return ""


def _contained_at_bar(client: SandboxClient, name: str) -> CheckResult:
    try:
        session = client.open_probe("escape", name)
    except Exception as exc:  # noqa: BLE001 — a load fault never counts (review r1 #3)
        return CheckResult(name, False, f"never OPENED, not proven: {_describe(exc)}")
    with session:
        try:
            _bars(session, 5)
        except SandboxFault as fault:
            return _attributed(name, fault)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(name, False, f"no fault at BAR, not proven: {_describe(exc)}")
    return CheckResult(name, False, "ran to completion: NOT contained")


def _attributed(name: str, fault: SandboxFault) -> CheckResult:
    detail = fault.detail if isinstance(fault.detail, str) else ""
    shown = detail[:_DETAIL_CHARS]
    if fault.code != SANDBOX_ERROR:
        return CheckResult(name, False, f"{fault.code} at BAR is not a contained escape: {shown}")
    if detail.startswith(LOAD_FAULT_PREFIXES):
        return CheckResult(name, False, f"a load fault is not containment: {shown}")
    if not detail.startswith(BAR_FAULT_PREFIXES):
        return CheckResult(name, False, f"the BAR fault did not come from the escape: {shown}")
    if not any(marker in detail for marker in EXPECTED_FAULT_MARKERS[name]):
        return CheckResult(name, False, f"the BAR fault does not show the escape was stopped: {shown}")
    return CheckResult(name, True, f"contained at BAR: {shown}")


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_DETAIL_CHARS]


def _short(value: Any) -> str:
    return repr(value)[:_DETAIL_CHARS]
