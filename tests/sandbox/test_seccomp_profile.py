import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "config_defaults" / "docker-default-seccomp.json"
SANDBOX = ROOT / "config_defaults" / "sandbox-seccomp.json"
DENIED = {"ptrace", "process_vm_readv", "process_vm_writev", "kcmp", "pidfd_open"}
UPSTREAM_SHA = "9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76"


def _load_build_script():
    spec = importlib.util.spec_from_file_location(
        "build_sandbox_seccomp", ROOT / "scripts" / "build_sandbox_seccomp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_vendored_default_is_the_pinned_upstream():
    assert hashlib.sha256(DEFAULT.read_bytes()).hexdigest() == UPSTREAM_SHA


def test_build_is_reproducible():
    # Compare in memory: a test must never write the committed file (xdist race).
    assert _load_build_script().render().encode() == SANDBOX.read_bytes()


def test_derived_profile_is_the_default_minus_exactly_the_five_names():
    default = json.loads(DEFAULT.read_text())
    profile = json.loads(SANDBOX.read_text())
    expected_rules = []
    for rule in default["syscalls"]:
        names = [name for name in rule["names"] if name not in DENIED]
        if names:
            expected_rules.append({**rule, "names": names})
    assert profile["syscalls"] == expected_rules
    assert {key: value for key, value in profile.items() if key != "syscalls"} == \
        {key: value for key, value in default.items() if key != "syscalls"}
    assert list(profile) == list(default)


def test_sandbox_profile_keeps_the_default_action_and_errno():
    default = json.loads(DEFAULT.read_text())
    profile = json.loads(SANDBOX.read_text())
    assert profile["defaultAction"] == default["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile.get("defaultErrnoRet") == default.get("defaultErrnoRet")
    assert profile["archMap"] == default["archMap"]


def test_the_five_syscalls_appear_in_no_rule_at_all():
    text = SANDBOX.read_text()
    for rule in json.loads(text)["syscalls"]:
        assert DENIED.isdisjoint(rule["names"]), rule["names"]
    for name in DENIED:
        assert f'"{name}"' not in text


def test_a_common_syscall_is_still_allowed():
    profile = json.loads(SANDBOX.read_text())
    allowed = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW"
               for name in rule["names"]}
    assert {"read", "write", "mmap", "futex"} <= allowed


def test_build_fails_loudly_on_a_changed_vendored_file(monkeypatch):
    script = _load_build_script()
    monkeypatch.setattr(script, "UPSTREAM_SHA", "0" * 64)
    with pytest.raises(SystemExit):
        script.build()


def test_build_fails_loudly_when_a_denied_name_is_not_in_the_default(monkeypatch):
    script = _load_build_script()
    monkeypatch.setattr(script, "DENIED", script.DENIED | {"no_such_syscall"})
    with pytest.raises(SystemExit):
        script.build()
