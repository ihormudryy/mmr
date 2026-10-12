import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "config_defaults" / "docker-default-seccomp.json"
SANDBOX = ROOT / "config_defaults" / "sandbox-seccomp.json"
DENIED = {"ptrace", "process_vm_readv", "process_vm_writev", "kcmp", "pidfd_open"}
UPSTREAM_SHA = "9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76"


def test_the_vendored_default_is_the_pinned_upstream():
    assert hashlib.sha256(DEFAULT.read_bytes()).hexdigest() == UPSTREAM_SHA


def test_build_is_reproducible():
    before = SANDBOX.read_text()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_sandbox_seccomp.py")], check=True)
    assert SANDBOX.read_text() == before   # committed file equals a fresh build


def test_sandbox_profile_keeps_the_default_action_and_errno():
    default = json.loads(DEFAULT.read_text())
    profile = json.loads(SANDBOX.read_text())
    assert profile["defaultAction"] == default["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile.get("defaultErrnoRet") == default.get("defaultErrnoRet")
    assert profile["archMap"] == default["archMap"]


def test_the_five_syscalls_appear_in_no_allow_rule():
    profile = json.loads(SANDBOX.read_text())
    for rule in profile["syscalls"]:
        if rule["action"] != profile["defaultAction"]:
            assert DENIED.isdisjoint(rule["names"]), rule["names"]


def test_a_common_syscall_is_still_allowed():
    profile = json.loads(SANDBOX.read_text())
    allowed = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW"
               for name in rule["names"]}
    assert {"read", "write", "mmap", "futex"} <= allowed
