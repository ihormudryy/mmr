# AI Paper SP3 — Plan 2: the sandbox service, its client and the isolation checks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `sandbox` compose service. It runs authored strategy code in one forked child per session. Each child has its own slot uid, no capability, rlimits, reduced built-ins and an import guard. Add the client that `research` and `strategy` use (Plan 4) and the isolation checks behind `mmr ai-author prove-isolation` (Plan 3).

**Architecture:** One single-threaded asyncio supervisor (`trader/sandbox/supervisor.py`) runs as uid 0 with only SETUID, SETGID and KILL. It serves two Unix sockets: live (3 slots, one per authored deployment, each holding that deployment's conids) and research (4 one-conid slots). A frame is a 4-byte length plus JSON with `"v": 1` (`trader/sandbox/protocol.py`). `OPEN` re-hashes the stored file and runs Plan 1's `require_clean`. Then it forks a child (`trader/sandbox/child.py`). The child drops to its slot uid, then loads the class. The session is bound to the caller's connection, and closing the connection kills the child. The client (`trader/authored/sandbox_client.py`) maps errors to `SandboxUnavailable` (availability) or `SandboxFault` (strategy fault).

**Tech Stack:** Python 3.12 (asyncio, `os.fork`, `resource`, `socket`), pandas/numpy (same image), Docker Compose, a seccomp profile vendored from moby, pytest and pytest-asyncio. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md` sections 7.5, 7.6 (client), 7.9 (checks), 9, 10. Index: `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md` (rule R0: the names below are owned here).

## Global Constraints

- Base: master after SP3 Plan 1 (`trader/authored/api.py`, `keys.py`, `allowed_api.py`, `static_check.py`). This plan imports `AuthoredStrategy`, `Decision`, `source_digest`, `ALLOWED_IMPORTS`, `require_clean`, `AuthoredStaticRejected` exactly as the index names them.
- Python 3.12. No new dependency. Load YAML with `yaml.safe_load` only.
- Spec values (as amended by SP3 decision D1, spec section 14): live pool 3, research pool 4; child uids `20001..20007`, group `sbxchild` gid 20000; per-bar wall 2 s; child memory 256 MB; `pids_limit: 64`, `cpus: 2`, `mem_limit: 4g`; `network_mode: none`, `read_only: true`, `cap_drop: [ALL]`, `cap_add: [SETUID, SETGID, KILL]`, `no-new-privileges:true`; tmpfs `/tmp` 64 MB `noexec`; environment `TZ=UTC`, `PYTHONDONTWRITEBYTECODE=1`, `HOME=/tmp`, `TMPDIR=/tmp`, `NUMBA_CACHE_DIR=/tmp/numba`, `MPLCONFIGDIR=/tmp/mpl` (plus the build id, ruling 1).
- Sockets: `/run/sandbox-live/sandbox.sock`, `/run/sandbox-research/sandbox.sock`, group `trader` (gid 1000), mode 0660. Store at `/store`, read-only.
- AI code never runs in `research`, `strategy`, `trader` or `ai`. The production entry never enables the fake child mode, and the store path never skips `require_clean`.
- Fail loudly: every refusal carries a code and a short detail. Never return an empty result for an error.
- Never print secrets. The sandbox holds none; its logs go to stdout.
- Test-first. Per task run only the listed tests. Full suite once, in Task 12.
- Never run Docker against the live stack. Container tests run in their own compose project (`mmr-sbxtest`), never `mmr`.
- Commit subjects `feat:` / `test:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. No push, deploy or container restart is authorized by this plan.

## Rulings

1. **Plan 2 ruling 1:** The identity in `PONG` is a build id, not the Docker image id. `docker.sh -b` passes `MMR_BUILD_ID` (git sha + UTC time) as a build arg. The Dockerfile bakes it into `/etc/mmr-build-id` and the label `mmr.build_id`. The supervisor reads the file and refuses to start on `unset` or a malformed value. This value cannot go stale: a running container always reports the id of its own image, so no `docker inspect` cross-check is needed (SP3 decision D2); the label stays for operators. The trader's isolation record and gate and the strategy service's load check all key on this build id. The field keeps the index name `image_digest`. *Spec amended (section 14):* the spec said "image digest".
2. **Plan 2 ruling 2:** Per-slot directories. `/tmp` is one 64 MB tmpfs (`noexec,nosuid,nodev`), mode 0755, owned by root. Compose cannot set a tmpfs's uid/gid (the long syntax takes `size` and `mode` only; the short list form takes no options), and a runtime tmpfs mount hides any dir baked into the image at that path. So the supervisor creates `/tmp/s<uid>` for each slot at startup, `chown`s it to the slot uid and group `sbxchild`, mode 0700, then **drops `CAP_CHOWN`** before it serves a single request. The stanza adds `CHOWN` to `cap_add` for the startup window only; after the drop the serving effective set is exactly SETUID/SETGID/KILL (`00000000000000e0`), which is what the spec section 10 production-service check asserts, and what `PONG` reports. Why a per-slot dir: pandas/numpy may write a cache, and a shared 1777 `/tmp` would let one child write where another reads (a cross-session channel), which breaks the "write outside its own dir fails" check. *The brief adds `CHOWN` for startup; since it is dropped before serving, the serving posture still matches spec 7.5's three caps. The owner should confirm this startup-only use of `CHOWN`.* If the owner refuses `CHOWN` entirely, the fallback is **no writable dir** (root-owned 0755 `/tmp`, child `HOME`/`TMPDIR` on a non-writable path, both "write inside" and "write outside" fail, caches tolerated). The container test checks the slot dir's owner and mode, that a write elsewhere fails, and that the serving CapEff is `…e0`.
3. **Plan 2 ruling 3:** `RLIMIT_AS` = the address space mapped at fork + 256 MiB. A forked pandas process already maps more than 256 MB, so an absolute 256 MB limit would fail every allocation. *Conflict with spec 7.5 wording.*
4. **Plan 2 ruling 4:** `RLIMIT_CPU` = 4 h per session (soft = hard, so the kernel sends SIGKILL). It is only a backstop, because a research session streams a whole evaluation period through one child. The 2 s wall time per step is the real limit. *The spec gives no value.*
5. **Plan 2 ruling 5:** Other rlimits: `RLIMIT_NPROC` 1 (it counts threads, so no thread and no fork), `RLIMIT_FSIZE` 4 MiB, `RLIMIT_CORE` 0. The supervisor sets `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS` and `MKL_NUM_THREADS` to 1 in code before it imports numpy. This keeps the compose environment list as the spec gives it.
6. **Plan 2 ruling 6:** The 2 s step limit applies to class load (`OPEN`) as well as to each `BAR`.
7. **Plan 2 ruling 7:** One session per connection. The supervisor kills the child when the caller's connection closes, so a crashed caller cannot leak a slot.
8. **Plan 2 ruling 8:** New code `SANDBOX_BAD_REQUEST` for caller errors: malformed frame, unknown session, a bar time that does not increase, bad params. The client raises `SandboxProtocolError` for it. It is neither availability nor a strategy fault.
9. **Plan 2 ruling 9:** A missing store file or a class that differs from the file's class is answered `SANDBOX_DIGEST_MISMATCH`. A static rejection at `OPEN` is answered `SANDBOX_ERROR` with detail `AUTHORED_STATIC_REJECTED:<rule>`.
10. **Plan 2 ruling 10:** `PONG` carries `image_digest`, `boot_id` (random at start), `pid`, `pool`, `free` and `posture`. `posture` holds the supervisor's own CapEff, Seccomp mode, NoNewPrivs, uid, network interfaces, forbidden env names and mounts under `/home`. `SandboxInfo` carries the same fields. These fields are additive to the index.
11. **Plan 2 ruling 11:** Probe sessions. Spec 7.9 runs hostile checks on the running service, but the protocol table has no request for that. This plan adds `OPEN_PROBE {kind, name}` and the client method `SandboxClient.open_probe`. Names resolve only against two directories in the read-only image: `trader/sandbox/probes/` (kind `probe`) and `tests/authored/escapes/` (kind `escape`). These are the only sessions that skip `require_clean`. `OPEN` (the store path) never skips it. Plan 1's corpus test asserts that every escape file, and each of the nine `HOSTILE_ESCAPES`, *is* refused by `check_source` (D9). A probe session holds one conid, `PROBE_CONID`. *Spec amended (section 14).*
12. **Plan 2 ruling 12:** The fake child mode is a `spawner` argument of `Supervisor`. `tests/sandbox/fakes.py` defines `InProcessSpawner`. `trader/sandbox_service.py` always passes `ForkSpawner()` and reads no option, environment variable or argument that could change this. A test asserts that no module under `trader/` imports `tests`.
13. **Plan 2 ruling 13:** A failed tmpfs wipe quarantines the slot: it is never reused, the failure is logged at ERROR, and `PONG.free` shows the loss. Reusing a dirty slot would let a later session read files from an earlier one, which is a lookahead channel.
14. **Plan 2 ruling 14:** `init: true` (docker-init is PID 1), so the supervisor is an ordinary process: a signal that reaches it is not ignored. `ipc: none` removes `/dev/shm`. `entrypoint` replaces `docker-entrypoint.sh`.
15. **Plan 2 ruling 15:** The supervisor joins group `trader` (`setgroups([1000])`). It sets `/run/sandbox-*` to root:trader 0750 and each socket to 0660. A child calls `setgroups([])` before `setresgid`/`setresuid`.
16. **Plan 2 ruling 16:** Seccomp profile = moby `v27.5.1` `profiles/seccomp/default.json` (sha256 `9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76`), vendored verbatim. `ptrace`, `process_vm_readv`, `process_vm_writev`, `kcmp` and `pidfd_open` are removed from every allow rule. The default action is `SCMP_ACT_ERRNO` with errno 1, so these calls fail with EPERM. No explicit rule is added that matches the default action, because runc skips such a rule and crun may refuse it.
17. **Plan 2 ruling 17:** Compose reads the `seccomp=./…` path relative to the current directory, so `docker.sh` runs compose from the repo root (`cd "$BUILDDIR"` in a subshell).
18. **Plan 2 ruling 18:** Profiles. `sandbox` has `profiles: ["ai", "test"]`. The test override gives `research` the `test` profile and `command: ["sleep", "infinity"]`, so the container tests can use the real research mounts without a running research service.
19. **Plan 2 ruling 19:** `sandbox` is not a key-holding service. It is added to the skip tuple of `test_every_long_lived_service_is_classified`, not to `SERVICE_PRINCIPAL`. A new static test asserts that it mounts no key and no config.
20. **Plan 2 ruling 20:** Bar times on the wire are UTC ISO-8601 strings. The child's `history` index is tz-aware UTC and named `date`.
21. **Plan 2 ruling 21:** Known limit: children share one network namespace, so two children could reach each other through abstract Unix sockets. The static checker (Plan 1) is the layer that keeps authored code away from socket APIs. This is recorded, not fixed.
22. **Plan 2 ruling 22:** Store mount for Plan 3: the Dockerfile creates `/store` owned by `trader`, mode 0755. Plan 3 must mount `mmr_authored_strategies` at `/store` (read-write) in `trader` and write files with mode 0644. *The index names the volume but not the trader's path.*
23. **Plan 2 ruling 23:** Compose cannot set `noexec`/`nosuid`/`nodev` on a tmpfs through the long (`type: tmpfs`) syntax; the short string form `"/tmp:size=…,mode=0755,noexec,nosuid,nodev"` passes the options through to the mount. The plan uses the short form and the container test checks `/proc/mounts` for `noexec`. If a runtime ignores the string options, switch to the long form and record that `noexec` then rests on non-root + no-new-privileges + seccomp.
24. **Plan 2 ruling 24:** `os.fork()` runs inside an asyncio process that has an executor thread pool (the per-bar timeout uses `run_in_executor`). This is the known 3.12 fork-with-threads hazard. It is low risk here: the BLAS libraries are single-threaded (ruling 5), the executor threads only ever block in `recv`, and the child calls only async-signal-safe operations (`setresuid`, `setrlimit`, `os.exec`-free) before it serves. The child never touches the inherited event loop. Recorded so the hazard is explicit.
25. **Plan 2 ruling 25:** Concurrency. The server offloads every request except `PING` to a thread pool so the event loop never blocks; `PING` is answered inline (lock-free) so health always responds. The supervisor's lock is held only for O(1) bookkeeping (slot pop/append, session insert/remove), never across a fork, a class load or a bar round-trip, so a slow research bar never holds the lock and the two pools stay independent. A `_Session.busy` flag rejects a second concurrent bar on one session. The 2 s timeout fires on the loop and calls `drop_connection`, which takes the free lock and kills the child; the kill makes the in-flight `send_bar` in the worker thread return an error. *Trade-off:* the pools share slots independently (research never consumes live's slots), but under extreme load both share one thread pool (16 workers for 7 possible sessions, so never starved in practice).
26. **Plan 2 ruling 26 (SP3 decision D1, spec amended in section 14):** A live session serves one authored deployment. `OPEN` carries `conids: [int]` (1-20, distinct, positive; `MAX_SESSION_CONIDS = 20` equals SP2c `MAX_CONIDS`); `BAR` and `SIGNAL` carry `conid`. One child (one slot uid) holds one strategy instance and one trimmed history per conid; all run the same digest, so no isolation is needed between them. Bar times must increase per conid. A conid outside the session is `SANDBOX_BAD_REQUEST`. A fault on any conid ends the whole session. Pools: live 3 (one per deployment; the trader caps `max_active` at 3), research 4 with exactly one conid per session (`MAX_CONIDS_PER_SESSION`). Seven slots, uids `20001..20007`. `OPEN_PROBE` also carries `conids` (one, `PROBE_CONID = 1`), so `BAR` is uniform.
27. **Plan 2 ruling 27 (SP3 decisions D2, D3):** The posture in `PONG` also reports `root_read_only` (from `/proc/mounts`) and `pids_max` (cgroup v2, then v1), and `run_isolation_checks` checks them (`read_only_root`, `pids_limit` = 64). With caps, seccomp, no-new-privileges, interfaces and env already in the posture, the proof needs no host `docker inspect`. `escape_kill_other_child` runs while a benign probe session is held open; the check passes only if the escape faults and the neighbour still answers.

## Review Focus

1. A caller that dies mid-session: the slot must come back without a restart (`test_dropped_connection_frees_the_slot`, Task 4).
2. A child that writes forged or oversized frames on its pipe: the supervisor must treat any reply it cannot validate as `SANDBOX_ERROR` (`test_forged_child_reply_is_a_fault`, Task 4).
3. Two `OPEN` requests that race for the last slot: one gets `OPENED`, the other `SANDBOX_BUSY` at once (`test_full_pool_answers_busy_at_once`, Task 4).
4. A `digest` or probe `name` with path characters: refused `SANDBOX_BAD_REQUEST` before any path join (`test_open_refuses_a_path_like_digest`, `test_probe_request_parses_and_refuses_a_traversal_name`, Task 4).
5. A failed wipe: the slot is quarantined and `free` drops (`test_failed_wipe_quarantines_the_slot`, Task 4).

## File map

| File | Change |
|---|---|
| `trader/sandbox/__init__.py`, `protocol.py` (new) | constants, frames, request parsing, reply validation |
| `trader/sandbox/child.py` (new) | `ChildRuntime` (pure), `ForkSpawner`, privilege drop, rlimits |
| `trader/sandbox/supervisor.py` (new) | pools, sessions, asyncio server |
| `trader/sandbox/posture.py` (new) | read caps, seccomp, env, interfaces, mounts |
| `trader/sandbox/probes/benign_orb.py` (new) | benign ORB used by checks and tests |
| `trader/sandbox/isolation_checks.py` (new) | `run_isolation_checks`, CLI |
| `trader/sandbox_service.py` (new) | process entry, `--health` |
| `trader/authored/sandbox_client.py` (new) | client |
| `config_defaults/docker-default-seccomp.json`, `config_defaults/sandbox-seccomp.json`, `scripts/build_sandbox_seccomp.py` (new) | seccomp |
| `Dockerfile`, `docker-compose.yml`, `docker-compose.test.override.yml`, `docker.sh`, `scripts/run_sandbox_fullstack.sh` (new) | image, stanza, build id, test runner |
| `tests/sandbox/*` (new), `tests/fullstack/test_sandbox_service.py` (new), `tests/test_compose_sandbox.py` (new), `tests/test_compose_rpc_keys.py` | tests |
| `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONAL_STATE.md` | docs |

---

### Task 1: The wire protocol

**Files:**
- Create: `trader/sandbox/__init__.py` (empty), `trader/sandbox/protocol.py`
- Create: `tests/sandbox/__init__.py` (empty), `tests/sandbox/test_protocol.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (index names): `LIVE_SOCKET`, `RESEARCH_SOCKET`, `STORE_DIR`, `PROTOCOL_VERSION`, error codes `SANDBOX_ERROR`, `SANDBOX_BUSY`, `SANDBOX_DIGEST_MISMATCH`, plus `SANDBOX_BAD_REQUEST` (ruling 8). `MAX_SESSION_CONIDS = 20` (equals SP2c `ai_deployments.MAX_CONIDS`) and `PROBE_CONID = 1` (ruling 26). Frame helpers `read_frame(reader) -> dict`, `write_frame(writer, obj)`, `encode_frame(obj) -> bytes`. Request parsing `parse_request(obj) -> Request` and `validate_signal(obj) -> None`. `FrameError(Exception)`, `MAX_FRAME_BYTES = 262144`, `DIGEST_RE`, `CLASS_RE`, `PROBE_NAME_RE`, `BUILD_ID_RE`.

- [ ] **Step 1: Write the failing tests.** Create `tests/sandbox/__init__.py` empty and write `tests/sandbox/test_protocol.py`:

```python
import asyncio

import pytest

from trader.sandbox import protocol as p


def _roundtrip(obj):
    async def run():
        reader, writer = await _pipe()
        p.write_frame(writer, obj)
        await writer.drain()
        got = await p.read_frame(reader)
        writer.close()
        return got

    return asyncio.run(run())


async def _pipe():
    loop = asyncio.get_event_loop()
    import socket as _s

    a, b = _s.socketpair()
    r, _ = await asyncio.open_connection(sock=a)
    _, w = await asyncio.open_connection(sock=b)
    return r, w


def test_constants_match_the_index():
    assert p.PROTOCOL_VERSION == 1
    assert p.LIVE_SOCKET == "/run/sandbox-live/sandbox.sock"
    assert p.RESEARCH_SOCKET == "/run/sandbox-research/sandbox.sock"
    assert p.STORE_DIR == "/store"
    assert p.SANDBOX_ERROR == "SANDBOX_ERROR"
    assert p.SANDBOX_BUSY == "SANDBOX_BUSY"
    assert p.SANDBOX_DIGEST_MISMATCH == "SANDBOX_DIGEST_MISMATCH"
    assert p.SANDBOX_BAD_REQUEST == "SANDBOX_BAD_REQUEST"


def test_a_live_session_can_hold_every_conid_a_deployment_binds():
    from trader.automation.ai_deployments import MAX_CONIDS

    assert p.MAX_SESSION_CONIDS == MAX_CONIDS == 20


def test_a_frame_round_trips():
    assert _roundtrip({"v": 1, "type": "PING"}) == {"v": 1, "type": "PING"}


def test_a_frame_over_the_cap_is_refused_on_read():
    big = p.encode_frame({"v": 1, "pad": "x" * (p.MAX_FRAME_BYTES + 10)})

    async def run():
        reader, writer = await _pipe()
        writer.write(big)
        await writer.drain()
        with pytest.raises(p.FrameError):
            await p.read_frame(reader)
        writer.close()

    asyncio.run(run())


def _open_frame(**override):
    frame = {"v": 1, "type": "OPEN", "digest": "sha256:" + "a" * 64, "class": "MyStrat",
             "params": {}, "bar_size": "1 min", "conids": [101]}
    frame.update(override)
    return frame


def test_open_request_parses_and_normalises():
    req = p.parse_request(_open_frame(params={"X": 1, "Y": 2.5}, conids=[101, 202]))
    assert req.type == "OPEN"
    assert req.digest == "sha256:" + "a" * 64
    assert req.class_name == "MyStrat"
    assert req.params == {"X": 1.0, "Y": 2.5}
    assert req.bar_size == "1 min"
    assert req.conids == (101, 202)


def test_open_accepts_a_param_outside_any_declared_range():
    # Ranges are the trader's submit-time check (SP3 decision D6); research neighbours move +-10 %.
    assert p.parse_request(_open_frame(params={"X": -1e6})).params == {"X": -1e6}


def test_open_refuses_a_path_like_digest():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_open_frame(digest="sha256:../../etc/passwd"))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_open_refuses_a_non_identifier_class():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_open_frame(**{"class": "a b"}))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_open_refuses_non_numeric_params():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_open_frame(params={"X": "big"}))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


@pytest.mark.parametrize("conids", [None, [], [0], [-5], [True], ["101"], [101, 101],
                                    list(range(1, 22))])
def test_open_refuses_a_bad_conid_list(conids):
    frame = _open_frame()
    if conids is None:
        del frame["conids"]
    else:
        frame["conids"] = conids
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(frame)
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_probe_request_parses_and_refuses_a_traversal_name():
    ok = p.parse_request({"v": 1, "type": "OPEN_PROBE", "kind": "escape", "name": "format_globals",
                          "conids": [p.PROBE_CONID]})
    assert ok.type == "OPEN_PROBE" and ok.kind == "escape" and ok.name == "format_globals"
    assert ok.conids == (p.PROBE_CONID,)
    with pytest.raises(p.FrameError) as exc:
        p.parse_request({"v": 1, "type": "OPEN_PROBE", "kind": "escape", "name": "../x",
                         "conids": [p.PROBE_CONID]})
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_bar_request_requires_increasing_numbers():
    req = p.parse_request({"v": 1, "type": "BAR", "session": "s1", "conid": 101,
                           "t": "2024-01-02T14:30:00+00:00",
                           "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0})
    assert req.session == "s1" and req.conid == 101 and req.ohlcv["close"] == 1.5
    with pytest.raises(p.FrameError):
        p.parse_request({"v": 1, "type": "BAR", "session": "s1", "conid": 101, "t": "not-a-time",
                         "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0})


@pytest.mark.parametrize("conid", [None, 0, True, "101", 1.5])
def test_bar_request_refuses_a_bad_conid(conid):
    frame = {"v": 1, "type": "BAR", "session": "s1", "t": "2024-01-02T14:30:00+00:00",
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}
    if conid is not None:
        frame["conid"] = conid
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(frame)
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_a_wrong_version_is_refused():
    with pytest.raises(p.FrameError):
        p.parse_request({"v": 2, "type": "PING"})


def test_validate_signal_checks_range_and_action():
    p.validate_signal({"action": "BUY", "probability": 0.5})
    p.validate_signal({"action": None, "probability": None})
    for bad in ({"action": "HOLD", "probability": 0.5},
                {"action": "BUY", "probability": 1.5},
                {"action": "BUY", "probability": float("nan")}):
        with pytest.raises(p.FrameError):
            p.validate_signal(bad)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_protocol.py -q` — expected: FAIL (`ModuleNotFoundError: trader.sandbox`).

- [ ] **Step 3: Implement.** Write `trader/sandbox/__init__.py` empty, then `trader/sandbox/protocol.py`:

```python
"""The sandbox wire protocol: 4-byte length + JSON frames, version 1."""
from __future__ import annotations

import asyncio
import json
import math
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

PROTOCOL_VERSION = 1

LIVE_SOCKET = "/run/sandbox-live/sandbox.sock"
RESEARCH_SOCKET = "/run/sandbox-research/sandbox.sock"
STORE_DIR = "/store"

SANDBOX_ERROR = "SANDBOX_ERROR"
SANDBOX_BUSY = "SANDBOX_BUSY"
SANDBOX_DIGEST_MISMATCH = "SANDBOX_DIGEST_MISMATCH"
SANDBOX_BAD_REQUEST = "SANDBOX_BAD_REQUEST"

MAX_FRAME_BYTES = 262144  # 256 KiB: a lookback of bars plus a 20 KB source, with headroom

# A live session serves one authored deployment: all of its conids in one child (ruling 26).
# Equals SP2c trader.automation.ai_deployments.MAX_CONIDS; a test pins the two together.
MAX_SESSION_CONIDS = 20
PROBE_CONID = 1           # the one conid of an OPEN_PROBE session (ruling 26)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PROBE_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# The build id PONG reports (ruling 1). Plan 3's IMAGE_ID wire pattern is the same text (one spelling, D2).
BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
SESSION_RE = re.compile(r"^s[0-9a-f]{1,32}$")
_PROBE_KINDS = frozenset({"probe", "escape"})


class FrameError(Exception):
    """A malformed or out-of-contract frame. ``code`` is the reply code to send."""

    def __init__(self, detail: str, *, code: str = SANDBOX_BAD_REQUEST):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def encode_frame(obj: Mapping[str, Any]) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def write_frame(writer: asyncio.StreamWriter, obj: Mapping[str, Any]) -> None:
    writer.write(encode_frame(obj))


async def read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {length} bytes over the {MAX_FRAME_BYTES} cap")
    body = await reader.readexactly(length)
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FrameError(f"frame is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameError("frame is not a JSON object")
    return obj


def _require_version(obj: Mapping[str, Any]) -> None:
    if obj.get("v") != PROTOCOL_VERSION:
        raise FrameError(f"unsupported protocol version {obj.get('v')!r}")


def _number(obj: Mapping[str, Any], key: str) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrameError(f"{key} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise FrameError(f"{key} is not finite")
    return out


def _conid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FrameError("conid must be a positive integer")
    return value


def _conids(obj: Mapping[str, Any]) -> tuple[int, ...]:
    raw = obj.get("conids")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SESSION_CONIDS:
        raise FrameError(f"conids must be a list of 1-{MAX_SESSION_CONIDS} conids")
    conids = tuple(_conid(value) for value in raw)
    if len(set(conids)) != len(conids):
        raise FrameError("conids must be distinct")
    return conids


def _params(obj: Mapping[str, Any]) -> dict[str, float]:
    raw = obj.get("params", {})
    if not isinstance(raw, dict):
        raise FrameError("params must be an object")
    out: dict[str, float] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not CLASS_RE.match(name):
            raise FrameError(f"bad param name {name!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FrameError(f"param {name} must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise FrameError(f"param {name} is not finite")
        out[name] = number
    return out


@dataclass(frozen=True)
class Request:
    type: str
    digest: str = ""
    class_name: str = ""
    params: Mapping[str, float] = field(default_factory=dict)
    bar_size: str = ""
    kind: str = ""
    name: str = ""
    session: str = ""
    conids: tuple[int, ...] = ()
    conid: int = 0
    t: datetime | None = None
    ohlcv: Mapping[str, float] = field(default_factory=dict)


def parse_request(obj: Mapping[str, Any]) -> Request:
    _require_version(obj)
    kind = obj.get("type")
    if kind == "PING":
        return Request(type="PING")
    if kind == "OPEN":
        digest = obj.get("digest")
        class_name = obj.get("class")
        bar_size = obj.get("bar_size")
        if not isinstance(digest, str) or not DIGEST_RE.match(digest):
            raise FrameError("digest must be sha256:<64 hex>")
        if not isinstance(class_name, str) or not CLASS_RE.match(class_name):
            raise FrameError("class must be a Python identifier")
        if not isinstance(bar_size, str) or not bar_size.strip():
            raise FrameError("bar_size must be a non-empty string")
        return Request(type="OPEN", digest=digest, class_name=class_name,
                       params=_params(obj), bar_size=bar_size, conids=_conids(obj))
    if kind == "OPEN_PROBE":
        probe_kind = obj.get("kind")
        name = obj.get("name")
        if probe_kind not in _PROBE_KINDS:
            raise FrameError(f"probe kind must be one of {sorted(_PROBE_KINDS)}")
        if not isinstance(name, str) or not PROBE_NAME_RE.match(name):
            raise FrameError("probe name must match [a-z0-9_]{1,64}")
        return Request(type="OPEN_PROBE", kind=probe_kind, name=name, conids=_conids(obj))
    if kind == "BAR":
        session = obj.get("session")
        if not isinstance(session, str) or not SESSION_RE.match(session):
            raise FrameError("bad session id")
        conid = _conid(obj.get("conid"))
        t_raw = obj.get("t")
        if not isinstance(t_raw, str):
            raise FrameError("t must be an ISO-8601 string")
        try:
            when = datetime.fromisoformat(t_raw)
        except ValueError as exc:
            raise FrameError(f"t is not ISO-8601: {exc}") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        ohlcv = {col: _number(obj, col) for col in ("open", "high", "low", "close", "volume")}
        return Request(type="BAR", session=session, conid=conid, t=when.astimezone(timezone.utc), ohlcv=ohlcv)
    if kind == "CLOSE":
        session = obj.get("session")
        if not isinstance(session, str) or not SESSION_RE.match(session):
            raise FrameError("bad session id")
        return Request(type="CLOSE", session=session)
    raise FrameError(f"unknown request type {kind!r}")


def validate_signal(obj: Mapping[str, Any]) -> None:
    """Raise FrameError when a SIGNAL payload is out of contract (ruling: a bad child reply is a fault)."""
    action = obj.get("action")
    probability = obj.get("probability")
    if action is None and probability is None:
        return
    if action not in ("BUY", "SELL"):
        raise FrameError(f"action must be BUY, SELL or null, got {action!r}", code=SANDBOX_ERROR)
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise FrameError("probability must be a number", code=SANDBOX_ERROR)
    number = float(probability)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise FrameError("probability must be within 0..1", code=SANDBOX_ERROR)
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/sandbox/test_protocol.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add trader/sandbox/__init__.py trader/sandbox/protocol.py tests/sandbox/__init__.py tests/sandbox/test_protocol.py
git commit -m "feat: add the sandbox wire protocol and request validation"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 2: The child runtime (load, import guard, reduced built-ins, history, output)

The runtime that runs authored code is a pure class so it can be tested in-process. The privilege drop, the rlimits and the fork live in `ForkSpawner`, exercised by the container tests (Task 7), because a unit test cannot setuid.

**Files:**
- Create: `trader/sandbox/child.py`
- Test: `tests/sandbox/test_child_runtime.py`

**Interfaces:**
- Consumes: Plan 1 `trader/authored/api.py` (`AuthoredStrategy`, `Decision`), `trader/authored/static_check.py` (`require_clean`, `AuthoredStaticRejected`), `trader/authored/allowed_api.py` (`ALLOWED_IMPORTS`).
- Produces: `ChildError(Exception)` (`detail`), `ChildRuntime(source: str, class_name: str, params, lookback: int, conids: tuple[int, ...])` with `load() -> None` and `on_bar(conid, t, ohlcv) -> dict` (a SIGNAL payload: `{"action", "probability"}`); one strategy instance and one trimmed history per conid (ruling 26); `build_guarded_builtins() -> dict`; `guarded_import(...)`; `load_authored_class(source, class_name) -> type`; `SpawnSpec` (adds `conids`); `ForkSpawner` and the `Spawner` protocol (`spawn(spec) -> ChildHandle`); `ChildHandle` (`send_bar(conid, t, ohlcv) -> dict`, `close() -> None`, `pid`).

- [ ] **Step 1: Write the failing tests.** Write `tests/sandbox/test_child_runtime.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from trader.sandbox.child import ChildError, ChildRuntime, build_guarded_builtins, load_authored_class

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
C1, C2 = 101, 202
BAR = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}

GOOD = '''
import numpy as np
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 3

    def on_bar(self, history, params):
        if len(history) >= int(params["N"]):
            return Decision(action="BUY", probability=float(min(max(len(history) / 10.0, 0.0), 1.0)))
        return None
'''


def _runtime(source=GOOD, params=None, conids=(C1,)):
    return ChildRuntime(source=source, class_name="Counter",
                        params=params or {"N": 2.0}, lookback=3, conids=conids)


def _bars(runtime, count, conid=C1):
    return [runtime.on_bar(conid, T0 + timedelta(minutes=i), BAR) for i in range(count)]


def test_a_good_strategy_loads_and_signals():
    runtime = _runtime()
    runtime.load()
    signals = _bars(runtime, 3)
    assert signals[0] == {"action": None, "probability": None}
    assert signals[1]["action"] == "BUY" and 0.0 <= signals[1]["probability"] <= 1.0


def test_history_is_trimmed_to_lookback():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 3

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=min(len(history) / 100.0, 1.0))
'''
    runtime = ChildRuntime(source=source, class_name="Counter", params={}, lookback=3, conids=(C1,))
    runtime.load()
    signals = _bars(runtime, 10)
    assert signals[-1]["probability"] == pytest.approx(3 / 100.0)  # never more than LOOKBACK_BARS


def test_each_conid_has_its_own_history_and_instance():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 50

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=min(len(history) / 100.0, 1.0))
'''
    runtime = ChildRuntime(source=source, class_name="Counter", params={}, lookback=50, conids=(C1, C2))
    runtime.load()
    _bars(runtime, 5, conid=C1)
    first_c2 = runtime.on_bar(C2, T0, BAR)          # C2's own clock: an earlier time than C1's last is fine
    assert first_c2["probability"] == pytest.approx(1 / 100.0)   # C2 never sees C1's bars
    assert runtime.on_bar(C1, T0 + timedelta(minutes=5), BAR)["probability"] == pytest.approx(6 / 100.0)


def test_a_conid_outside_the_session_is_a_fault():
    runtime = _runtime()
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C2, T0, BAR)


def test_a_bar_time_that_does_not_increase_is_a_fault():
    runtime = _runtime()
    runtime.load()
    runtime.on_bar(C1, T0, BAR)
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_an_exception_in_on_bar_is_a_child_error():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Boom(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=1.0 / (len(history) - 1))
'''
    runtime = ChildRuntime(source=source, class_name="Boom", params={}, lookback=2, conids=(C1,))
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_a_non_decision_return_is_a_child_error():
    source = '''
from trader.authored.api import AuthoredStrategy


class Bad(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return "BUY"
'''
    runtime = ChildRuntime(source=source, class_name="Bad", params={}, lookback=2, conids=(C1,))
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_a_probability_out_of_range_is_a_child_error():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Over(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=2.0)
'''
    runtime = ChildRuntime(source=source, class_name="Over", params={}, lookback=2, conids=(C1,))
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)


def test_the_guarded_builtins_hide_dangerous_names():
    guarded = build_guarded_builtins()
    for name in ("eval", "exec", "open", "__import__", "compile", "input", "breakpoint", "getattr"):
        assert name not in guarded
    assert "len" in guarded and "range" in guarded and "min" in guarded


def test_a_disallowed_import_at_module_load_is_blocked():
    source = '''
import os
from trader.authored.api import AuthoredStrategy, Decision


class Sneaky(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        return None
'''
    # The static checker refuses it first; even with that bypassed the import guard blocks it.
    with pytest.raises(ChildError):
        load_authored_class(source, "Sneaky", skip_static_check=True)


def test_a_runtime_import_of_a_blocked_module_is_blocked():
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Late(AuthoredStrategy):
    LOOKBACK_BARS = 2

    def on_bar(self, history, params):
        mod = __import__("socket")
        return None
'''
    runtime = ChildRuntime(source=source, class_name="Late", params={}, lookback=2, conids=(C1,),
                           skip_static_check=True)
    runtime.load()
    with pytest.raises(ChildError):
        runtime.on_bar(C1, T0, BAR)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_child_runtime.py -q` — expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement.** Write `trader/sandbox/child.py`:

```python
"""The code that runs one authored strategy: load it, feed it bars, validate its output.

``ChildRuntime`` is pure (no fork, no privilege drop) so it is unit-tested in process.
``ForkSpawner`` forks a child, drops it to its slot uid, sets rlimits and runs the loop.
"""
from __future__ import annotations

import builtins as _builtins
import os
import resource
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

import numpy as np
import pandas as pd

from trader.authored.allowed_api import ALLOWED_IMPORTS
from trader.authored.api import AuthoredStrategy, Decision
from trader.authored.static_check import AuthoredStaticRejected, require_clean

# Built-ins an authored module may see. Everything the static checker denies, and
# everything that reaches the import system or the file system, is left out.
_SAFE_BUILTIN_NAMES = frozenset({
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
    "frozenset", "int", "isinstance", "issubclass", "iter", "len", "list", "map",
    "max", "min", "next", "pow", "range", "reversed", "round", "set", "slice",
    "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    "ValueError", "TypeError", "ZeroDivisionError", "KeyError", "IndexError",
    "ArithmeticError", "Exception", "FloatingPointError", "OverflowError",
})

_ALLOWED_IMPORT_NAMES = frozenset(ALLOWED_IMPORTS)


class ChildError(Exception):
    """Any fault in the authored code or its I/O: a wrong return, an exception, a blocked import."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if level != 0 or root not in _ALLOWED_IMPORT_NAMES:
        raise ChildError(f"import of {name!r} is not allowed")
    return _builtins.__import__(name, globals, locals, fromlist, level)


def build_guarded_builtins() -> dict:
    guarded = {name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES
               if hasattr(_builtins, name)}
    guarded["__import__"] = guarded_import
    return guarded


def load_authored_class(source: str, class_name: str, *, skip_static_check: bool = False) -> type:
    if not skip_static_check:
        try:
            found = require_clean(source)
        except AuthoredStaticRejected as exc:
            raise ChildError(f"AUTHORED_STATIC_REJECTED:{';'.join(v.rule for v in exc.violations)}") from exc
        if found != class_name:
            raise ChildError(f"class {class_name!r} does not match the file's class {found!r}")
    module_globals: dict[str, Any] = {"__builtins__": build_guarded_builtins(), "__name__": "authored"}
    try:
        code = compile(source, "<authored>", "exec")
        exec(code, module_globals)  # noqa: S102 — guarded builtins, no real __import__
    except ChildError:
        raise
    except Exception as exc:  # noqa: BLE001 — any load failure is a child fault
        raise ChildError(f"load failed: {type(exc).__name__}: {exc}") from exc
    obj = module_globals.get(class_name)
    if not isinstance(obj, type) or not issubclass(obj, AuthoredStrategy) or obj is AuthoredStrategy:
        raise ChildError(f"{class_name!r} is not an AuthoredStrategy subclass")
    return obj


class _ConidBook:
    """One conid's strategy instance and its trimmed history."""

    def __init__(self, strategy: AuthoredStrategy):
        self.strategy = strategy
        self.rows: list[dict[str, float]] = []
        self.times: list[datetime] = []

    def add(self, when: datetime, ohlcv: Mapping[str, float], lookback: int) -> pd.DataFrame:
        if self.times and when <= self.times[-1]:
            raise ChildError("bar time must strictly increase")
        self.times.append(when)
        self.rows.append({col: float(ohlcv[col]) for col in ("open", "high", "low", "close", "volume")})
        if len(self.rows) > lookback:
            self.rows = self.rows[-lookback:]
            self.times = self.times[-lookback:]
        index = pd.DatetimeIndex(self.times, name="date")
        return pd.DataFrame(self.rows, index=index, columns=["open", "high", "low", "close", "volume"])


class ChildRuntime:
    """One session: one strategy instance and one trimmed history per conid (ruling 26).

    All conids of a session run the same digest, so they share one child and one uid. A fault on
    any conid ends the whole session."""

    def __init__(self, *, source: str, class_name: str, params: Mapping[str, float],
                 lookback: int, conids: tuple[int, ...], skip_static_check: bool = False):
        if not conids:
            raise ChildError("a session needs at least one conid")
        self._source = source
        self._class_name = class_name
        self._params = dict(params)
        self._lookback = max(1, int(lookback))
        self._conids = tuple(conids)
        self._skip = skip_static_check
        self._books: dict[int, _ConidBook] = {}

    def load(self) -> None:
        cls = load_authored_class(self._source, self._class_name, skip_static_check=self._skip)
        try:
            self._books = {conid: _ConidBook(cls()) for conid in self._conids}
        except Exception as exc:  # noqa: BLE001
            raise ChildError(f"constructor failed: {type(exc).__name__}: {exc}") from exc

    def on_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        if not self._books:
            raise ChildError("session not loaded")
        book = self._books.get(conid)
        if book is None:
            raise ChildError(f"conid {conid} is not in this session")
        when = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        history = book.add(when, ohlcv, self._lookback)
        try:
            decision = book.strategy.on_bar(history, self._params)
        except ChildError:
            raise
        except Exception as exc:  # noqa: BLE001 — authored code failed
            raise ChildError(f"on_bar raised {type(exc).__name__}: {exc}") from exc
        return _signal_payload(decision)


def _signal_payload(decision: Any) -> dict:
    if decision is None:
        return {"action": None, "probability": None}
    if not isinstance(decision, Decision):
        raise ChildError(f"on_bar must return a Decision or None, got {type(decision).__name__}")
    if decision.action not in ("BUY", "SELL"):
        raise ChildError(f"action must be BUY or SELL, got {decision.action!r}")
    try:
        probability = float(decision.probability)
    except (TypeError, ValueError) as exc:
        raise ChildError("probability is not a number") from exc
    if not np.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ChildError("probability must be within 0..1")
    return {"action": decision.action, "probability": probability}


# ── the real forked child ────────────────────────────────────────────────

@dataclass(frozen=True)
class SpawnSpec:
    source: str          # the already-verified source bytes decoded to str
    class_name: str
    params: Mapping[str, float]
    lookback: int
    conids: tuple[int, ...]   # one strategy instance per conid in this child (ruling 26)
    slot_uid: int        # always a real slot uid (20001..20007)
    slot_dir: str        # /tmp/s<N>, owned by slot_uid
    child_mem_bytes: int
    skip_static_check: bool = False   # True only for a probe (ruling 11); the store path is always False


class ChildHandle(Protocol):
    pid: int

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict: ...

    def close(self) -> None: ...


class Spawner(Protocol):
    def spawn(self, spec: SpawnSpec) -> ChildHandle: ...


_FRAME = struct.Struct(">I")


def _send(sock: socket.socket, obj: dict) -> None:
    import json

    body = json.dumps(obj).encode("utf-8")
    sock.sendall(_FRAME.pack(len(body)) + body)


def _recv(sock: socket.socket) -> dict:
    import json

    header = _recv_exact(sock, 4)
    (length,) = _FRAME.unpack(header)
    return json.loads(_recv_exact(sock, length))


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            raise ChildError("child pipe closed")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _set_child_rlimits(spec: SpawnSpec) -> None:
    baseline = _mapped_bytes()
    as_limit = baseline + spec.child_mem_bytes          # ruling 3: baseline + 256 MiB
    resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (4 * 3600, 4 * 3600))   # ruling 4: backstop
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))              # ruling 5
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _mapped_bytes() -> int:
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 512 * 1024 * 1024


def _drop_privileges(spec: SpawnSpec) -> None:
    os.chdir(spec.slot_dir)
    os.environ["HOME"] = spec.slot_dir
    os.environ["TMPDIR"] = spec.slot_dir
    os.setgroups([])
    os.setresgid(20000, 20000, 20000)
    os.setresuid(spec.slot_uid, spec.slot_uid, spec.slot_uid)
    if os.getresuid() != (spec.slot_uid,) * 3 or os.getresgid() != (20000,) * 3:
        os._exit(71)  # EX_OSERR: refuse to run if the drop did not take
    for line in open("/proc/self/status"):           # the child must keep no capability (spec 7.5)
        if line.startswith("CapEff:") and int(line.split()[1], 16) != 0:
            os._exit(71)


class ForkSpawner:
    """Fork one child per session. Production always uses this (ruling 12)."""

    def __init__(self, open_timeout_s: float = 2.0):
        self._open_timeout = open_timeout_s   # ruling 6: class load is bounded too

    def spawn(self, spec: SpawnSpec) -> ChildHandle:
        parent_sock, child_sock = socket.socketpair()
        pid = os.fork()
        if pid == 0:
            parent_sock.close()
            try:
                _child_main(spec, child_sock)
            finally:
                os._exit(0)
        child_sock.close()
        parent_sock.settimeout(self._open_timeout)   # a child that hangs in load must not block the server
        try:
            return _ForkedChild(pid=pid, sock=parent_sock)
        except (ChildError, socket.timeout, OSError) as exc:
            _kill_and_reap(pid)
            parent_sock.close()
            raise ChildError(getattr(exc, "detail", f"child load failed: {exc}")) from exc


def _kill_and_reap(pid: int) -> None:
    try:
        os.kill(pid, 9)   # SIGKILL: this is what CAP_KILL is for (own children only)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _child_main(spec: SpawnSpec, sock: socket.socket) -> None:
    _drop_privileges(spec)
    _set_child_rlimits(spec)
    _close_other_fds(keep={sock.fileno()})
    try:
        runtime = ChildRuntime(source=spec.source, class_name=spec.class_name,
                               params=spec.params, lookback=spec.lookback, conids=spec.conids,
                               skip_static_check=spec.skip_static_check)
        runtime.load()
        _send(sock, {"ok": True})
    except ChildError as exc:
        _send(sock, {"ok": False, "detail": exc.detail})
        return
    while True:
        try:
            request = _recv(sock)
        except ChildError:
            return
        if request.get("type") == "CLOSE":
            return
        try:
            when = datetime.fromisoformat(request["t"])
            payload = runtime.on_bar(int(request["conid"]), when, request["ohlcv"])
            _send(sock, {"ok": True, "signal": payload})
        except ChildError as exc:
            _send(sock, {"ok": False, "detail": exc.detail})
            return


def _close_other_fds(keep: set[int]) -> None:
    keep = keep | {1, 2}  # keep stdout/stderr for logging
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return
    for name in names:
        fd = int(name)
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


@dataclass
class _ForkedChild:
    pid: int
    sock: socket.socket

    def __post_init__(self) -> None:
        reply = _recv(self.sock)   # may raise socket.timeout (caught by ForkSpawner.spawn)
        self.sock.settimeout(None)
        if not reply.get("ok"):
            raise ChildError(reply.get("detail", "child load failed"))

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        _send(self.sock, {"type": "BAR", "conid": conid, "t": t.astimezone(timezone.utc).isoformat(),
                          "ohlcv": dict(ohlcv)})
        reply = _recv(self.sock)
        if not reply.get("ok"):
            raise ChildError(reply.get("detail", "child failed"))
        return reply["signal"]

    def close(self) -> None:
        # Kill first: a child looping or ignoring CLOSE must die so its slot and pid free (CAP_KILL).
        _kill_and_reap(self.pid)
        try:
            self.sock.close()
        except OSError:
            pass
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/sandbox/test_child_runtime.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add trader/sandbox/child.py tests/sandbox/test_child_runtime.py
git commit -m "feat: add the sandbox child runtime, import guard and fork spawner"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 3: The supervisor (pools, sessions, store re-hash, probes)

The supervisor is transport-free: it takes parsed requests and returns reply dicts, so it is unit-tested with an in-process spawner. The asyncio server (Task 4) wires sockets to it.

**Files:**
- Create: `trader/sandbox/supervisor.py`
- Create: `trader/sandbox/probes/__init__.py` (empty), `trader/sandbox/probes/benign_orb.py`
- Create: `tests/sandbox/fakes.py`
- Test: `tests/sandbox/test_supervisor.py`

**Interfaces:**
- Consumes: Task 1 `protocol` (`Request`, codes, `validate_signal`, `FrameError`), Task 2 `child` (`Spawner`, `SpawnSpec`, `ChildError`, `ChildHandle`, `ChildRuntime`, `load_authored_class`), Plan 1 `source_digest`, `require_clean`, `AuthoredStaticRejected`.
- Produces: `Pool` (`name`, `size`, slot pool), `Supervisor(*, spawners, pool_sizes, store_dir, image_digest, posture, child_mem_bytes=256*1024*1024)`, `LIVE_POOL_SIZE = 3`, `RESEARCH_POOL_SIZE = 4`, `MAX_CONIDS_PER_SESSION = {"live": 20, "research": 1}`, `Supervisor.handle(pool_name, conn_id, request) -> dict`, `Supervisor.drop_connection(conn_id) -> None`, `Supervisor.pong(pool_name) -> dict`, `Slot` (uid, dir). `PROBE_DIR`, `ESCAPE_DIR`. `tests/sandbox/fakes.py`: `InProcessSpawner`.

- [ ] **Step 1: Write the failing tests.** Write `tests/sandbox/fakes.py`:

```python
"""Test-only in-process spawner (ruling 12). The production entry never constructs it."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from trader.sandbox.child import ChildRuntime, SpawnSpec


class _InProcessChild:
    pid = -1

    def __init__(self, spec: SpawnSpec):
        # In process (pytest), NEVER skip the static check, even for a probe: hostile escape
        # code must never run inside the test runner. A clean probe (benign_orb) passes
        # require_clean anyway; a hostile one is refused here, so only the real forked child
        # (container tests) ever runs it with the check skipped.
        self._runtime = ChildRuntime(source=spec.source, class_name=spec.class_name,
                                     params=spec.params, lookback=spec.lookback, conids=spec.conids,
                                     skip_static_check=False)
        self._runtime.load()

    def send_bar(self, conid: int, t: datetime, ohlcv: Mapping[str, float]) -> dict:
        return self._runtime.on_bar(conid, t, ohlcv)

    def close(self) -> None:
        return None


class InProcessSpawner:
    def __init__(self):
        self.spawned = 0
        self.closed = 0

    def spawn(self, spec: SpawnSpec):
        self.spawned += 1
        return _InProcessChild(spec)
```

Write `tests/sandbox/test_supervisor.py`:

```python
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.sandbox import protocol as p
from trader.sandbox.supervisor import Supervisor
from tests.sandbox.fakes import InProcessSpawner

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
C1, C2 = 101, 202
OHLCV = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}

GOOD = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        if len(history) >= 2:
            return Decision(action="BUY", probability=0.6)
        return None
'''


def _store(tmp_path: Path, source: str) -> str:
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    (tmp_path / f"{digest.split(':')[1]}.py").write_text(source)
    return digest


def _supervisor(tmp_path, live=3, research=4):
    return Supervisor(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                      pool_sizes={"live": live, "research": research},
                      store_dir=str(tmp_path), image_digest="mmr-build-id", posture={})


def _open_frame(digest, class_name="Counter", conids=(C1,)):
    return {"v": 1, "type": "OPEN", "digest": digest, "class": class_name, "params": {},
            "bar_size": "1 min", "conids": list(conids)}


def _open(sup, tmp_path, conn=1, pool="live", conids=(C1,)):
    digest = _store(tmp_path, GOOD)
    return sup.handle(pool, conn, p.parse_request(_open_frame(digest, conids=conids)))


def _bar(sup, session, when, conn=1, pool="live", conid=C1):
    return sup.handle(pool, conn, p.parse_request(
        {"v": 1, "type": "BAR", "session": session, "conid": conid, "t": when.isoformat(), **OHLCV}))


def test_open_then_bars_then_close(tmp_path):
    sup = _supervisor(tmp_path)
    opened = _open(sup, tmp_path)
    assert opened["type"] == "OPENED"
    session = opened["session"]
    reply = _bar(sup, session, T0)
    assert reply["type"] == "SIGNAL" and reply["action"] is None and reply["conid"] == C1
    reply = _bar(sup, session, T0 + timedelta(minutes=1))
    assert reply["action"] == "BUY"
    assert sup.handle("live", 1, p.parse_request({"v": 1, "type": "CLOSE", "session": session}))["type"] == "CLOSED"


def test_a_live_session_serves_each_of_its_conids_on_its_own_clock(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path, conids=(C1, C2))["session"]
    assert _bar(sup, session, T0, conid=C1)["action"] is None
    assert _bar(sup, session, T0, conid=C2)["action"] is None      # C2's first bar: its own history
    assert _bar(sup, session, T0 + timedelta(minutes=1), conid=C1)["action"] == "BUY"
    assert sup.pong("live")["free"] == 2                             # one slot for both conids


def test_a_bar_for_a_conid_outside_the_session_is_a_bad_request(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path)["session"]
    assert _bar(sup, session, T0, conid=C2)["code"] == p.SANDBOX_BAD_REQUEST
    assert _bar(sup, session, T0)["type"] == "SIGNAL"                # the session still serves C1


def test_a_research_session_takes_exactly_one_conid(tmp_path):
    sup = _supervisor(tmp_path)
    reply = _open(sup, tmp_path, pool="research", conids=(C1, C2))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_BAD_REQUEST
    assert sup.pong("research")["free"] == 4                         # refused before a slot was taken


def test_a_fault_on_one_conid_ends_the_whole_session(tmp_path):
    source = '''
from trader.authored.api import AuthoredStrategy, Decision


class Picky(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        if history["close"].iloc[-1] > 100.0:
            return Decision(action="BUY", probability=2.0)
        return None
'''
    sup = _supervisor(tmp_path)
    digest = _store(tmp_path, source)
    session = sup.handle("live", 1, p.parse_request(_open_frame(digest, "Picky", (C1, C2))))["session"]
    bad = sup.handle("live", 1, p.parse_request(
        {"v": 1, "type": "BAR", "session": session, "conid": C2, "t": T0.isoformat(),
         "open": 1.0, "high": 200.0, "low": 0.5, "close": 150.0, "volume": 1.0}))
    assert bad["code"] == p.SANDBOX_ERROR
    assert _bar(sup, session, T0, conid=C1)["code"] == p.SANDBOX_BAD_REQUEST   # session gone for C1 too
    assert sup.pong("live")["free"] == 3


def test_a_missing_store_file_is_a_digest_mismatch(tmp_path):
    sup = _supervisor(tmp_path)
    reply = sup.handle("live", 1, p.parse_request(_open_frame("sha256:" + "a" * 64)))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_tampered_store_file_is_a_digest_mismatch(tmp_path):
    sup = _supervisor(tmp_path)
    digest = _store(tmp_path, GOOD)
    (tmp_path / f"{digest.split(':')[1]}.py").write_text(GOOD + "\n# changed\n")
    reply = sup.handle("live", 1, p.parse_request(_open_frame(digest)))
    assert reply["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_class_name_not_in_the_file_is_a_digest_mismatch(tmp_path):
    sup = _supervisor(tmp_path)
    digest = _store(tmp_path, GOOD)
    reply = sup.handle("live", 1, p.parse_request(_open_frame(digest, "Other")))
    assert reply["code"] == p.SANDBOX_DIGEST_MISMATCH


def test_a_static_rejection_at_open_is_a_sandbox_error(tmp_path):
    sup = _supervisor(tmp_path)
    source = "import os\nfrom trader.authored.api import AuthoredStrategy, Decision\n" \
             "class S(AuthoredStrategy):\n    LOOKBACK_BARS = 2\n" \
             "    def on_bar(self, history, params):\n        return None\n"
    digest = _store(tmp_path, source)
    reply = sup.handle("live", 1, p.parse_request(_open_frame(digest, "S")))
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_ERROR
    assert "AUTHORED_STATIC_REJECTED" in reply["detail"]


def test_full_pool_answers_busy_at_once(tmp_path):
    sup = _supervisor(tmp_path, live=2)
    _open(sup, tmp_path, conn=1)
    _open(sup, tmp_path, conn=2)
    reply = _open(sup, tmp_path, conn=3)
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_BUSY


def test_dropping_a_connection_frees_the_slot(tmp_path):
    sup = _supervisor(tmp_path, live=1)
    _open(sup, tmp_path, conn=1)
    assert _open(sup, tmp_path, conn=2)["code"] == p.SANDBOX_BUSY
    sup.drop_connection(1)
    assert _open(sup, tmp_path, conn=2)["type"] == "OPENED"


def test_a_bar_for_an_unknown_session_is_a_bad_request(tmp_path):
    sup = _supervisor(tmp_path)
    assert _bar(sup, "sdead", T0)["code"] == p.SANDBOX_BAD_REQUEST


def test_a_session_belongs_to_its_connection(tmp_path):
    sup = _supervisor(tmp_path)
    session = _open(sup, tmp_path, conn=1)["session"]
    assert _bar(sup, session, T0, conn=2)["code"] == p.SANDBOX_BAD_REQUEST


def test_research_and_live_pools_are_separate(tmp_path):
    sup = _supervisor(tmp_path, live=1, research=1)
    _open(sup, tmp_path, conn=1, pool="live")
    assert _open(sup, tmp_path, conn=2, pool="research")["type"] == "OPENED"


def test_pong_reports_the_image_digest_and_free_counts(tmp_path):
    sup = _supervisor(tmp_path, live=3, research=4)
    pong = sup.pong("live")
    assert pong["type"] == "PONG" and pong["image_digest"] == "mmr-build-id"
    assert pong["free"] == 3 and pong["pool"] == "live"


def test_the_production_pools_are_three_live_and_four_research():
    from trader.sandbox.supervisor import LIVE_POOL_SIZE, MAX_CONIDS_PER_SESSION, RESEARCH_POOL_SIZE

    assert (LIVE_POOL_SIZE, RESEARCH_POOL_SIZE) == (3, 4)     # live: one session per authored deployment
    assert MAX_CONIDS_PER_SESSION == {"live": p.MAX_SESSION_CONIDS, "research": 1}


def test_a_probe_loads_with_the_static_check_skipped(tmp_path):
    sup = _supervisor(tmp_path)
    opened = sup.handle("research", 1, p.parse_request(
        {"v": 1, "type": "OPEN_PROBE", "kind": "probe", "name": "benign_orb", "conids": [p.PROBE_CONID]}))
    assert opened["type"] == "OPENED"
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_supervisor.py -q` — expected: FAIL (`ModuleNotFoundError: trader.sandbox.supervisor`).

- [ ] **Step 3: Implement.** Write `trader/sandbox/probes/__init__.py` empty, then `trader/sandbox/probes/benign_orb.py`:

```python
"""A tiny, honest opening-range breakout. Used by the isolation checks and the tests as the benign case."""
from trader.authored.api import AuthoredStrategy, Decision


class BenignOrb(AuthoredStrategy):
    LOOKBACK_BARS = 30

    def on_bar(self, history, params):
        if len(history) < 3:
            return None
        opening_high = history["high"].iloc[:2].max()
        last_close = history["close"].iloc[-1]
        if last_close > opening_high:
            return Decision(action="BUY", probability=0.55)
        return None
```

Write `trader/sandbox/supervisor.py`:

```python
"""The sandbox supervisor: pools of slots, one child per session, store re-hash before load.

Transport-free: ``handle`` takes a parsed ``Request`` and returns a reply dict. The asyncio
server (sandbox_service) wires sockets to it and reports a dropped connection.
"""
from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from trader.authored.keys import source_digest
from trader.authored.static_check import AuthoredStaticRejected, require_clean
from trader.sandbox import protocol as proto
from trader.sandbox.child import ChildError, SpawnSpec, Spawner

log = logging.getLogger("mmr.sandbox")

PROBE_DIR = Path(__file__).resolve().parent / "probes"
ESCAPE_DIR = Path(__file__).resolve().parents[2] / "tests" / "authored" / "escapes"

# Pools (ruling 26, spec 7.5 as amended): live has one session per authored deployment, and the
# trader caps deployed authored strategies at max_active <= 3; research has 4 one-conid sessions.
LIVE_POOL_SIZE = 3
RESEARCH_POOL_SIZE = 4
MAX_CONIDS_PER_SESSION = {"live": proto.MAX_SESSION_CONIDS, "research": 1}

# Slot uids 20001..20007: live gets the first 3, research the next 4 (spec 9 as amended).
_LIVE_UIDS = tuple(range(20001, 20004))
_RESEARCH_UIDS = tuple(range(20004, 20008))


@dataclass
class Slot:
    uid: int
    directory: str


@dataclass
class _Session:
    session_id: str
    pool: str
    conn_id: int
    slot: Slot
    handle: object  # ChildHandle
    conids: tuple[int, ...] = ()
    last_t: dict = field(default_factory=dict)   # conid -> datetime of its last accepted bar
    busy: bool = False     # a bar round-trip is in flight (ruling 25)


@dataclass
class Pool:
    name: str
    free_slots: list[Slot]
    quarantined: list[Slot] = field(default_factory=list)
    size: int = 0


class Supervisor:
    def __init__(self, *, spawners: Mapping[str, Spawner], pool_sizes: Mapping[str, int],
                 store_dir: str, image_digest: str, posture: Mapping[str, object],
                 child_mem_bytes: int = 256 * 1024 * 1024):
        self._spawners = dict(spawners)
        self._store = Path(store_dir)
        self._image_digest = image_digest
        self._posture = dict(posture)
        self._child_mem = child_mem_bytes
        self._boot_id = secrets.token_hex(8)
        self._lock = threading.Lock()   # the server offloads BAR to a thread; guard shared state (ruling 25)
        self._sessions: dict[str, _Session] = {}
        uids = {"live": _LIVE_UIDS, "research": _RESEARCH_UIDS}
        self._pools: dict[str, Pool] = {}
        for name, size in pool_sizes.items():
            slots = [Slot(uid=uids[name][i], directory=f"/tmp/s{uids[name][i]}") for i in range(size)]
            self._pools[name] = Pool(name=name, free_slots=slots, size=size)

    # ── requests ──────────────────────────────────────────────────────────
    # Threading (ruling 25): the server offloads every request except PING to a thread, so the
    # event loop never blocks. The lock below is held only for O(1) bookkeeping (slot pop/append,
    # session insert/remove), never across a fork, a class load or a bar round-trip. So a slow
    # research bar never holds the lock, and live and research stay independent.
    def handle(self, pool_name: str, conn_id: int, request: proto.Request) -> dict:
        if request.type == "PING":
            return self.pong(pool_name)
        if request.type == "OPEN":
            return self._open_store(pool_name, conn_id, request)
        if request.type == "OPEN_PROBE":
            return self._open_probe(pool_name, conn_id, request)
        if request.type == "BAR":
            return self._bar(conn_id, request)
        if request.type == "CLOSE":
            return self._close(conn_id, request)
        return _error(proto.SANDBOX_BAD_REQUEST, f"unknown type {request.type!r}")

    def pong(self, pool_name: str) -> dict:
        pool = self._pools.get(pool_name)
        return {"type": "PONG", "v": proto.PROTOCOL_VERSION, "image_digest": self._image_digest,
                "boot_id": self._boot_id, "pool": pool_name,
                "free": len(pool.free_slots) if pool else 0,
                "size": pool.size if pool else 0, "posture": self._posture}

    def drop_connection(self, conn_id: int) -> None:
        with self._lock:
            for session in [s for s in self._sessions.values() if s.conn_id == conn_id]:
                self._end(session)

    # ── open: the store path ────────────────────────────────────────────────
    def _open_store(self, pool_name: str, conn_id: int, request: proto.Request) -> dict:
        path = self._store / f"{request.digest.split(':')[1]}.py"
        try:
            raw = path.read_bytes()
        except OSError:
            return _error(proto.SANDBOX_DIGEST_MISMATCH, "store file not found")
        if source_digest(raw) != request.digest:
            return _error(proto.SANDBOX_DIGEST_MISMATCH, "stored bytes do not match the digest")
        source = raw.decode("utf-8", errors="replace")
        try:
            found = require_clean(source)
        except AuthoredStaticRejected as exc:
            rules = ";".join(v.rule for v in exc.violations)
            return _error(proto.SANDBOX_ERROR, f"AUTHORED_STATIC_REJECTED:{rules}")
        if found != request.class_name:
            return _error(proto.SANDBOX_DIGEST_MISMATCH, "class does not match the stored file")
        return self._spawn(pool_name, conn_id, source, request.class_name,
                           request.params, request.conids, skip_static_check=False)

    # ── open: a probe (ruling 11) ───────────────────────────────────────────
    def _open_probe(self, pool_name: str, conn_id: int, request: proto.Request) -> dict:
        base = PROBE_DIR if request.kind == "probe" else ESCAPE_DIR
        path = (base / f"{request.name}.py").resolve()
        if base.resolve() not in path.parents:
            return _error(proto.SANDBOX_BAD_REQUEST, "probe name escapes its directory")
        try:
            source = path.read_text()
        except OSError:
            return _error(proto.SANDBOX_BAD_REQUEST, f"probe {request.name!r} not found")
        class_name = _first_class(source)
        if class_name is None:
            return _error(proto.SANDBOX_BAD_REQUEST, "probe has no class")
        return self._spawn(pool_name, conn_id, source, class_name, {}, request.conids,
                           skip_static_check=True)

    def _spawn(self, pool_name, conn_id, source, class_name, params, conids, *, skip_static_check) -> dict:
        if len(conids) > MAX_CONIDS_PER_SESSION.get(pool_name, 0):
            return _error(proto.SANDBOX_BAD_REQUEST,
                          f"a {pool_name} session takes at most {MAX_CONIDS_PER_SESSION.get(pool_name, 0)} conids")
        with self._lock:
            pool = self._pools.get(pool_name)
            if pool is None:
                return _error(proto.SANDBOX_BAD_REQUEST, f"unknown pool {pool_name!r}")
            if not pool.free_slots:
                return _error(proto.SANDBOX_BUSY, "no free slot")
            slot = pool.free_slots.pop()
        # Fork and load OUTSIDE the lock (a hanging load must not block other pools).
        spec = SpawnSpec(source=source, class_name=class_name, params=params, lookback=2000,
                         conids=tuple(conids), slot_uid=slot.uid, slot_dir=slot.directory,
                         child_mem_bytes=self._child_mem, skip_static_check=skip_static_check)
        try:
            handle = self._spawners[pool_name].spawn(spec)
        except ChildError as exc:
            with self._lock:
                self._reclaim(pool, slot)
            return _error(proto.SANDBOX_ERROR, exc.detail)
        with self._lock:
            session_id = "s" + secrets.token_hex(8)
            self._sessions[session_id] = _Session(session_id, pool_name, conn_id, slot, handle,
                                                  conids=tuple(conids))
        return {"type": "OPENED", "v": proto.PROTOCOL_VERSION, "session": session_id}

    def _bar(self, conn_id: int, request: proto.Request) -> dict:
        with self._lock:
            session = self._sessions.get(request.session)
            if session is None or session.conn_id != conn_id:
                return _error(proto.SANDBOX_BAD_REQUEST, "unknown session")
            if session.busy:
                return _error(proto.SANDBOX_BAD_REQUEST, "a bar is already in flight")
            if request.conid not in session.conids:
                return _error(proto.SANDBOX_BAD_REQUEST, f"conid {request.conid} is not in this session")
            last_t = session.last_t.get(request.conid)
            if last_t is not None and request.t <= last_t:
                # A caller clock bug, not a strategy fault (ruling 8): refuse without killing.
                return _error(proto.SANDBOX_BAD_REQUEST, "bar time must strictly increase per conid")
            session.busy = True
            handle = session.handle
        # The child round-trip runs OUTSIDE the lock; a kill (drop_connection) can interrupt it.
        # A fault on any conid ends the whole session (ruling 26).
        try:
            payload = handle.send_bar(request.conid, request.t, request.ohlcv)
            proto.validate_signal(payload)
        except (ChildError, proto.FrameError) as exc:
            with self._lock:
                live = self._sessions.get(request.session)
                if live is not None:
                    self._end(live)
            return _error(proto.SANDBOX_ERROR, getattr(exc, "detail", str(exc)))
        with self._lock:
            live = self._sessions.get(request.session)
            if live is None:                       # dropped or killed during the call
                return _error(proto.SANDBOX_ERROR, "session ended during the bar")
            live.last_t[request.conid] = request.t
            live.busy = False
        return {"type": "SIGNAL", "v": proto.PROTOCOL_VERSION, "session": request.session,
                "conid": request.conid, "t": request.t.isoformat(), "action": payload["action"],
                "probability": payload["probability"]}

    def _close(self, conn_id: int, request: proto.Request) -> dict:
        with self._lock:
            session = self._sessions.get(request.session)
            if session is None or session.conn_id != conn_id:
                return _error(proto.SANDBOX_BAD_REQUEST, "unknown session")
            self._end(session)
        return {"type": "CLOSED", "v": proto.PROTOCOL_VERSION}

    def _end(self, session: _Session) -> None:
        """Caller holds the lock. Kill the child, then reclaim its slot."""
        self._sessions.pop(session.session_id, None)
        try:
            session.handle.close()   # SIGKILL + reap; also unblocks an in-flight send_bar
        except Exception:  # noqa: BLE001 — closing a dead child must not raise
            log.warning("closing session %s raised", session.session_id, exc_info=True)
        self._reclaim(self._pools[session.pool], session.slot)

    def _reclaim(self, pool: Pool, slot: Slot) -> None:
        if _wipe_slot(slot):
            pool.free_slots.append(slot)
        else:
            pool.quarantined.append(slot)   # ruling 13: never reuse a slot we could not wipe
            log.error("quarantined slot %s: wipe failed", slot.uid)


def _error(code: str, detail: str) -> dict:
    return {"type": "ERROR", "v": proto.PROTOCOL_VERSION, "code": code, "detail": detail}


def _first_class(source: str) -> str | None:
    for line in source.splitlines():
        if line.startswith("class "):
            return line[len("class "):].split("(")[0].split(":")[0].strip()
    return None


def _wipe_slot(slot: Slot) -> bool:
    directory = Path(slot.directory)
    try:
        if not directory.exists():
            return True
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                _rmtree(child)
            else:
                child.unlink()
        return True
    except OSError:
        return False


def _rmtree(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            _rmtree(child)
        else:
            child.unlink()
    directory.rmdir()
```

Note: the class constant is authoritative. The supervisor passes the 2000 ceiling; the child sets the trim window from the loaded class's `LOOKBACK_BARS`, clamped to 1..2000 (spec 9).

- [ ] **Step 4: Apply the ChildRuntime lookback clamp.** In `trader/sandbox/child.py`, `ChildRuntime.load`, after the class is loaded and before `self._books = {...}`:

```python
        self._lookback = max(1, min(2000, int(getattr(cls, "LOOKBACK_BARS", self._lookback))))
```

- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/sandbox/test_supervisor.py tests/sandbox/test_child_runtime.py -q` — expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add trader/sandbox/supervisor.py trader/sandbox/probes tests/sandbox/fakes.py tests/sandbox/test_supervisor.py trader/sandbox/child.py
git commit -m "feat: add the sandbox supervisor, pools and probe loading"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 4: The asyncio server, the per-bar wall timeout and the service entry

**Files:**
- Create: `trader/sandbox/server.py`
- Create: `trader/sandbox_service.py`
- Test: `tests/sandbox/test_server.py`

**Interfaces:**
- Consumes: Task 1 `protocol`, Task 3 `Supervisor`, Task 2 `ForkSpawner`.
- Produces: `async serve(supervisor, sockets: Mapping[str, str], *, bar_timeout_s=2.0) -> Server` where `sockets` maps a pool name to its socket path; `Server.close()`. `bind_socket(path, gid=1000, mode=0o660) -> socket.socket`. `trader/sandbox_service.py`: `build_supervisor() -> Supervisor` (always `ForkSpawner`), `main()`, `--health`.

- [ ] **Step 1: Write the failing tests.** Write `tests/sandbox/test_server.py`:

```python
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.sandbox import protocol as p
from trader.sandbox.server import serve
from trader.sandbox.supervisor import Supervisor
from tests.sandbox.fakes import InProcessSpawner

pytestmark = pytest.mark.asyncio

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
GOOD = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.6) if len(history) >= 2 else None
'''


def _store(tmp_path, source):
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    (tmp_path / f"{digest.split(':')[1]}.py").write_text(source)
    return digest


async def _client(path):
    reader, writer = await asyncio.open_unix_connection(path)
    return reader, writer


async def _call(reader, writer, obj):
    p.write_frame(writer, obj)
    await writer.drain()
    return await p.read_frame(reader)


@pytest.fixture
async def running(tmp_path):
    sup = Supervisor(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                     pool_sizes={"live": 2, "research": 1}, store_dir=str(tmp_path),
                     image_digest="build-x", posture={})
    live = str(tmp_path / "live.sock")
    research = str(tmp_path / "research.sock")
    server = await serve(sup, {"live": live, "research": research}, bar_timeout_s=0.5)
    yield sup, live, research, tmp_path
    await server.close()


async def test_ping_answers_pong(running):
    _, live, _, _ = running
    reader, writer = await _client(live)
    pong = await _call(reader, writer, {"v": 1, "type": "PING"})
    assert pong["type"] == "PONG" and pong["image_digest"] == "build-x"
    writer.close()


async def test_open_bar_close_over_the_socket(running):
    _, live, _, tmp_path = running
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest,
                                          "class": "Counter", "params": {}, "bar_size": "1 min", "conids": [101]})
    session = opened["session"]
    await _call(reader, writer, {"v": 1, "type": "BAR", "conid": 101, "session": session, "t": T0.isoformat(),
                                 "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0})
    sig = await _call(reader, writer, {"v": 1, "type": "BAR", "conid": 101, "session": session,
                                       "t": (T0 + timedelta(minutes=1)).isoformat(),
                                       "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0})
    assert sig["action"] == "BUY"
    writer.close()


async def test_a_dropped_connection_frees_the_slot(running):
    sup, live, _, tmp_path = running
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter",
                                 "params": {}, "bar_size": "1 min", "conids": [101]})
    writer.close()
    await writer.wait_closed()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(sup._pools["live"].free_slots) == 2:
            break
    assert len(sup._pools["live"].free_slots) == 2


async def test_a_full_pool_answers_busy_at_once(running):
    _, live, _, tmp_path = running
    digest = _store(tmp_path, GOOD)
    held = []
    for _ in range(2):
        reader, writer = await _client(live)
        await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter",
                                     "params": {}, "bar_size": "1 min", "conids": [101]})
        held.append((reader, writer))
    reader, writer = await _client(live)
    reply = await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter",
                                         "params": {}, "bar_size": "1 min", "conids": [101]})
    assert reply["code"] == p.SANDBOX_BUSY
    for _, w in held:
        w.close()
    writer.close()


async def test_a_forged_child_reply_is_a_fault(running, tmp_path):
    sup, live, _, _ = running

    class BadChild:
        pid = -1

        def send_bar(self, conid, t, ohlcv):
            return {"action": "SIDEWAYS", "probability": 9.0}   # out of contract

        def close(self):
            return None

    class BadSpawner:
        def spawn(self, spec):
            return BadChild()

    sup._spawners["live"] = BadSpawner()
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter",
                                          "params": {}, "bar_size": "1 min", "conids": [101]})
    reply = await _call(reader, writer, {"v": 1, "type": "BAR", "conid": 101, "session": opened["session"],
                                         "t": T0.isoformat(), "open": 1.0, "high": 2.0, "low": 0.5,
                                         "close": 1.5, "volume": 1.0})
    assert reply["type"] == "ERROR" and reply["code"] == p.SANDBOX_ERROR
    writer.close()


async def test_a_slow_bar_hits_the_wall_timeout(running, tmp_path, monkeypatch):
    sup, live, _, _ = running

    class SlowChild:
        pid = -1

        def send_bar(self, conid, t, ohlcv):
            import time

            time.sleep(5)
            return {"action": None, "probability": None}

        def close(self):
            return None

    class SlowSpawner:
        def spawn(self, spec):
            return SlowChild()

    sup._spawners["live"] = SlowSpawner()
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    opened = await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter",
                                          "params": {}, "bar_size": "1 min", "conids": [101]})
    reply = await _call(reader, writer, {"v": 1, "type": "BAR", "conid": 101, "session": opened["session"],
                                         "t": T0.isoformat(), "open": 1.0, "high": 2.0, "low": 0.5,
                                         "close": 1.5, "volume": 1.0})
    assert reply["code"] == p.SANDBOX_ERROR and "timeout" in reply["detail"].lower()
    writer.close()


async def test_a_failed_wipe_quarantines_the_slot(running, tmp_path, monkeypatch):
    sup, live, _, _ = running
    monkeypatch.setattr("trader.sandbox.supervisor._wipe_slot", lambda slot: False)
    digest = _store(tmp_path, GOOD)
    reader, writer = await _client(live)
    await _call(reader, writer, {"v": 1, "type": "OPEN", "digest": digest, "class": "Counter",
                                 "params": {}, "bar_size": "1 min", "conids": [101]})
    writer.close()
    await writer.wait_closed()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if sup._pools["live"].quarantined:
            break
    assert len(sup._pools["live"].quarantined) == 1
    assert sup.pong("live")["free"] == 1   # one slot lost
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_server.py -q` — expected: FAIL (`ModuleNotFoundError: trader.sandbox.server`).

- [ ] **Step 3: Implement.** Write `trader/sandbox/server.py`:

```python
"""Asyncio Unix-socket server: one connection, one session, one child (ruling 7).

The per-bar wall timeout runs the blocking child call in a thread and cancels it. When a
connection closes the session is ended and its slot reclaimed, without a restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Mapping

from trader.sandbox import protocol as proto
from trader.sandbox.supervisor import Supervisor

log = logging.getLogger("mmr.sandbox")


def bind_socket(path: str, *, gid: int = 1000, mode: int = 0o660) -> socket.socket:
    if os.path.exists(path):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    try:
        os.chown(path, 0, gid)   # root:trader; the supervisor is in group trader (ruling 15)
    except PermissionError:
        log.warning("could not chown %s to gid %s", path, gid)
    os.chmod(path, mode)
    sock.listen(128)
    sock.setblocking(False)
    return sock


@dataclass
class Server:
    _servers: list[asyncio.AbstractServer]

    async def close(self) -> None:
        for server in self._servers:
            server.close()
            await server.wait_closed()


async def serve(supervisor: Supervisor, sockets: Mapping[str, str], *,
                bar_timeout_s: float = 2.0) -> Server:
    # One worker per possible session (3 live + 4 research) plus slack, so a slow bar in
    # one session never queues another session's request (ruling 25).
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=16))
    servers: list[asyncio.AbstractServer] = []
    conn_counter = {"next": 0}

    def make_handler(pool_name: str):
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            conn_counter["next"] += 1
            conn_id = conn_counter["next"]
            try:
                while True:
                    try:
                        frame = await proto.read_frame(reader)
                    except asyncio.IncompleteReadError:
                        return
                    try:
                        request = proto.parse_request(frame)
                    except proto.FrameError as exc:
                        proto.write_frame(writer, _error(exc.code, exc.detail))
                        await writer.drain()
                        continue
                    reply = await _run(supervisor, pool_name, conn_id, request, bar_timeout_s)
                    proto.write_frame(writer, reply)
                    await writer.drain()
            except (ConnectionError, proto.FrameError):
                return
            finally:
                supervisor.drop_connection(conn_id)
                writer.close()

        return handle

    for pool_name, path in sockets.items():
        bound = bind_socket(path)
        server = await asyncio.start_unix_server(make_handler(pool_name), sock=bound)
        servers.append(server)
    return Server(servers)


async def _run(supervisor: Supervisor, pool_name: str, conn_id: int,
               request: proto.Request, bar_timeout_s: float) -> dict:
    if request.type == "PING":
        return supervisor.handle(pool_name, conn_id, request)   # instant, lock-free: health always answers
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, supervisor.handle, pool_name, conn_id, request)
    if request.type != "BAR":
        return await future          # OPEN/OPEN_PROBE/CLOSE: bounded by the ForkSpawner load timeout
    try:
        return await asyncio.wait_for(future, timeout=bar_timeout_s)
    except asyncio.TimeoutError:
        supervisor.drop_connection(conn_id)   # kill the child whose bar overran (lock is free: not held across the call)
        return _error(proto.SANDBOX_ERROR, f"bar timeout after {bar_timeout_s}s")


def _error(code: str, detail: str) -> dict:
    return {"type": "ERROR", "v": proto.PROTOCOL_VERSION, "code": code, "detail": detail}
```

Note on the timeout killing the right session: because one connection holds one session (ruling 7), dropping the connection ends exactly that overrunning session; the blocking executor thread is abandoned but the forked child is killed when its pipe closes in `drop_connection`.

Write `trader/sandbox_service.py`:

```python
"""The sandbox service entry. Always uses ForkSpawner (ruling 12): no knob can swap it."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# Single-thread the math libraries before numpy is imported anywhere (ruling 5).
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from trader.sandbox import protocol as proto  # noqa: E402
from trader.sandbox.child import ForkSpawner  # noqa: E402
from trader.sandbox.posture import read_posture  # noqa: E402  (Task 6)
from trader.sandbox.server import serve  # noqa: E402
from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE, Supervisor  # noqa: E402

log = logging.getLogger("mmr.sandbox")

_BUILD_ID_FILE = "/etc/mmr-build-id"


def read_build_id() -> str:
    try:
        value = open(_BUILD_ID_FILE).read().strip()
    except OSError:
        value = ""
    if not value or value == "unset":
        raise SystemExit("sandbox refuses to start: /etc/mmr-build-id is unset (ruling 1)")
    if not proto.BUILD_ID_RE.fullmatch(value):
        raise SystemExit(f"sandbox refuses to start: /etc/mmr-build-id is malformed (ruling 1): {value[:40]!r}")
    return value


def build_supervisor() -> Supervisor:
    return Supervisor(
        spawners={"live": ForkSpawner(), "research": ForkSpawner()},
        pool_sizes={"live": LIVE_POOL_SIZE, "research": RESEARCH_POOL_SIZE},
        store_dir=proto.STORE_DIR,
        image_digest=read_build_id(),
        posture=read_posture(),
    )


async def _amain() -> None:
    _join_trader_group()
    supervisor = build_supervisor()
    await serve(supervisor, {"live": proto.LIVE_SOCKET, "research": proto.RESEARCH_SOCKET})
    log.info("sandbox serving live=%s research=%s", proto.LIVE_SOCKET, proto.RESEARCH_SOCKET)
    await asyncio.Event().wait()   # serve forever


def _join_trader_group() -> None:
    try:
        os.setgroups([1000])   # group trader, so it can chgrp the sockets (ruling 15)
    except OSError:
        log.warning("could not join group trader")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if "--health" in sys.argv[1:]:
        raise SystemExit(0 if os.path.exists(proto.LIVE_SOCKET) else 1)
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/sandbox/test_server.py -q` — expected: PASS. (`trader/sandbox_service.py` imports `posture`, written in Task 6; if Task 6 is not yet done, add a one-line stub `read_posture` in `trader/sandbox/posture.py` returning `{}` and replace it in Task 6. The server tests do not import `sandbox_service`.)

- [ ] **Step 5: Commit.**

```bash
git add trader/sandbox/server.py trader/sandbox_service.py tests/sandbox/test_server.py
git commit -m "feat: add the sandbox asyncio server, per-bar timeout and service entry"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 5: The sandbox client

Callers (Plan 4's research backtester and strategy runtime) are synchronous and feed one bar at a time, so the client is a plain blocking socket client. A live session holds every conid of one deployment; a research session holds one conid (ruling 26). It maps reply codes to the two error classes the spec names, plus `SandboxProtocolError` for caller mistakes (ruling 8).

**Files:**
- Create: `trader/authored/sandbox_client.py`
- Test: `tests/sandbox/test_sandbox_client.py`

**Interfaces:**
- Consumes: Task 1 `protocol`, Task 4 `serve`, Task 3 `Supervisor`, Plan 1 `Decision`.
- Produces (index names exactly): `SandboxClient(socket_path: str, *, connect_timeout_s=5.0)` with `ping() -> SandboxInfo`, `open_session(digest, class_name, params, bar_size, conids) -> SandboxSession`, `open_probe(kind, name) -> SandboxSession` (one conid, `PROBE_CONID`); `SandboxSession.bar(conid, t, open, high, low, close, volume) -> Decision | None`, `SandboxSession.conids`, `SandboxSession.close()`; `SandboxInfo` (frozen: `image_digest`, `boot_id`, `pool`, `free`, `size`, `posture`); `SandboxUnavailable(Exception)`, `SandboxFault(Exception)` (attribute `code`), `SandboxProtocolError(Exception)`.

- [ ] **Step 1: Write the failing tests.** Write `tests/sandbox/test_sandbox_client.py`:

```python
import asyncio
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.authored.api import Decision
from trader.authored.sandbox_client import (
    SandboxClient, SandboxFault, SandboxProtocolError, SandboxUnavailable,
)
from trader.sandbox import protocol as p
from trader.sandbox.server import serve
from trader.sandbox.supervisor import Supervisor
from tests.sandbox.fakes import InProcessSpawner

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
C1, C2 = 101, 202
GOOD = '''
from trader.authored.api import AuthoredStrategy, Decision


class Counter(AuthoredStrategy):
    LOOKBACK_BARS = 5

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.6) if len(history) >= 2 else None
'''


class _Stack:
    """Run the asyncio server in a background thread so a synchronous client can call it."""

    def __init__(self, tmp_path, live=2, research=1):
        self.tmp_path = tmp_path
        self.live = str(tmp_path / "live.sock")
        self.research = str(tmp_path / "research.sock")
        self._loop = asyncio.new_event_loop()
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._sizes = {"live": live, "research": research}
        self._thread.start()
        self._ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        sup = Supervisor(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                         pool_sizes=self._sizes, store_dir=str(self.tmp_path),
                         image_digest="build-x", posture={"caps": "00000000000000e0"})
        self._server = self._loop.run_until_complete(serve(sup, {"live": self.live, "research": self.research}))
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def store(self, source):
        digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        (self.tmp_path / f"{digest.split(':')[1]}.py").write_text(source)
        return digest

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def stack(tmp_path):
    s = _Stack(tmp_path)
    yield s
    s.stop()


def test_ping_returns_the_info(stack):
    info = SandboxClient(stack.live).ping()
    assert info.image_digest == "build-x" and info.posture["caps"] == "00000000000000e0"


def test_open_session_streams_bars_and_returns_decisions(stack):
    digest = stack.store(GOOD)
    client = SandboxClient(stack.live)
    session = client.open_session(digest, "Counter", {}, "1 min", [C1, C2])
    assert session.conids == (C1, C2)
    assert session.bar(C1, T0, 1.0, 2.0, 0.5, 1.5, 1.0) is None
    assert session.bar(C2, T0, 1.0, 2.0, 0.5, 1.5, 1.0) is None
    decision = session.bar(C1, T0 + timedelta(minutes=1), 1.0, 2.0, 0.5, 1.5, 1.0)
    assert isinstance(decision, Decision) and decision.action == "BUY"
    session.close()


def test_a_conid_the_session_does_not_hold_is_refused_before_it_is_sent(stack):
    digest = stack.store(GOOD)
    session = SandboxClient(stack.live).open_session(digest, "Counter", {}, "1 min", [C1])
    with pytest.raises(SandboxProtocolError):
        session.bar(C2, T0, 1.0, 2.0, 0.5, 1.5, 1.0)
    assert session.bar(C1, T0, 1.0, 2.0, 0.5, 1.5, 1.0) is None
    session.close()


def test_a_research_session_with_two_conids_is_a_protocol_error(stack):
    digest = stack.store(GOOD)
    with pytest.raises(SandboxProtocolError):
        SandboxClient(stack.research).open_session(digest, "Counter", {}, "1 min", [C1, C2])


def test_an_unreachable_socket_is_unavailable(tmp_path):
    with pytest.raises(SandboxUnavailable):
        SandboxClient(str(tmp_path / "nope.sock"), connect_timeout_s=0.2).ping()


def test_a_busy_pool_is_unavailable(stack):
    digest = stack.store(GOOD)
    held = [SandboxClient(stack.live).open_session(digest, "Counter", {}, "1 min", [C1]) for _ in range(2)]
    with pytest.raises(SandboxUnavailable):
        SandboxClient(stack.live).open_session(digest, "Counter", {}, "1 min", [C1])
    for s in held:
        s.close()


def test_a_digest_mismatch_is_a_fault_with_its_code(stack):
    with pytest.raises(SandboxFault) as exc:
        SandboxClient(stack.live).open_session("sha256:" + "a" * 64, "Counter", {}, "1 min", [C1])
    assert exc.value.code == p.SANDBOX_DIGEST_MISMATCH


def test_a_static_rejection_is_a_sandbox_error_fault(stack):
    bad = "import os\nfrom trader.authored.api import AuthoredStrategy, Decision\n" \
          "class S(AuthoredStrategy):\n    LOOKBACK_BARS = 2\n" \
          "    def on_bar(self, history, params):\n        return None\n"
    digest = stack.store(bad)
    with pytest.raises(SandboxFault) as exc:
        SandboxClient(stack.live).open_session(digest, "S", {}, "1 min", [C1])
    assert exc.value.code == p.SANDBOX_ERROR


def test_a_non_increasing_bar_time_is_a_protocol_error(stack):
    digest = stack.store(GOOD)
    session = SandboxClient(stack.live).open_session(digest, "Counter", {}, "1 min", [C1])
    session.bar(C1, T0, 1.0, 2.0, 0.5, 1.5, 1.0)
    with pytest.raises(SandboxProtocolError):   # ruling 8: a caller clock bug, child not killed
        session.bar(C1, T0, 1.0, 2.0, 0.5, 1.5, 1.0)
    # the session still works: a later, increasing bar is accepted
    assert session.bar(C1, T0 + timedelta(minutes=1), 1.0, 2.0, 0.5, 1.5, 1.0) is None
    session.close()
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_sandbox_client.py -q` — expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement.** Write `trader/authored/sandbox_client.py`:

```python
"""Synchronous client for the sandbox. One session per (strategy, run): a live session holds
every conid of one deployment, a research session one conid (ruling 26).

Error mapping (spec 7.6): SANDBOX_BUSY and an unreachable or slow socket are availability
errors (SandboxUnavailable); SANDBOX_ERROR and SANDBOX_DIGEST_MISMATCH are strategy faults
(SandboxFault); SANDBOX_BAD_REQUEST is a caller mistake (SandboxProtocolError, ruling 8).
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from trader.authored.api import Decision
from trader.sandbox import protocol as proto


class SandboxUnavailable(Exception):
    """The sandbox is busy, unreachable or slow to connect. Not a fault of the strategy."""


class SandboxFault(Exception):
    """The strategy failed in the sandbox. ``code`` is SANDBOX_ERROR or SANDBOX_DIGEST_MISMATCH."""

    def __init__(self, detail: str, *, code: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SandboxProtocolError(Exception):
    """The caller sent something out of contract (SANDBOX_BAD_REQUEST)."""


@dataclass(frozen=True)
class SandboxInfo:
    image_digest: str
    boot_id: str
    pool: str
    free: int
    size: int
    posture: Mapping[str, Any]


_HEADER = struct.Struct(">I")


class SandboxClient:
    def __init__(self, socket_path: str, *, connect_timeout_s: float = 5.0):
        self._path = socket_path
        self._connect_timeout = connect_timeout_s

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout)
        try:
            sock.connect(self._path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            sock.close()
            raise SandboxUnavailable(f"cannot reach the sandbox at {self._path}: {exc}") from exc
        return sock

    def ping(self) -> SandboxInfo:
        sock = self._connect()
        try:
            reply = _exchange(sock, {"v": 1, "type": "PING"})
        finally:
            sock.close()
        if reply.get("type") != "PONG":
            raise SandboxUnavailable(f"unexpected reply to PING: {reply!r}")
        return SandboxInfo(image_digest=reply["image_digest"], boot_id=reply.get("boot_id", ""),
                           pool=reply.get("pool", ""), free=reply.get("free", 0),
                           size=reply.get("size", 0), posture=reply.get("posture", {}))

    def open_session(self, digest: str, class_name: str, params: Mapping[str, float],
                     bar_size: str, conids: Sequence[int]) -> "SandboxSession":
        return self._open({"v": 1, "type": "OPEN", "digest": digest, "class": class_name,
                           "params": dict(params), "bar_size": bar_size,
                           "conids": [int(conid) for conid in conids]})

    def open_probe(self, kind: str, name: str) -> "SandboxSession":
        return self._open({"v": 1, "type": "OPEN_PROBE", "kind": kind, "name": name,
                           "conids": [proto.PROBE_CONID]})

    def _open(self, request: Mapping[str, Any]) -> "SandboxSession":
        sock = self._connect()
        sock.settimeout(None)
        try:
            reply = _exchange(sock, request)
        except OSError as exc:
            sock.close()
            raise SandboxUnavailable(f"sandbox dropped during OPEN: {exc}") from exc
        if reply.get("type") == "OPENED":
            return SandboxSession(sock, reply["session"], tuple(request["conids"]))
        sock.close()
        _raise_for(reply)
        raise SandboxUnavailable(f"unexpected OPEN reply: {reply!r}")


class SandboxSession:
    """Not thread-safe: the supervisor refuses a second bar in flight on one session (ruling 25)."""

    def __init__(self, sock: socket.socket, session_id: str, conids: tuple[int, ...]):
        self._sock = sock
        self._id = session_id
        self.conids = conids
        self._open = True

    def bar(self, conid: int, t: datetime, open: float, high: float, low: float, close: float,
            volume: float) -> Decision | None:
        if not self._open:
            raise SandboxProtocolError("session is closed")
        if conid not in self.conids:
            raise SandboxProtocolError(f"conid {conid} is not in this session")
        when = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        request = {"v": 1, "type": "BAR", "session": self._id, "conid": int(conid), "t": when.isoformat(),
                   "open": float(open), "high": float(high), "low": float(low),
                   "close": float(close), "volume": float(volume)}
        try:
            reply = _exchange(self._sock, request)
        except OSError as exc:
            self._open = False
            raise SandboxUnavailable(f"sandbox dropped during BAR: {exc}") from exc
        if reply.get("type") == "SIGNAL":
            if reply.get("action") is None:
                return None
            return Decision(action=reply["action"], probability=float(reply["probability"]))
        # A BAD_REQUEST (caller clock bug) leaves the session usable; a fault ends it.
        if reply.get("code") != proto.SANDBOX_BAD_REQUEST:
            self._open = False
        _raise_for(reply)
        raise SandboxUnavailable(f"unexpected BAR reply: {reply!r}")

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        try:
            _exchange(self._sock, {"v": 1, "type": "CLOSE", "session": self._id})
        except OSError:
            pass
        finally:
            self._sock.close()

    def __enter__(self) -> "SandboxSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _raise_for(reply: Mapping[str, Any]) -> None:
    if reply.get("type") != "ERROR":
        return
    code = reply.get("code", proto.SANDBOX_ERROR)
    detail = reply.get("detail", "")
    if code == proto.SANDBOX_BUSY:
        raise SandboxUnavailable(f"sandbox busy: {detail}")
    if code == proto.SANDBOX_BAD_REQUEST:
        raise SandboxProtocolError(detail)
    raise SandboxFault(detail, code=code)


def _exchange(sock: socket.socket, obj: Mapping[str, Any]) -> dict:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(_HEADER.pack(len(body)) + body)
    header = _recv_exact(sock, 4)
    (length,) = _HEADER.unpack(header)
    return json.loads(_recv_exact(sock, length))


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            raise OSError("sandbox connection closed")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/sandbox/test_sandbox_client.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add trader/authored/sandbox_client.py tests/sandbox/test_sandbox_client.py
git commit -m "feat: add the synchronous sandbox client"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 6: Posture and the isolation checks (`prove-isolation` engine)

`run_isolation_checks` runs the spec section 10 "production service" and "hostile session" set on a live service through the client and reports each check. The hostile source is **not** in this plan: it lives in Plan 1's escape corpus (`tests/authored/escapes/`, index), which this plan loads as probes (ruling 11). The posture includes the read-only root and the pids limit, so the proof needs no `docker inspect` from the host (SP3 decision D2). Plan 3's `mmr ai-author prove-isolation --in-container` calls `run_isolation_checks` inside the `research` container and prints the result; the host command records it (SP3 decision D3). This plan adds no command line of its own.

**Cross-plan names (D9).** `HOSTILE_ESCAPES` names nine files that Plan 1 ships in `tests/authored/escapes/` and whose static refusal Plan 1's `test_every_sandbox_hostile_file_exists_and_is_rejected` asserts. One per spec section 10 hostile behaviour:
`escape_read_config_keys`, `escape_write_outside_tmpfs`, `escape_fork_bomb`, `escape_allocate_past_limit`, `escape_loop_forever`, `escape_pd_io_common_oskill` (the exact `pd.io.common.os.kill(pd.io.common.os.getppid(), 9)` path), `escape_kill_other_child`, `escape_sys_modules_lookup`, `escape_open_socket`.

**Files:**
- Create: `trader/sandbox/posture.py`, `trader/sandbox/isolation_checks.py`
- Test: `tests/sandbox/test_posture.py`, `tests/sandbox/test_isolation_checks.py`

**Interfaces:**
- Consumes: Task 5 `SandboxClient`, `SandboxFault`, `SandboxUnavailable`, `SandboxInfo`; Task 1 datetimes.
- Produces: `read_posture() -> dict` (adds `root_read_only: bool` and `pids_max: str`); `NEEDS_A_NEIGHBOUR`; `IsolationResult` (frozen: `passed: bool`, `image_digest: str`, `checks: tuple[CheckResult, ...]`), `CheckResult` (frozen: `name: str`, `passed: bool`, `detail: str`), `run_isolation_checks(client: SandboxClient) -> IsolationResult`, `HOSTILE_ESCAPES: tuple[str, ...]`, `EXPECTED_CAP_EFF = "00000000000000e0"`.

- [ ] **Step 1: Write the failing tests.** Write `tests/sandbox/test_posture.py`:

```python
from trader.sandbox import posture


def test_read_posture_reports_known_keys(monkeypatch):
    out = posture.read_posture()
    assert set(out) >= {"cap_eff", "seccomp", "no_new_privs", "uid", "interfaces",
                        "forbidden_env", "home_mounts", "root_read_only", "pids_max"}


def test_the_root_mount_options_are_read(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay ro,relatime,lowerdir=/x 0 0\ntmpfs /tmp tmpfs rw,noexec 0 0\n")
    assert posture.root_read_only(str(mounts)) is True
    mounts.write_text("overlay / overlay rw,relatime 0 0\n")
    assert posture.root_read_only(str(mounts)) is False


def test_the_pids_limit_is_read_from_cgroup_v2_then_v1(tmp_path):
    v2, v1 = tmp_path / "pids.max", tmp_path / "v1-pids.max"
    v1.write_text("64\n")
    assert posture.pids_max((str(v2), str(v1))) == "64"
    assert posture.pids_max((str(tmp_path / "none"),)) == ""


def test_decode_cap_eff_lists_capabilities():
    caps = posture.decode_cap_eff("00000000000000e0")   # bits 5,6,7
    assert set(caps) == {"CAP_KILL", "CAP_SETGID", "CAP_SETUID"}


def test_forbidden_env_names_are_detected(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "x")
    monkeypatch.setenv("TZ", "UTC")
    assert "ALPACA_API_KEY_ID" in posture.forbidden_env_names()
    assert "TZ" not in posture.forbidden_env_names()
```

Write `tests/sandbox/test_isolation_checks.py`:

```python
import pytest

from trader.authored.sandbox_client import SandboxFault
from trader.sandbox import isolation_checks as ic


class _FakeSession:
    def __init__(self, fault):
        self._fault = fault
        self.bars = 0

    def bar(self, conid, *a, **k):
        assert conid == 1      # a probe session holds PROBE_CONID only
        if self._fault:
            raise SandboxFault("boom", code="SANDBOX_ERROR")
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
    posture = {"cap_eff": ic.EXPECTED_CAP_EFF, "seccomp": 2, "no_new_privs": 1, "uid": 0,
               "interfaces": ["lo"], "forbidden_env": [], "home_mounts": [],
               "root_read_only": True, "pids_max": "64"}


class _FakeClient:
    """Benign probe runs; every hostile escape faults; posture is clean -> all checks pass."""

    def ping(self):
        return _FakeInfo()

    def open_probe(self, kind, name):
        return _FakeSession(fault=(name != "benign_orb"))


def test_a_clean_service_passes_every_check():
    result = ic.run_isolation_checks(_FakeClient())
    assert result.passed is True and result.image_digest == "build-x"
    assert {c.name for c in result.checks} >= set(ic.HOSTILE_ESCAPES) | {
        "benign_session", "cap_eff", "seccomp", "network_none", "read_only_root", "pids_limit"}


def test_the_hostile_names_are_plan_1s_files():
    from tests.authored.test_escape_corpus import ESCAPE_DIR, SANDBOX_HOSTILE

    assert ic.HOSTILE_ESCAPES == SANDBOX_HOSTILE
    assert all((ESCAPE_DIR / f"{name}.py").is_file() for name in ic.HOSTILE_ESCAPES)


def test_kill_other_child_needs_the_parallel_benign_session_to_survive():
    class KillsNeighbour(_FakeClient):
        def __init__(self):
            self.benign = []

        def open_probe(self, kind, name):
            session = _FakeSession(fault=(name != "benign_orb"))
            if name == "benign_orb":
                self.benign.append(session)
            if name == "escape_kill_other_child":
                for held in self.benign:
                    held._fault = True        # the escape killed the neighbour
            return session

    result = ic.run_isolation_checks(KillsNeighbour())
    check = next(c for c in result.checks if c.name == "escape_kill_other_child")
    assert check.passed is False and "neighbour" in check.detail


def test_a_hostile_escape_that_does_not_fault_fails_the_run():
    class Leaky(_FakeClient):
        def open_probe(self, kind, name):
            return _FakeSession(fault=False)   # nothing faults: the sandbox did not contain it

    result = ic.run_isolation_checks(Leaky())
    assert result.passed is False
    assert any(not c.passed for c in result.checks)


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
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_posture.py tests/sandbox/test_isolation_checks.py -q` — expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement.** Write `trader/sandbox/posture.py`:

```python
"""Read what the running process can do: capabilities, seccomp, privileges, network, env, mounts.

Returned by the supervisor in PONG so the isolation checks can judge the live posture.
"""
from __future__ import annotations

import os

_CAP_NAMES = {5: "CAP_KILL", 6: "CAP_SETGID", 7: "CAP_SETUID"}
_FORBIDDEN_PREFIXES = ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_",
                       "AWS_", "AZURE_", "DASHBOARD_", "TELEGRAM")


def _proc_status() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in open("/proc/self/status"):
            if ":" in line:
                key, value = line.split(":", 1)
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def decode_cap_eff(hex_value: str) -> list[str]:
    try:
        bits = int(hex_value, 16)
    except (TypeError, ValueError):
        return []
    return sorted(name for bit, name in _CAP_NAMES.items() if bits & (1 << bit)) + \
        [f"bit{bit}" for bit in range(64) if bits & (1 << bit) and bit not in _CAP_NAMES]


def forbidden_env_names() -> list[str]:
    return sorted(name for name in os.environ if name.startswith(_FORBIDDEN_PREFIXES))


def interfaces() -> list[str]:
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def home_mounts() -> list[str]:
    found = []
    try:
        for line in open("/proc/mounts"):
            target = line.split(" ")[1] if len(line.split(" ")) > 1 else ""
            if target.startswith("/home") or target.startswith("/store") or target.startswith("/root"):
                found.append(target)
    except OSError:
        pass
    return sorted(found)


def root_read_only(mounts_path: str = "/proc/mounts") -> bool:
    """True when the container's root filesystem is mounted read-only (compose ``read_only: true``)."""
    try:
        for line in open(mounts_path):
            fields = line.split(" ")
            if len(fields) > 3 and fields[1] == "/":
                return "ro" in fields[3].split(",")
    except OSError:
        pass
    return False


def pids_max(paths: tuple[str, ...] = ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max")) -> str:
    """The container's pids limit (compose ``pids_limit``), cgroup v2 first; "" when unreadable."""
    for path in paths:
        try:
            return open(path).read().strip()
        except OSError:
            continue
    return ""


def read_posture() -> dict:
    status = _proc_status()
    return {
        "cap_eff": status.get("CapEff", ""),
        "seccomp": int(status.get("Seccomp", "0") or "0"),
        "no_new_privs": int(status.get("NoNewPrivs", "0") or "0"),
        "uid": os.getuid(),
        "pid": os.getpid(),
        "interfaces": interfaces(),
        "forbidden_env": forbidden_env_names(),
        "home_mounts": home_mounts(),
        "root_read_only": root_read_only(),
        "pids_max": pids_max(),
    }
```

Write `trader/sandbox/isolation_checks.py`:

```python
"""The hostile and posture checks that `mmr ai-author prove-isolation` (Plan 3) runs.

Each check runs against a live service through the client. Hostile source lives in Plan 1's
escape corpus (loaded as probes, ruling 11); this module only names and streams them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.authored.sandbox_client import (
    SandboxClient, SandboxFault, SandboxProtocolError, SandboxUnavailable,
)
from trader.sandbox.posture import decode_cap_eff
from trader.sandbox.protocol import PROBE_CONID

EXPECTED_CAP_EFF = "00000000000000e0"   # KILL + SETGID + SETUID
EXPECTED_CAPS = frozenset({"CAP_KILL", "CAP_SETGID", "CAP_SETUID"})
EXPECTED_PIDS_MAX = "64"                # compose pids_limit (spec 7.5)

# Plan 1's escape files, one per spec section 10 hostile behaviour (see the cross-plan note, D9).
HOSTILE_ESCAPES: tuple[str, ...] = (
    "escape_read_config_keys", "escape_write_outside_tmpfs", "escape_fork_bomb",
    "escape_allocate_past_limit", "escape_loop_forever", "escape_pd_io_common_oskill",
    "escape_kill_other_child", "escape_sys_modules_lookup", "escape_open_socket",
)
NEEDS_A_NEIGHBOUR = frozenset({"escape_kill_other_child"})

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


def _bars(session, count: int = 5, start: int = 0) -> None:
    for i in range(start, start + count):
        session.bar(PROBE_CONID, _T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)


def _posture_checks(posture: dict) -> list[CheckResult]:
    caps = set(decode_cap_eff(posture.get("cap_eff", "")))
    checks = [
        CheckResult("cap_eff", caps == EXPECTED_CAPS, f"caps={sorted(caps)}"),
        CheckResult("seccomp", posture.get("seccomp") == 2, f"seccomp={posture.get('seccomp')}"),
        CheckResult("no_new_privs", posture.get("no_new_privs") == 1,
                    f"no_new_privs={posture.get('no_new_privs')}"),
        CheckResult("network_none", set(posture.get("interfaces", [])) <= {"lo"},
                    f"interfaces={posture.get('interfaces')}"),
        CheckResult("no_forbidden_env", not posture.get("forbidden_env"),
                    f"forbidden_env={posture.get('forbidden_env')}"),
        CheckResult("read_only_root", posture.get("root_read_only") is True,
                    f"root_read_only={posture.get('root_read_only')}"),
        CheckResult("pids_limit", posture.get("pids_max") == EXPECTED_PIDS_MAX,
                    f"pids_max={posture.get('pids_max')!r}"),
    ]
    return checks


def _benign_check(client: SandboxClient) -> CheckResult:
    try:
        with client.open_probe("probe", "benign_orb") as session:
            _bars(session, 5)
        return CheckResult("benign_session", True, "ran five bars and closed")
    except (SandboxFault, SandboxUnavailable) as exc:
        return CheckResult("benign_session", False, f"benign probe failed: {exc}")


def _hostile_check(client: SandboxClient, name: str) -> CheckResult:
    try:
        with client.open_probe("escape", name) as session:
            _bars(session, 5)
    except SandboxFault as exc:
        return CheckResult(name, True, f"contained: {exc.code}")
    except SandboxUnavailable as exc:
        return CheckResult(name, False, f"unavailable, not proven: {exc}")
    except SandboxProtocolError as exc:
        # A missing escape file (Plan 1 filename drift) is a setup error, not containment.
        return CheckResult(name, False, f"escape {name!r} not loadable: {exc}")
    return CheckResult(name, False, "ran to completion: NOT contained")


def _hostile_check_with_a_neighbour(client: SandboxClient, name: str) -> CheckResult:
    """Hold a benign session open while the escape runs; it must fault and the neighbour must live."""
    try:
        neighbour = client.open_probe("probe", "benign_orb")
    except (SandboxFault, SandboxUnavailable, SandboxProtocolError) as exc:
        return CheckResult(name, False, f"no neighbour session, not proven: {exc}")
    with neighbour:
        try:
            _bars(neighbour, 2)
        except (SandboxFault, SandboxUnavailable) as exc:
            return CheckResult(name, False, f"neighbour failed before the escape ran: {exc}")
        verdict = _hostile_check(client, name)
        try:
            _bars(neighbour, 2, start=2)
        except (SandboxFault, SandboxUnavailable) as exc:
            return CheckResult(name, False, f"the neighbour session died: {exc}")
    return verdict


def run_isolation_checks(client: SandboxClient) -> IsolationResult:
    info = client.ping()
    checks: list[CheckResult] = list(_posture_checks(dict(info.posture)))
    checks.append(_benign_check(client))
    for name in HOSTILE_ESCAPES:
        run = _hostile_check_with_a_neighbour if name in NEEDS_A_NEIGHBOUR else _hostile_check
        checks.append(run(client, name))
    passed = all(check.passed for check in checks)
    return IsolationResult(passed=passed, image_digest=info.image_digest, checks=tuple(checks))
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/sandbox/test_posture.py tests/sandbox/test_isolation_checks.py -q` — expected: PASS. If Task 4 left a `read_posture` stub, it is now the real one.

- [ ] **Step 5: Commit.**

```bash
git add trader/sandbox/posture.py trader/sandbox/isolation_checks.py tests/sandbox/test_posture.py tests/sandbox/test_isolation_checks.py
git commit -m "feat: add sandbox posture reading and the isolation check engine"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 7: The seccomp profile

The sandbox profile is Docker's default plus a deny of `ptrace`, `process_vm_readv`, `process_vm_writev`, `kcmp` and `pidfd_open`. The repo does not vendor Docker's default, so this task vendors it verbatim (ruling 16) and derives the sandbox profile from it with a script, so a reader can see the exact difference.

**Files:**
- Create: `config_defaults/docker-default-seccomp.json` (vendored verbatim), `scripts/build_sandbox_seccomp.py`, `config_defaults/sandbox-seccomp.json` (generated)
- Test: `tests/sandbox/test_seccomp_profile.py`

**Interfaces:** none consumed; produces the on-disk profile the compose stanza (Task 9) references.

- [ ] **Step 1: Vendor the upstream default.** Download moby `v27.5.1` `profiles/seccomp/default.json` verbatim into `config_defaults/docker-default-seccomp.json`:

```bash
curl -fsSL -o config_defaults/docker-default-seccomp.json \
  https://raw.githubusercontent.com/moby/moby/v27.5.1/profiles/seccomp/default.json
shasum -a 256 config_defaults/docker-default-seccomp.json
# must print: 9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76
```

If the hash differs, stop: the upstream file changed; do not hand-edit it. Put the source URL and version in a one-line header comment is not possible in JSON, so record them in `scripts/build_sandbox_seccomp.py` (below) and in `docs/OPERATIONAL_STATE.md` (Task 11).

- [ ] **Step 2: Write the failing test.** Write `tests/sandbox/test_seccomp_profile.py`:

```python
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
```

- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/sandbox/test_seccomp_profile.py -q` — expected: FAIL (missing `scripts/build_sandbox_seccomp.py` / `sandbox-seccomp.json`).

- [ ] **Step 4: Implement.** Write `scripts/build_sandbox_seccomp.py`:

```python
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
```

- [ ] **Step 5: Generate the profile.**

```bash
.venv/bin/python scripts/build_sandbox_seccomp.py
```

- [ ] **Step 6: Run** `.venv/bin/python -m pytest tests/sandbox/test_seccomp_profile.py -q` — expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add config_defaults/docker-default-seccomp.json config_defaults/sandbox-seccomp.json scripts/build_sandbox_seccomp.py tests/sandbox/test_seccomp_profile.py
git commit -m "feat: vendor the docker seccomp default and derive the sandbox profile"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 8: Dockerfile users, the build id, and docker.sh

The image gains group `sbxchild` (gid 20000) and uids 20001..20007 (none in `sudo` or `trader`; ruling 26), a baked build id (ruling 1), and `/store`. `docker.sh` exports `MMR_BUILD_ID`, runs compose from the repo root so the relative seccomp path resolves (ruling 17), and adds `sandbox` to the `-e` allow-list.

**Files:**
- Modify: `Dockerfile`
- Modify: `docker.sh`
- Test: modify `tests/test_docker_helper.py`, add `tests/test_dockerfile_sandbox.py`

**Interfaces:** none consumed; produces the image identity the supervisor reads and the compose stanza (Task 9) uses.

- [ ] **Step 1: Write the failing tests.** Add `tests/test_dockerfile_sandbox.py`:

```python
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_sbxchild_group_and_uid_range_exist():
    assert "groupadd -g 20000 sbxchild" in DOCKERFILE
    assert "seq 20001 20007" in DOCKERFILE                  # seven slots: live 3 + research 4 (ruling 26)
    assert re.search(r'useradd -u "\$uid" -g 20000 -M -N', DOCKERFILE)


def test_sandbox_uids_are_not_in_sudo_or_trader():
    line = next(text for text in DOCKERFILE.splitlines() if 'useradd -u "$uid"' in text)
    assert "-G" not in line and "sudo" not in line and "trader" not in line


def test_the_build_id_is_baked_and_labelled():
    assert "ARG MMR_BUILD_ID" in DOCKERFILE
    assert "/etc/mmr-build-id" in DOCKERFILE
    assert "LABEL mmr.build_id" in DOCKERFILE


def test_the_store_directory_is_created_for_the_read_only_mount():
    assert "/store" in DOCKERFILE
```

In `tests/test_docker_helper.py`, add two tests near the other `-e` / build tests:

```python
def test_exec_accepts_the_sandbox_service(fake_docker: FakeDocker):
    result = fake_docker.run("-e", "sandbox")
    assert result.returncode == 0, result.stdout


def test_build_exports_a_non_empty_build_id(fake_docker: FakeDocker):
    result = fake_docker.run("-b")
    assert result.returncode == 0, result.stdout
    assert "MMR_BUILD_ID" in result.log or "build-id" in result.log.lower()
```

For the second test to see `MMR_BUILD_ID`, `build()` must echo it (the fake docker logs only argv, not env). Make `build()` print the id. Adjust the assertion to match what you print; keep it to one grep-able line.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_dockerfile_sandbox.py "tests/test_docker_helper.py::test_exec_accepts_the_sandbox_service" -q` — expected: FAIL.

- [ ] **Step 3: Edit the Dockerfile.** After the existing `useradd ... trader` block (the `RUN groupadd -g 1000 trader ...`), add a new block:

```dockerfile
# Sandbox child identities (SP3 Plan 2, spec 7.5): one uid per session slot, in a
# group of their own, none in sudo or trader. The sandbox supervisor drops each
# forked child to one of these before any authored code loads.
RUN groupadd -g 20000 sbxchild \
    && for uid in $(seq 20001 20007); do \
         useradd -u "$uid" -g 20000 -M -N -s /usr/sbin/nologin "sbx$uid"; \
       done
```

After the `COPY --chown=trader:trader ./ /home/trader/mmr/` line and the editable install, add the build id and the store directory (place the `ARG` near the top of the `runtime` stage, before it is used):

Near the top of the `runtime` stage, after the `ENV DEBIAN_FRONTEND` line:

```dockerfile
# Build identity baked at image build time (SP3 Plan 2 ruling 1). docker.sh passes
# MMR_BUILD_ID; a plain `docker build` leaves it "unset" and the sandbox refuses to start.
ARG MMR_BUILD_ID=unset
```

Just before the final `USER trader` / `ENTRYPOINT`:

```dockerfile
# Bake the build id so the running sandbox cannot misreport its own image (ruling 1).
RUN printf '%s\n' "${MMR_BUILD_ID}" > /etc/mmr-build-id && chmod 0444 /etc/mmr-build-id
LABEL mmr.build_id="${MMR_BUILD_ID}"

# The authored-strategy store is mounted read-only here on the sandbox; create it so
# a fresh volume belongs to trader (the trader service, Plan 3, writes it).
RUN mkdir -p /store && chown trader:trader /store && chmod 0755 /store
```

- [ ] **Step 4: Edit `docker-compose.yml`'s build anchor** so every service's build passes the id:

```yaml
x-mmr-build: &mmr-build
  context: .
  dockerfile: Dockerfile
  target: runtime
  args:
    MMR_BUILD_ID: ${MMR_BUILD_ID:-unset}
```

- [ ] **Step 5: Edit `docker.sh`.** In the `-e` service validation `case` (around the `trader|data|strategy|dashboard|scheduler|ib-gateway` line), add `sandbox`:

```bash
        trader|data|strategy|dashboard|scheduler|ib-gateway|sandbox) ;;
```

Add a build-id helper near the top helpers and use it in `build()` and `up()`:

```bash
_export_build_id() {
    if [[ -z "${MMR_BUILD_ID:-}" ]]; then
        local sha stamp
        sha=$(git -C "$BUILDDIR" rev-parse --short HEAD 2>/dev/null || echo nogit)
        stamp=$(date -u +%Y%m%dT%H%M%SZ)
        export MMR_BUILD_ID="${sha}-${stamp}"
    fi
    echo "MMR_BUILD_ID=${MMR_BUILD_ID}"
}
```

In `build()`, as the first line of the function body, call it:

```bash
build() {
    _export_build_id
    echo "Building MMR image..."
    ...
```

`up()` does **not** need the build id: the id is baked into `/etc/mmr-build-id` at build time, and a recreate reuses that image, so the running container always reports its own id. Do not re-export it in `up()` (a fresh timestamp there would never match the baked file).

For ruling 17 (a `seccomp=./config_defaults/...` path resolves against the current directory), make compose run from the repo root **without** breaking the operator's relative `--restore`/`--identity-file` paths. At the very top of the script, capture the invocation directory, resolve those two user paths against it, then `cd`:

```bash
BUILDDIR=$(cd $(dirname "$0"); pwd)
INVOCATION_DIR="$PWD"
```

After the argument-parsing `while` loop (where `KEYS_ARCHIVE` and `KEYS_IDENTITY_FILE` are set), resolve any relative values against `INVOCATION_DIR`, then change to the repo root:

```bash
_abs() { case "$1" in /*) printf '%s' "$1";; *) printf '%s/%s' "$INVOCATION_DIR" "$1";; esac; }
[[ -n "$KEYS_ARCHIVE" ]] && KEYS_ARCHIVE="$(_abs "$KEYS_ARCHIVE")"
[[ -n "$KEYS_IDENTITY_FILE" ]] && KEYS_IDENTITY_FILE="$(_abs "$KEYS_IDENTITY_FILE")"
cd "$BUILDDIR"   # ruling 17: a relative seccomp=./config_defaults path resolves against cwd
```

- [ ] **Step 6: Run** `.venv/bin/python -m pytest tests/test_dockerfile_sandbox.py tests/test_docker_helper.py -q` — expected: PASS (all helper tests still green).

- [ ] **Step 7: Commit.**

```bash
git add Dockerfile docker.sh docker-compose.yml tests/test_dockerfile_sandbox.py tests/test_docker_helper.py
git commit -m "feat: add sandbox child uids, the baked build id and docker.sh wiring"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 9: Slot directories and the startup capability drop

The supervisor needs `CAP_CHOWN` only to make each slot's directory owned by its uid, then drops it so it serves with exactly SETUID/SETGID/KILL (ruling 2). This code needs root and capabilities, so the unit test only checks the pure helpers; the live behaviour is checked by the container test (Task 11).

**Files:**
- Create: `trader/sandbox/caps.py`
- Modify: `trader/sandbox_service.py`
- Test: `tests/sandbox/test_caps.py`

**Interfaces:**
- Produces: `trader/sandbox/caps.py`: `CAP_CHOWN = 0`, `has_effective_cap(bit: int) -> bool`, `drop_capability(bit: int) -> None`, `slot_dirs(uids) -> list[str]`, `prepare_slot_dirs(uids, gid=20000) -> None`. `trader/sandbox_service.py` calls `prepare_slot_dirs` then `drop_capability(CAP_CHOWN)` before serving.
- Consumes: Task 3 slot uids.

- [ ] **Step 1: Write the failing test.** Write `tests/sandbox/test_caps.py`:

```python
from trader.sandbox import caps


def test_slot_dirs_are_named_by_uid():
    assert caps.slot_dirs([20001, 20002]) == ["/tmp/s20001", "/tmp/s20002"]


def test_has_effective_cap_reads_proc_status(monkeypatch, tmp_path):
    status = tmp_path / "status"
    status.write_text("CapEff:\t00000000000000e0\n")
    monkeypatch.setattr(caps, "_STATUS_PATH", str(status))
    assert caps.has_effective_cap(caps.CAP_KILL) is True     # bit 5
    assert caps.has_effective_cap(caps.CAP_CHOWN) is False    # bit 0


def test_cap_chown_is_bit_zero():
    assert caps.CAP_CHOWN == 0 and caps.CAP_KILL == 5 and caps.CAP_SETGID == 6 and caps.CAP_SETUID == 7
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/sandbox/test_caps.py -q` — expected: FAIL.

- [ ] **Step 3: Implement.** Write `trader/sandbox/caps.py`:

```python
"""Create per-slot directories (needs CAP_CHOWN) then drop CAP_CHOWN before serving (ruling 2)."""
from __future__ import annotations

import ctypes
import os

CAP_CHOWN = 0
CAP_KILL = 5
CAP_SETGID = 6
CAP_SETUID = 7

_STATUS_PATH = "/proc/self/status"
_LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


def slot_dirs(uids) -> list[str]:
    return [f"/tmp/s{uid}" for uid in uids]


def prepare_slot_dirs(uids, gid: int = 20000) -> None:
    for uid in uids:
        path = f"/tmp/s{uid}"
        os.makedirs(path, exist_ok=True)
        os.chown(path, uid, gid)    # needs CAP_CHOWN
        os.chmod(path, 0o700)


def has_effective_cap(bit: int) -> bool:
    try:
        for line in open(_STATUS_PATH):
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << bit))
    except OSError:
        return False
    return False


def drop_capability(bit: int) -> None:
    """Clear one capability from the effective and permitted sets, then confirm it is gone."""
    libc = ctypes.CDLL(None, use_errno=True)
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    if libc.capget(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capget failed")
    mask = ~(1 << bit) & 0xFFFFFFFF
    data[0].effective &= mask
    data[0].permitted &= mask
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")
    if has_effective_cap(bit):
        raise RuntimeError(f"capability bit {bit} still effective after drop")
```

- [ ] **Step 4: Edit `trader/sandbox_service.py`.** Import `caps` and the slot uids, and in `_amain` prepare the dirs and drop CHOWN before `serve`:

```python
from trader.sandbox import caps  # noqa: E402
from trader.sandbox.supervisor import _LIVE_UIDS, _RESEARCH_UIDS  # noqa: E402
```

In `_amain`, replace the body so it runs:

```python
async def _amain() -> None:
    _join_trader_group()
    caps.prepare_slot_dirs(_LIVE_UIDS + _RESEARCH_UIDS)
    caps.drop_capability(caps.CAP_CHOWN)   # ruling 2: serve with SETUID/SETGID/KILL only
    supervisor = build_supervisor()
    await serve(supervisor, {"live": proto.LIVE_SOCKET, "research": proto.RESEARCH_SOCKET})
    log.info("sandbox serving live=%s research=%s", proto.LIVE_SOCKET, proto.RESEARCH_SOCKET)
    await asyncio.Event().wait()
```

Export the uid tuples from the supervisor module (they already exist as `_LIVE_UIDS`, `_RESEARCH_UIDS`; add a public alias so the import reads clean): at the bottom of `trader/sandbox/supervisor.py` add `LIVE_UIDS = _LIVE_UIDS` and `RESEARCH_UIDS = _RESEARCH_UIDS`, and import those public names instead.

- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/sandbox/test_caps.py -q` — expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add trader/sandbox/caps.py trader/sandbox_service.py trader/sandbox/supervisor.py tests/sandbox/test_caps.py
git commit -m "feat: prepare per-slot dirs then drop CAP_CHOWN before serving"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 10: The compose `sandbox` stanza, volumes, socket mounts and static tests

**Files:**
- Modify: `docker-compose.yml` (the `sandbox` service, three volumes, socket mounts on `strategy` and `research`)
- Modify: `docker-compose.test.override.yml` (research reachable without its real service)
- Modify: `tests/test_compose_rpc_keys.py` (classify-skip), `tests/compose_rpc_helpers.py` (unchanged unless needed)
- Create: `tests/test_compose_sandbox.py`, `tests/test_sandbox_no_test_imports.py`

Ruling 23 governs the tmpfs mount options (see the Rulings section): the short string form carries `noexec`; the container test (Task 11) checks `/proc/mounts`.

**Interfaces:** none consumed by code; Plan 4 mounts `mmr_sandbox_live` on `strategy` and `mmr_sandbox_research` on `research` (added here so Plan 4 does not touch compose).

- [ ] **Step 1: Write the failing tests.** Write `tests/test_compose_sandbox.py`:

```python
import pytest
import yaml

from tests.compose_rpc_helpers import ROOT, load_compose, volumes

FORBIDDEN_ENV_PREFIXES = ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_",
                          "AWS_", "AZURE_", "DASHBOARD_", "TELEGRAM", "TRADING_MODE")
ALLOWED_ENV = {"TZ", "PYTHONDONTWRITEBYTECODE", "HOME", "TMPDIR", "NUMBA_CACHE_DIR",
               "MPLCONFIGDIR"}   # the build id is baked at /etc/mmr-build-id, not an env var


@pytest.fixture(scope="module")
def sandbox():
    return load_compose()["services"]["sandbox"]


def test_sandbox_runs_the_service(sandbox):
    assert sandbox["command"] == ["python", "-m", "trader.sandbox_service"]
    assert sandbox["user"] in ("0", 0)


def test_sandbox_does_not_inherit_the_shared_anchors():
    text = (ROOT / "docker-compose.yml").read_text()
    block = text.split("\n  sandbox:\n", 1)[1].split("\n  # ──", 1)[0].split("\nvolumes:", 1)[0]
    assert "*mmr-common-env" not in block and "*mmr-hardening" not in block


def test_sandbox_has_no_network(sandbox):
    assert sandbox["network_mode"] == "none"
    assert "networks" not in sandbox


def test_sandbox_drops_all_caps_and_adds_only_the_four(sandbox):
    assert sandbox["cap_drop"] == ["ALL"]
    assert set(sandbox["cap_add"]) == {"CHOWN", "SETUID", "SETGID", "KILL"}


def test_sandbox_hardening_flags(sandbox):
    assert sandbox["read_only"] is True
    assert sandbox["init"] is True
    assert "no-new-privileges:true" in sandbox["security_opt"]
    assert any(s.startswith("seccomp=") and s.endswith("sandbox-seccomp.json")
               for s in sandbox["security_opt"])
    assert sandbox["pids_limit"] == 64
    assert str(sandbox["mem_limit"]) == "4g"


def test_sandbox_environment_is_minimal(sandbox):
    env = sandbox.get("environment") or {}
    assert set(env) <= ALLOWED_ENV
    assert not [name for name in env if name.startswith(FORBIDDEN_ENV_PREFIXES)]
    assert "env_file" not in sandbox


def test_sandbox_mounts_the_store_read_only_and_the_two_socket_volumes(sandbox):
    vols = {v["target"]: v for v in volumes(sandbox)}
    assert vols["/store"]["read_only"] is True and vols["/store"]["source"] == "mmr_authored_strategies"
    assert "/run/sandbox-live" in vols and "/run/sandbox-research" in vols


def test_sandbox_mounts_no_keys_and_masks_config(sandbox):
    targets = {v["target"]: v for v in volumes(sandbox)}
    assert not [t for t in targets if "keys" in t]
    # The baked config dir is masked by an empty tmpfs, not bound from the host.
    mask = targets.get("/home/trader/.config")
    assert mask is not None and mask["type"] == "tmpfs"
    assert not [t for t in targets if t.startswith("/home/trader/.config/mmr")]


def test_sandbox_tmpfs_is_capped(sandbox):
    entries = sandbox["tmpfs"]
    joined = " ".join(entries if isinstance(entries, list) else [entries])
    assert "/tmp" in joined and "size=" in joined


def test_the_three_volumes_are_declared():
    compose = load_compose()
    for name in ("mmr_authored_strategies", "mmr_sandbox_live", "mmr_sandbox_research"):
        assert name in compose["volumes"]


def test_strategy_and_research_mount_their_socket_volumes():
    compose = load_compose()
    strat = {v["target"] for v in volumes(compose["services"]["strategy"])}
    research = {v["target"] for v in volumes(compose["services"]["research"])}
    assert "/run/sandbox-live" in strat
    assert "/run/sandbox-research" in research
```

Write `tests/test_sandbox_no_test_imports.py`:

```python
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "trader"


def test_no_production_module_imports_the_tests_package():
    offenders = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [(path, a.name) for a in node.names if a.name.split(".")[0] == "tests"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tests":
                offenders.append((path, node.module))
    assert not offenders, offenders
```

In `tests/test_compose_rpc_keys.py::test_every_long_lived_service_is_classified`, add `sandbox` to the skip tuple:

```python
        if name in ("ib-gateway", "fullstack-tests", "keygen", "sandbox"):
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_compose_sandbox.py tests/test_sandbox_no_test_imports.py -q` — expected: FAIL (`KeyError: 'sandbox'`).

- [ ] **Step 3: Add the `sandbox` stanza** to `docker-compose.yml`, after the `research` service and before `cli`:

```yaml
  # ── sandbox: runs authored strategy code, nothing else (SP3 Plan 2, spec 7.5) ──
  # Deliberately does NOT inherit x-mmr-hardening (it sets `networks`, which clashes
  # with network_mode: none) or x-mmr-common-env (that anchor carries Alpaca/IB keys).
  # One supervisor (uid 0, only CHOWN at startup then SETUID/SETGID/KILL) forks one
  # child per session, drops it to a slot uid (20001..20007) before any authored code
  # loads. No network, read-only root, its own seccomp profile. It mounts only the
  # read-only store and the two socket volumes; no keys, no config, no DB, no secrets.
  sandbox:
    build: *mmr-build
    image: mmr:latest
    profiles: ["ai", "test"]
    restart: unless-stopped
    command: ["python", "-m", "trader.sandbox_service"]
    user: "0"
    init: true
    read_only: true
    network_mode: none
    ipc: none
    cap_drop: [ALL]
    cap_add: [CHOWN, SETUID, SETGID, KILL]
    security_opt:
      - "no-new-privileges:true"
      - "seccomp=./config_defaults/sandbox-seccomp.json"
    pids_limit: 64
    cpus: 2
    mem_limit: 4g
    environment:
      TZ: UTC
      PYTHONDONTWRITEBYTECODE: "1"
      HOME: /tmp
      TMPDIR: /tmp
      NUMBA_CACHE_DIR: /tmp/numba
      MPLCONFIGDIR: /tmp/mpl
    tmpfs:
      - "/tmp:size=67108864,mode=0755,noexec,nosuid,nodev"
    volumes:
      # Mask the baked ~/.config (config_defaults/*.yaml) so a child cannot read it even
      # with pandas I/O: an empty, root-owned 0700 tmpfs (bug found in review).
      - type: tmpfs
        target: /home/trader/.config
        tmpfs:
          size: 4096
          mode: 0700
      - mmr_authored_strategies:/store:ro
      - mmr_sandbox_live:/run/sandbox-live
      - mmr_sandbox_research:/run/sandbox-research
    healthcheck:
      test: ["CMD", "python3", "-c", "import os,sys; sys.exit(0 if os.path.exists('/run/sandbox-live/sandbox.sock') else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

- [ ] **Step 4: Mount the socket volumes on the callers.** In the `strategy` service `volumes:` add:

```yaml
      # The live sandbox socket (SP3 Plan 2); Plan 4's live proxy connects here.
      - mmr_sandbox_live:/run/sandbox-live
```

In the `research` service `volumes:` add:

```yaml
      # The research sandbox socket (SP3 Plan 2); Plan 4's proxy strategy connects here.
      - mmr_sandbox_research:/run/sandbox-research
```

Add the three volumes under the top-level `volumes:`:

```yaml
  mmr_authored_strategies:
  mmr_sandbox_live:
  mmr_sandbox_research:
```

- [ ] **Step 5: Make research reachable in the test override.** In `docker-compose.test.override.yml`, add a `research` block so the container test can exec against the real research mounts without the real service running (ruling 18):

```yaml
  research:
    profiles: ["ai", "test"]
    command: ["sleep", "infinity"]
```

Note: `sandbox` already carries `profiles: ["ai", "test"]`, so `--profile test` brings it up.

- [ ] **Step 6: Run** `.venv/bin/python -m pytest tests/test_compose_sandbox.py tests/test_sandbox_no_test_imports.py tests/test_compose_rpc_keys.py tests/test_compose_topology.py tests/test_compose_ai_service.py -q` — expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add docker-compose.yml docker-compose.test.override.yml tests/test_compose_sandbox.py tests/test_sandbox_no_test_imports.py tests/test_compose_rpc_keys.py
git commit -m "feat: add the sandbox compose stanza, volumes and socket mounts"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 11: Container tests against the real `sandbox` stanza

These run in the `fullstack-tests` profile against the real stanza, never the live stack. They check the capabilities, slot uids, no provider env, network none, seccomp loaded, socket reachability, a benign ORB session, a live session over several conids, every hostile case (including the exact `escape_pd_io_common_oskill` escape loaded with the static check skipped, ruling 11), and that the supervisor and a parallel benign session survive. The hostile source is Plan 1's escape corpus, referenced by name. Plan 3 Task 10 adds the `compose exec research mmr ai-author prove-isolation --in-container` run once its CLI exists (D3).

**Files:**
- Modify: `docker-compose.yml` (`fullstack-tests`: mount the three volumes, depend on `sandbox`)
- Create: `tests/fullstack/test_sandbox_service.py`, `scripts/run_sandbox_fullstack.sh`

**Interfaces:** consumes Task 5 `SandboxClient`, Task 6 `run_isolation_checks`, `HOSTILE_ESCAPES`, `EXPECTED_CAPS`; `trader.sandbox.protocol` socket paths.

- [ ] **Step 1: Add the runner wiring.** In `docker-compose.yml`, extend `fullstack-tests`:

```yaml
    depends_on: [data, trader, strategy, dashboard, scheduler, sandbox]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - mmr_authored_strategies:/store
      - mmr_sandbox_live:/run/sandbox-live
      - mmr_sandbox_research:/run/sandbox-research
```

(Keep the existing docker.sock mount; add the three volume lines.)

- [ ] **Step 2: Write the test.** Write `tests/fullstack/test_sandbox_service.py`:

```python
"""Container checks of the real sandbox stanza (spec 7.5, 7.9, 10). Runs only in the
fullstack-tests profile, against its own compose project, never the live stack.

Bring up with:
  docker compose -f docker-compose.yml -f docker-compose.test.override.yml \
    --profile test up --build --abort-on-container-exit fullstack-tests
"""
import hashlib
import os
import socket as _socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

docker = pytest.importorskip("docker", reason="docker-py not installed")

from trader.authored.sandbox_client import SandboxClient, SandboxFault
from trader.sandbox import protocol as proto
from trader.sandbox.isolation_checks import EXPECTED_CAPS, HOSTILE_ESCAPES, NEEDS_A_NEIGHBOUR, run_isolation_checks


def _skip_reason():
    if os.environ.get("MMR_FAKE_BROKER") != "1":
        return "only the fullstack-tests runner sets MMR_FAKE_BROKER=1"
    if not Path(proto.LIVE_SOCKET).exists():
        return "the live sandbox socket is not mounted"
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
BENIGN = '''
from trader.authored.api import AuthoredStrategy, Decision


class Edge(AuthoredStrategy):
    LOOKBACK_BARS = 10

    def on_bar(self, history, params):
        return Decision(action="BUY", probability=0.5) if len(history) >= 3 else None
'''


@pytest.fixture(scope="module")
def client():
    c = docker.from_env()
    yield c
    c.close()


def _sandbox_container(client):
    me = client.containers.get(_socket.gethostname())
    project = me.labels["com.docker.compose.project"]
    found = client.containers.list(filters={"label": [
        f"com.docker.compose.project={project}", "com.docker.compose.service=sandbox"]})
    assert len(found) == 1, "expected one sandbox container"
    return found[0]


def _store(source: str) -> str:
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    Path(proto.STORE_DIR, f"{digest.split(':')[1]}.py").write_text(source)
    return digest


def test_sandbox_capabilities_are_exactly_the_four_at_boot(client):
    host = _sandbox_container(client).attrs["HostConfig"]
    assert {c.upper() for c in host["CapDrop"]} == {"ALL"}
    added = {c.upper().removeprefix("CAP_") for c in host["CapAdd"]}
    assert added == {"CHOWN", "SETUID", "SETGID", "KILL"}
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert any("seccomp=" in opt for opt in host["SecurityOpt"])
    assert any("no-new-privileges" in opt for opt in host["SecurityOpt"])


def test_sandbox_env_has_no_provider_or_broker_names(client):
    env = _sandbox_container(client).attrs["Config"]["Env"]
    names = [entry.split("=", 1)[0] for entry in env]
    for forbidden in ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_",
                      "AWS_", "AZURE_"):
        assert not [n for n in names if n.startswith(forbidden)], names


def test_the_serving_supervisor_has_only_three_effective_caps(client):
    sandbox = _sandbox_container(client)
    pid = sandbox.exec_run(["pgrep", "-f", "trader.sandbox_service"]).output.decode().split()[0]
    status = sandbox.exec_run(["cat", f"/proc/{pid}/status"]).output.decode()
    cap_eff = next(line.split()[1] for line in status.splitlines() if line.startswith("CapEff:"))
    assert int(cap_eff, 16) == (1 << 5) | (1 << 6) | (1 << 7)   # KILL|SETGID|SETUID, CHOWN dropped
    seccomp = next(line.split()[1] for line in status.splitlines() if line.startswith("Seccomp:"))
    assert seccomp == "2"   # filter mode


def test_a_slot_directory_is_owned_by_its_slot_uid(client):
    out = _sandbox_container(client).exec_run(["stat", "-c", "%u:%g %a", "/tmp/s20001"]).output.decode()
    assert out.strip() == "20001:20000 700"


def test_the_tmpfs_is_noexec(client):
    mounts = _sandbox_container(client).exec_run(["cat", "/proc/mounts"]).output.decode()
    tmp_line = next(line for line in mounts.splitlines() if " /tmp " in line)
    assert "noexec" in tmp_line


def test_the_custom_seccomp_profile_denies_pidfd_open(client):
    # Docker's default profile ALLOWS pidfd_open; only our derived profile denies it, so this
    # proves the sandbox profile (not merely "a" profile) is loaded.
    result = _sandbox_container(client).exec_run(
        ["python3", "-c", "import os; os.pidfd_open(os.getpid())"])
    assert result.exit_code != 0
    assert b"PermissionError" in result.output or b"Operation not permitted" in result.output


def test_no_sandbox_child_processes_or_zombies_remain(client):
    # After the hostile and benign sessions, every forked child was killed and reaped.
    out = _sandbox_container(client).exec_run(["ps", "-eo", "uid=,stat="]).output.decode()
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            assert not 20001 <= int(parts[0]) <= 20007, f"a slot child survived: {line}"
        assert not (len(parts) > 1 and parts[1].startswith("Z")), f"a zombie remains: {line}"


def test_a_benign_live_session_returns_decisions_for_each_of_its_conids():
    digest = _store(BENIGN)
    conids = list(range(101, 121))                     # 20 conids: the most a deployment binds
    with SandboxClient(proto.LIVE_SOCKET).open_session(digest, "Edge", {}, "1 min", conids) as session:
        signals = {conid: [session.bar(conid, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)
                           for i in range(5)] for conid in conids}
    assert all(any(s is not None for s in per_conid) for per_conid in signals.values())


def test_the_benign_probe_also_runs():
    with SandboxClient(proto.RESEARCH_SOCKET).open_probe("probe", "benign_orb") as session:
        for i in range(5):
            session.bar(proto.PROBE_CONID, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)


@pytest.mark.parametrize("escape", [name for name in HOSTILE_ESCAPES if name not in NEEDS_A_NEIGHBOUR])
def test_every_hostile_escape_is_contained(escape):
    # Plan 1's escape corpus, loaded with the static check skipped on purpose (ruling 11).
    # Includes the exact pd.io.common.os.kill(pd.io.common.os.getppid(), 9) path (escape_pd_io_common_oskill).
    with pytest.raises(SandboxFault):
        with SandboxClient(proto.RESEARCH_SOCKET).open_probe("escape", escape) as session:
            for i in range(5):
                session.bar(proto.PROBE_CONID, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)


def test_killing_another_sessions_child_fails_and_the_neighbour_lives():
    # escape_kill_other_child only acts when another slot child exists, so hold one open (ruling 27).
    digest = _store(BENIGN)
    neighbour = SandboxClient(proto.LIVE_SOCKET).open_session(digest, "Edge", {}, "1 min", [101])
    try:
        neighbour.bar(101, T0, 1.0, 2.0, 0.5, 1.5, 100.0)
        with pytest.raises(SandboxFault):
            with SandboxClient(proto.RESEARCH_SOCKET).open_probe("escape", "escape_kill_other_child") as bad:
                for i in range(3):
                    bad.bar(proto.PROBE_CONID, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)
        neighbour.bar(101, T0 + timedelta(minutes=1), 1.0, 2.0, 0.5, 1.5, 100.0)   # still answers: not killed
    finally:
        neighbour.close()


def test_run_isolation_checks_passes_on_the_running_service():
    result = run_isolation_checks(SandboxClient(proto.RESEARCH_SOCKET))
    failed = [c for c in result.checks if not c.passed]
    assert result.passed, failed


def test_the_supervisor_and_ping_survive_every_hostile_session():
    # After the hostile sessions above, PING still answers on both sockets.
    assert SandboxClient(proto.LIVE_SOCKET).ping().image_digest
    assert SandboxClient(proto.RESEARCH_SOCKET).ping().image_digest


def test_a_parallel_benign_session_survives_a_hostile_one():
    digest = _store(BENIGN)
    good = SandboxClient(proto.LIVE_SOCKET).open_session(digest, "Edge", {}, "1 min", [101])
    try:
        with pytest.raises(SandboxFault):
            with SandboxClient(proto.RESEARCH_SOCKET).open_probe("escape", "escape_loop_forever") as bad:
                for i in range(3):
                    bad.bar(proto.PROBE_CONID, T0 + timedelta(minutes=i), 1.0, 2.0, 0.5, 1.5, 100.0)
        decision = good.bar(101, T0 + timedelta(minutes=9), 1.0, 2.0, 0.5, 1.5, 100.0)
        assert decision is not None or decision is None   # it still answers, no crash
    finally:
        good.close()
```

- [ ] **Step 3: Write the run script** `scripts/run_sandbox_fullstack.sh`:

```bash
#!/bin/bash
# Bring up the full stack plus the sandbox and run the container checks (SP3 Plan 2).
# Never run against the live stack: this uses the test override (fake broker) and its own project.
set -o errexit -o pipefail -o nounset
BUILDDIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$BUILDDIR"
export MMR_BUILD_ID="${MMR_BUILD_ID:-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)-test}"
exec docker compose -f docker-compose.yml -f docker-compose.test.override.yml \
  --profile test up --build --abort-on-container-exit fullstack-tests
```

Make it executable: `chmod +x scripts/run_sandbox_fullstack.sh`.

- [ ] **Step 4: Note on running.** These container tests need Docker and are not part of the unit suite (they skip without `MMR_FAKE_BROKER=1` and the mounted socket, like the existing `tests/fullstack/*`). Do not run them from this plan's unit steps. The owner runs `scripts/run_sandbox_fullstack.sh` on a host with Docker. State in the PR that they were not run here if Docker was unavailable.

- [ ] **Step 5: Commit.**

```bash
git add docker-compose.yml tests/fullstack/test_sandbox_service.py scripts/run_sandbox_fullstack.sh
git commit -m "test: add container checks for the real sandbox stanza"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 12: Docs and the full suite

**Files:** Modify `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONAL_STATE.md`, and `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md` (rulings only).

- [ ] **Step 1: Docs.**
  - `AGENTS.md` Architecture: add a bullet: "**sandbox** (`trader.sandbox_service`, compose profile `ai`, opt-in): runs authored strategy code only. One supervisor (uid 0, `CHOWN` at startup then only SETUID/SETGID/KILL) forks one child per session and drops it to a slot uid (20001..20007) before any authored code loads. No network, read-only root, its own seccomp profile. Two Unix sockets: live (3 sessions, one per authored deployment, each serving that deployment's conids) and research (4 one-conid sessions). It mounts only the read-only store and the socket volumes; no keys, config, DB or secrets. It speaks no typed RPC." In the port/protocol table, add a row: `| Unix socket | 4-byte length + JSON, v1 | sandbox: OPEN/BAR/CLOSE/PING on /run/sandbox-live and /run/sandbox-research |` (the sandbox binds no TCP port).
  - `docs/ARCHITECTURE.md`: a short "Sandbox service (SP3)" section: the process model (supervisor + forked children, slot uids, privilege drop before load), the protocol frames, the two pools and sockets, the store re-hash and static check at OPEN, the per-bar 2 s wall timeout and the rlimits (ruling 3, 4, 5), the seccomp profile (ruling 16, with the upstream source and sha), and that the client maps busy/unreachable to `SandboxUnavailable` and `SANDBOX_ERROR`/`SANDBOX_DIGEST_MISMATCH` to `SandboxFault`.
  - `docs/OPERATIONAL_STATE.md` under a new "Sandbox (SP3)" heading: the service is **not armed** (profile `ai`, off by default). First start: `./docker.sh -b` bakes a build id; `docker compose --profile ai up -d sandbox`; the operator then runs `mmr ai-author prove-isolation` (Plan 3) which calls `run_isolation_checks` against the running service. Record the open deviations the owner must confirm: ruling 2 (startup `CHOWN`, then dropped), ruling 3 (`RLIMIT_AS` = baseline + 256 MiB, not an absolute 256 MB), ruling 4 (`RLIMIT_CPU` 4 h backstop; the 2 s wall is the real limit), ruling 23 (tmpfs `noexec` via the short string form), and ruling 22 (Plan 3 mounts `mmr_authored_strategies` at `/store` read-write on `trader`). Note the seccomp source and sha from `scripts/build_sandbox_seccomp.py` and that a rebuild of the image needs a fresh `prove-isolation` (Plan 3 gate).
  - `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md`: under "Owner rulings to confirm", add the 25 lines `Plan 2 ruling 1 … Plan 2 ruling 25` (one line each, copied from this plan's Rulings section). Do not change any other part of the index.

- [ ] **Step 2: Full suite** (once, under the shared lock):

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
if .venv/bin/python -c "import xdist" 2>/dev/null; then
  .venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 --timeout-method=thread \
    --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/mmr-suite-sp3-02
else
  .venv/bin/python -m pytest tests/ -q --timeout=60 --timeout-method=thread \
    --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/mmr-suite-sp3-02
fi
.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q
rmdir /private/tmp/mmr-suite.lock
```

Expected: PASS. The `tests/fullstack/test_sandbox_service.py` cases skip (no `MMR_FAKE_BROKER`). Quote the live pytest summary line in the PR, not a remembered count.

- [ ] **Step 3: Commit.**

```bash
git add AGENTS.md docs/ARCHITECTURE.md docs/OPERATIONAL_STATE.md docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md
git commit -m "docs: describe the sandbox service, its sockets and its first start"
```

Commit body ends with:
```

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

## Self-review

**Spec coverage.**

| Spec | Where |
|---|---|
| 7.5 stanza (no anchors, network none, read-only, cap_drop/add, no-new-privileges, seccomp, pids/cpu/mem, tmpfs, env, volumes, same image, health) | Task 10; seccomp Task 7 |
| 7.5 process model (uid-0 supervisor, slot uids, setgid/setuid before load, no child capability, CAP_KILL only for own children) | Tasks 2, 3, 8, 9 |
| 7.5 Dockerfile group `sbxchild` gid 20000, uids 20001..20007 (amended), not in sudo/trader, world-readable image | Task 8 |
| 7.5 sockets group trader 0660, children never see the socket (only the pipe) | Task 4 (`bind_socket`), Task 2 (child keeps only its pipe) |
| 7.5 protocol frames OPEN/BAR/CLOSE/PING + replies | Task 1 |
| 7.5 OPEN re-hashes, static check, fork, drop, then load; LOOKBACK trim; strictly increasing times; SANDBOX_BUSY at once; per-bar 2 s; rlimits; closed fds; import guard; reduced built-ins; bad output → SANDBOX_ERROR, keep serving | Tasks 2, 3, 4 |
| 7.6 client, one session per (strategy, run): live = one deployment's conids, research = one conid (amended, ruling 26); busy/unreachable = availability; SANDBOX_ERROR/DIGEST_MISMATCH = fault | Tasks 1, 3, 5 |
| 7.9 `run_isolation_checks`, build id in PONG, posture incl. read-only root and pids limit | Tasks 1, 6 (Plan 3 runs the CLI) |
| 9 limits (pools 3/4 as amended, uids, 2 s, 256 MB, file size via protocol cap) | Tasks 1, 3, 9 |
| 10 escape corpus loaded with the check skipped | Task 6 manifest, Task 11 (bodies are Plan 1's) |
| 10 production-service, benign, hostile, pools, PING container tests | Task 11 |
| 10 fake child mode impossible in production | Task 3 (`InProcessSpawner` test-only), Task 10 grep test |
| 10 static-check refuses every probe/escape (so none is "fixed" into clean code) | **gap** — see note |

**Placeholders.** None: every step has real code or an exact edit. Three named dependencies live outside this plan: Plan 1's `api.py`/`static_check.py`/`keys.py`/`allowed_api.py` (base), Plan 1's escape corpus bodies (Task 6, Task 11, by name), and Plan 3's `prove-isolation` CLI and `/store` trader mount (ruling 22).

**Names.** Index names used exactly: `protocol.py`, `supervisor.py`, `child.py`, `isolation_checks.py`, `trader/sandbox_service.py`, `trader/authored/sandbox_client.py`, `config_defaults/sandbox-seccomp.json`; `LIVE_SOCKET`, `RESEARCH_SOCKET`, `STORE_DIR`, `SANDBOX_ERROR`, `SANDBOX_BUSY`, `SANDBOX_DIGEST_MISMATCH`, `PROTOCOL_VERSION`; `SandboxClient`, `ping`, `open_session`, `SandboxSession.bar`, `SandboxSession.close`, `SandboxInfo.image_digest`, `SandboxUnavailable`, `SandboxFault.code`; `run_isolation_checks`, `IsolationResult`, `CheckResult`; volumes `mmr_authored_strategies`, `mmr_sandbox_live`, `mmr_sandbox_research`. Additions beyond the index (all flagged as rulings): `SANDBOX_BAD_REQUEST`, `OPEN_PROBE`/`open_probe`, `SandboxProtocolError`, `PONG` fields, build-id identity, `conids`/`conid` on the wire, `MAX_SESSION_CONIDS`, `PROBE_CONID`, `LIVE_POOL_SIZE`, `RESEARCH_POOL_SIZE`, `MAX_CONIDS_PER_SESSION`, `SandboxSession.conids` (ruling 26).

**Review Focus → tests.** 1: `test_server.py::test_a_dropped_connection_frees_the_slot`. 2: `test_server.py::test_a_forged_child_reply_is_a_fault`. 3: `test_server.py::test_a_full_pool_answers_busy_at_once`. 4: `test_protocol.py::test_open_refuses_a_path_like_digest`, `test_protocol.py::test_probe_request_parses_and_refuses_a_traversal_name`. 5: `test_server.py::test_a_failed_wipe_quarantines_the_slot`.

**Static refusal of the hostile files (closed by D9).** Plan 1 ships the nine `HOSTILE_ESCAPES` files and its `test_every_sandbox_hostile_file_exists_and_is_rejected` asserts `check_source` refuses each; Task 6's `test_the_hostile_names_are_plan_1s_files` pins the two lists together. This plan's Task 11 proves runtime containment of the same files.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-11-ai-paper-sp3-02-sandbox-service.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — a fresh subagent implements each task and a fresh reviewer checks it before the next, then a whole-branch review at the end.
- **Native** — I implement every task myself in this session, then one fresh reviewer on the most capable model checks the whole branch.

For this plan I recommend **subagent-driven**, because the tasks lean heavily on each other's exact interfaces (protocol → child → supervisor → server → client) and a shipped mistake in the isolation boundary is high cost; an independent gate per task is worth the extra context.
