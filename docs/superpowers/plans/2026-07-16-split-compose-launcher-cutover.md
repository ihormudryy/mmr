# Split-Compose Launcher Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `docker.sh` operate the split Compose services without targeting the retired monolith.

**Architecture:** `docker.sh` selects Compose services by allowlisted service name, bootstraps portable HMAC configuration before `up`, and runs backups through `scheduler`. Tests run the launcher against a fake Docker executable; no test touches the real stack.

**Tech Stack:** Bash, Docker Compose/Podman Compose, pytest.

## Global Constraints

- Only stale-container cleanup may reference `mmr-mmr` or `mmr_mmr`.
- `-e` defaults to `trader`; source is immutable in split containers.
- New HMAC keys are 0600 and configured as `~/.config/mmr/service_hmac.key`.
- Do not modify any M1-F1 domain, feed, journal, or snapshot file.

---

### Task 1: Lock the launcher contract with fake-runtime tests

**Files:**

- Create: `tests/test_docker_helper.py`
- Modify: `tests/test_compose_topology.py`

**Interfaces:**

- Produces `run_docker_helper(*args, env=None)`, using temporary `HOME`, `PATH`, and a fake `docker` command that logs invocations.

- [ ] **Step 1: Write failing tests**

```python
def test_exec_defaults_to_trader(fake_docker):
    result = fake_docker.run("-e")
    assert result.returncode == 0
    assert "compose exec -it -u trader -w /home/trader/mmr trader bash -l" in result.log

def test_exec_rejects_unknown_service_before_runtime(fake_docker):
    result = fake_docker.run("-e", "unknown")
    assert result.returncode == 1
    assert "Unknown exec service: unknown" in result.stdout
    assert result.log == ""

def test_build_does_not_target_retired_mmr_service(fake_docker):
    result = fake_docker.run("-b")
    assert "build mmr" not in result.log

def test_sync_fails_for_read_only_images(fake_docker):
    result = fake_docker.run("-s")
    assert result.returncode == 1
    assert "read-only images" in result.stdout
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/python -m pytest tests/test_docker_helper.py -q`

Expected: FAIL because the helper and split-service behavior do not exist.

- [ ] **Step 3: Implement the fake-runtime fixture**

The fake executable logs each invocation through `MMR_FAKE_DOCKER_LOG`, succeeds for `info`, and returns a numeric memory value for `info --format`. The fixture invokes `docker.sh` with a temporary `HOME` and prepended fake `PATH`.

### Task 2: Make launcher commands service-aware

**Files:**

- Modify: `docker.sh`

**Interfaces:**

- Produces `EXEC_SERVICE=trader` and an allowlist of `trader`, `data`, `strategy`, `dashboard`, `scheduler`, and `ib-gateway`.

- [ ] **Step 1: Run the Task 1 tests to confirm the existing incorrect behavior**

Run: `.venv/bin/python -m pytest tests/test_docker_helper.py -q`

Expected: `-e dashboard` is ignored, `-b` invokes `build mmr`, and sync searches for the monolith.

- [ ] **Step 2: Parse and validate before runtime detection**

Move option parsing ahead of Docker/Podman detection. `-e` consumes one following non-option service token; otherwise it remains `trader`. Validate with:

```bash
case "$EXEC_SERVICE" in
  trader|data|strategy|dashboard|scheduler|ib-gateway) ;;
  *) echo "Unknown exec service: $EXEC_SERVICE"; exit 1 ;;
esac
```

- [ ] **Step 3: Replace monolith operations**

Use `$COMPOSE -f "$BUILDDIR/docker-compose.yml" build` in `build()`. Use Compose exec for the selected service; use `trader`/`/home/trader/mmr` for application services and `ibgateway`/`/home/ibgateway` for `ib-gateway`. Keep legacy names only in `_remove_legacy_monolith()`. Make `sync` and `sync_all` exit 1 with `./docker.sh -b -u` as the migration command.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/python -m pytest tests/test_docker_helper.py tests/test_compose_topology.py -q`

Expected: PASS.

### Task 3: Bootstrap config, correct backup ownership, and update docs

**Files:**

- Modify: `docker.sh`
- Modify: `start_mmr.sh`
- Modify: `README.md`
- Modify: `tests/test_docker_helper.py`

**Interfaces:**

- Produces `ensure_split_config()` before `up()` and uses `scheduler` for live backups.

- [ ] **Step 1: Add failing tests**

```python
def test_up_bootstraps_a_portable_hmac_key(fake_docker):
    result = fake_docker.run("-u", env={"TWS_USERID": "u", "TWS_PASSWORD": "p"})
    assert "service_hmac_key_file: ~/.config/mmr/service_hmac.key" in result.config.read_text()
    assert result.key.stat().st_mode & 0o777 == 0o600

def test_backup_uses_scheduler_when_running(fake_docker):
    result = fake_docker.run("-B", env={"MMR_FAKE_SCHEDULER": "1"})
    assert "exec -T scheduler python3 -m trader.mmr_cli data backup --keep 30" in result.log

def test_backup_fallback_uses_split_volume(fake_docker):
    result = fake_docker.run("-B")
    assert "-v mmr_db_data:/src:ro" in result.log
    assert "mmr_mmr_db_data" not in result.log
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/python -m pytest tests/test_docker_helper.py -q`

Expected: FAIL because startup does not provision configuration and backup targets old resources.

- [ ] **Step 3: Implement safe bootstrap and backup**

Copy only missing default YAML files to `$HOME/.config/mmr`. For a blank or missing default HMAC setting, create the key with `(umask 177 && head -c 48 /dev/urandom > "$key_path")`, chmod it 600, and write `service_hmac_key_file: ~/.config/mmr/service_hmac.key`. Normalize only the equivalent host-default absolute spelling; preserve any other configured key so the trader retains its fail-closed validation. Change `start_mmr.sh` to write the same portable spelling. Execute `python3 -m trader.mmr_cli data backup --keep 30` through `scheduler` when it is running; otherwise sidecar-copy `mmr_db_data`.

- [ ] **Step 4: Update Docker quick-start docs**

Document `-e [service]`, rebuild/recreate as the replacement for sync, and that Compose starts split services directly. Remove instructions to run the retired monolithic launcher inside the container.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_docker_helper.py tests/test_compose_topology.py tests/test_service_health.py -q && bash -n docker.sh start_mmr.sh && git diff --check`

Expected: PASS.

### Task 4: Keep live cutover outside this branch’s test run

**Files:**

- Modify: none unless verification identifies a launcher defect.

- [ ] **Step 1: Render the compose topology**

Run: `docker compose config --services`

Expected: `ib-gateway`, `data`, `trader`, `strategy`, `dashboard`, and `scheduler`; no `mmr` service.

- [ ] **Step 2: Do not run `docker.sh -u` against the current legacy stack**

The reviewed branch is ready for a planned operator cutover; live containers and credentials remain outside automated tests.
