"""Read what the running process can do: capabilities, seccomp, privileges, network, env, mounts.

Returned by the supervisor in PONG (rulings 10, 27) so the isolation checks can judge the live
posture. A value that cannot be read is reported as unknown (``None``, ``""`` or ``UNREADABLE``),
never as a clean value: the checks fail on unknown.
"""
from __future__ import annotations

import errno
import fcntl
import os
import socket
import stat
import struct

UNREADABLE = "<unreadable>"   # a list entry no isolation check accepts

_CAP_NAMES = {5: "CAP_KILL", 6: "CAP_SETGID", 7: "CAP_SETUID"}
# Spec 10 names ALPACA_, MASSIVE_, TWELVEDATA_, IB_ and OPENROUTER_; the rest are other secrets the stack holds.
FORBIDDEN_ENV_PREFIXES = ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_",
                          "AWS_", "AZURE_", "DASHBOARD_", "TELEGRAM", "ANTHROPIC_", "OPENAI_", "XAI_")
FORBIDDEN_ENV_NAMES = frozenset({"MMR_WEB_TOKEN", "MMR_SERVICE_HMAC_KEY_FILE", "MMR_RPC_KEYS_DIR"})
# Mount targets the posture reports: secrets live under /home and /root, authored files under /store.
_REPORTED_MOUNT_ROOTS = ("/home", "/store", "/root")
_SIOCGIFADDR = 0x8915   # Linux ioctl: a device's primary IPv4 address
_IFNAMSIZ = 16          # a device name is at most 15 bytes plus NUL


def _proc_status(path: str = "/proc/self/status") -> dict[str, str]:
    """The ``Key: value`` lines of a status file; {} when it cannot be read or decoded."""
    try:
        with open(path, encoding="utf-8") as status:
            lines = status.readlines()
    except (OSError, ValueError):   # ValueError: bytes that are not UTF-8
        return {}
    return {key.strip(): value.strip() for key, value in (line.split(":", 1) for line in lines if ":" in line)}


def _int_or_none(text: str | None, base: int = 10) -> int | None:
    try:
        return int(text, base)
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
                  if name.upper().startswith(FORBIDDEN_ENV_PREFIXES) or name.upper() in FORBIDDEN_ENV_NAMES)


def interfaces(net_dir: str = "/sys/class/net") -> list[str]:
    """Every network device. A regular file there (bonding_masters, when the bonding module is loaded) is a sysfs
    control file, not a device; any other entry, also one that cannot be inspected, counts as a device."""
    try:
        names = os.listdir(net_dir)
    except (OSError, ValueError):
        return [UNREADABLE]
    return sorted(name for name in names if not _is_regular_file(os.path.join(net_dir, name)))


def _is_regular_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False   # unknown: kept as a device, so the network check judges it


def has_ipv4_address(name: str) -> bool | None:
    """Whether ``name`` holds an IPv4 address, up or down (SIOCGIFADDR); None when it cannot be asked.

    A device with no primary IPv4 address has none at all: a secondary needs a primary."""
    try:
        raw_name = name.encode()
    except UnicodeError:
        return None
    if not 0 < len(raw_name) < _IFNAMSIZ or b"\0" in raw_name:   # never truncate into another device's name
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, struct.pack("256s", raw_name))
    except OSError as exc:
        return False if exc.errno == errno.EADDRNOTAVAIL else None
    return True


def ipv6_devices(if_inet6_path: str = "/proc/net/if_inet6") -> set[str] | None:
    """Every device that holds an IPv6 address; None when the table cannot be read."""
    try:
        with open(if_inet6_path, encoding="utf-8") as table:
            return {fields[-1] for fields in (line.split() for line in table) if len(fields) == 6}
    except (OSError, ValueError):
        return None


def interface_states(names: list[str], *, net_dir: str = "/sys/class/net",
                     if_inet6_path: str = "/proc/net/if_inet6",
                     ipv4_reader=has_ipv4_address) -> dict[str, dict]:
    """Per device (ruling 33): operstate, the flags word (IFF_UP is bit 0) and whether it holds an IPv4 or
    IPv6 address. A value that cannot be read is None."""
    with_ipv6 = ipv6_devices(if_inet6_path)
    return {name: {"operstate": _read_text(os.path.join(net_dir, name, "operstate")),
                   "flags": _int_or_none(_read_text(os.path.join(net_dir, name, "flags")), base=16),
                   "ipv4": ipv4_reader(name),
                   "ipv6": None if with_ipv6 is None else name in with_ipv6}
            for name in names if name != UNREADABLE}


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read().strip()
    except (OSError, ValueError):
        return None


def home_mounts(mounts_path: str = "/proc/mounts") -> list[str]:
    try:
        with open(mounts_path, encoding="utf-8") as mounts:
            targets = [line.split(" ")[1] for line in mounts if len(line.split(" ")) > 1]
    except (OSError, ValueError):
        return [UNREADABLE]
    return sorted(target for target in targets if target.startswith(_REPORTED_MOUNT_ROOTS))


def root_read_only(mounts_path: str = "/proc/mounts") -> bool:
    """True when the root filesystem is mounted read-only (compose ``read_only: true``).

    Every mount on ``/`` must be ``ro``: a missing line, an unreadable file or any ``rw`` layer is False."""
    try:
        with open(mounts_path, encoding="utf-8") as mounts:
            root_options = [fields[3].split(",") for fields in (line.split(" ") for line in mounts)
                            if len(fields) > 3 and fields[1] == "/"]
    except (OSError, ValueError):
        return False
    return bool(root_options) and all("ro" in options for options in root_options)


def pids_max(paths: tuple[str, ...] = ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max")) -> str:
    """The container's pids limit (compose ``pids_limit``), cgroup v2 first; "" when unreadable."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as limit:
                return limit.read().strip()
        except (OSError, ValueError):
            continue
    return ""


def read_posture() -> dict:
    status = _proc_status()
    names = interfaces()
    return {
        "cap_eff": status.get("CapEff", ""),
        "seccomp": _int_or_none(status.get("Seccomp")),
        "no_new_privs": _int_or_none(status.get("NoNewPrivs")),
        "uid": os.getuid(),
        "pid": os.getpid(),
        "interfaces": names,
        "interface_state": interface_states(names),
        "forbidden_env": forbidden_env_names(),
        "home_mounts": home_mounts(),
        "root_read_only": root_read_only(),
        "pids_max": pids_max(),
    }
