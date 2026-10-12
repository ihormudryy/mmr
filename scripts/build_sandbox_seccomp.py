#!/usr/bin/env python3
"""Derive config_defaults/sandbox-seccomp.json from the vendored Docker default.

Source: moby v27.5.1 profiles/seccomp/default.json
  https://raw.githubusercontent.com/moby/moby/v27.5.1/profiles/seccomp/default.json
  sha256 9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76

The only change: remove ptrace, process_vm_readv, process_vm_writev, kcmp and pidfd_open
from every allow rule's name list, and drop a rule left with no names. The default action
stays SCMP_ACT_ERRNO (errno 1), so a removed syscall fails with EPERM. No new rule is added
that matches the default action (runc skips it; crun may refuse it) -- ruling 16.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "config_defaults" / "docker-default-seccomp.json"
OUT = ROOT / "config_defaults" / "sandbox-seccomp.json"
DENIED = {"ptrace", "process_vm_readv", "process_vm_writev", "kcmp", "pidfd_open"}


def build() -> dict:
    profile = json.loads(DEFAULT.read_text())
    kept = []
    for rule in profile["syscalls"]:
        names = [name for name in rule["names"] if name not in DENIED]
        if not names:
            continue
        kept.append({**rule, "names": names})
    profile["syscalls"] = kept
    return profile


def main() -> None:
    OUT.write_text(json.dumps(build(), indent=2, sort_keys=False) + "\n")


if __name__ == "__main__":
    main()
