"""Read what the running process can do: capabilities, seccomp, privileges, network, env, mounts.

Returned by the supervisor in PONG (rulings 10, 27) so the isolation checks can judge the live
posture. A value that cannot be read is reported as unknown (``None``, ``""`` or ``UNREADABLE``),
never as a clean value: the checks fail on unknown.
"""
from __future__ import annotations

import os

UNREADABLE = "<unreadable>"   # a list entry no isolation check accepts

_CAP_NAMES = {5: "CAP_KILL", 6: "CAP_SETGID", 7: "CAP_SETUID"}
# Spec 10 names ALPACA_, MASSIVE_, TWELVEDATA_, IB_ and OPENROUTER_; the rest are other secrets the stack holds.
_FORBIDDEN_PREFIXES = ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_",
                       "AWS_", "AZURE_", "DASHBOARD_", "TELEGRAM", "ANTHROPIC_", "OPENAI_", "XAI_")
_FORBIDDEN_NAMES = frozenset({"MMR_WEB_TOKEN", "MMR_SERVICE_HMAC_KEY_FILE", "MMR_RPC_KEYS_DIR"})
# Mount targets the posture reports: secrets live under /home and /root, authored files under /store.
_REPORTED_MOUNT_ROOTS = ("/home", "/store", "/root")


def _proc_status() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if ":" in line:
                    key, value = line.split(":", 1)
                    out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def _int_or_none(text: str | None) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def decode_cap_eff(hex_value: str) -> list[str]:
    """Every capability bit set in a CapEff hex value; bits without a known name show as ``bit<N>``."""
    try:
        bits = int(hex_value, 16)
    except (TypeError, ValueError):
        return []
    if bits < 0:
        return []
    set_bits = [bit for bit in range(bits.bit_length()) if bits & (1 << bit)]
    return (sorted(_CAP_NAMES[bit] for bit in set_bits if bit in _CAP_NAMES)
            + [f"bit{bit}" for bit in set_bits if bit not in _CAP_NAMES])


def forbidden_env_names() -> list[str]:
    return sorted(name for name in os.environ
                  if name.upper().startswith(_FORBIDDEN_PREFIXES) or name.upper() in _FORBIDDEN_NAMES)


def interfaces(net_dir: str = "/sys/class/net") -> list[str]:
    try:
        return sorted(os.listdir(net_dir))
    except OSError:
        return [UNREADABLE]


def home_mounts(mounts_path: str = "/proc/mounts") -> list[str]:
    try:
        with open(mounts_path) as mounts:
            targets = [line.split(" ")[1] for line in mounts if len(line.split(" ")) > 1]
    except OSError:
        return [UNREADABLE]
    return sorted(target for target in targets if target.startswith(_REPORTED_MOUNT_ROOTS))


def root_read_only(mounts_path: str = "/proc/mounts") -> bool:
    """True when the root filesystem is mounted read-only (compose ``read_only: true``).

    Every mount on ``/`` must be ``ro``: a missing line, an unreadable file or any ``rw`` layer is False."""
    try:
        with open(mounts_path) as mounts:
            root_options = [fields[3].split(",") for fields in (line.split(" ") for line in mounts)
                            if len(fields) > 3 and fields[1] == "/"]
    except OSError:
        return False
    return bool(root_options) and all("ro" in options for options in root_options)


def pids_max(paths: tuple[str, ...] = ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max")) -> str:
    """The container's pids limit (compose ``pids_limit``), cgroup v2 first; "" when unreadable."""
    for path in paths:
        try:
            with open(path) as limit:
                return limit.read().strip()
        except OSError:
            continue
    return ""


def read_posture() -> dict:
    status = _proc_status()
    return {
        "cap_eff": status.get("CapEff", ""),
        "seccomp": _int_or_none(status.get("Seccomp")),
        "no_new_privs": _int_or_none(status.get("NoNewPrivs")),
        "uid": os.getuid(),
        "pid": os.getpid(),
        "interfaces": interfaces(),
        "forbidden_env": forbidden_env_names(),
        "home_mounts": home_mounts(),
        "root_read_only": root_read_only(),
        "pids_max": pids_max(),
    }
