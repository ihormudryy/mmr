"""Black-box contract tests for the split-Compose ``docker.sh`` helper.

The helper starts by probing Docker and normally mutates host-mounted runtime
paths, so these tests place a tiny fake ``docker`` executable first in PATH and
give the script an isolated HOME.  They assert the public operator commands,
not bash implementation details.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import stat
import subprocess

import pytest

from tests.rpc_identity_fixtures import write_keyset


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class DockerHelperResult:
    completed: subprocess.CompletedProcess[str]
    log_path: Path
    home: Path

    @property
    def returncode(self) -> int:
        return self.completed.returncode

    @property
    def stdout(self) -> str:
        return self.completed.stdout

    @property
    def log(self) -> str:
        return self.log_path.read_text() if self.log_path.exists() else ""

    @property
    def config(self) -> Path:
        return self.home / ".config/mmr/trader.yaml"

    @property
    def key(self) -> Path:
        return self.home / ".config/mmr/service_hmac.key"



class FakeDocker:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.bin_dir = tmp_path / "bin"
        self.log_path = tmp_path / "docker.log"
        self.home.mkdir()
        self.bin_dir.mkdir()
        fake_docker = self.bin_dir / "docker"
        fake_docker.write_text(
            """#!/bin/sh
printf '%s\\n' \"$*\" >> \"${MMR_FAKE_DOCKER_LOG:?}\"
if [ \"$1\" = \"info\" ]; then
  if [ \"${2:-}\" = \"--format\" ]; then
    printf '%s\\n' 34359738368
  fi
  exit 0
fi
if [ \"$1\" = \"compose\" ]; then
  case \" $* \" in
    *\" ps -q scheduler \"*)
      if [ \"${MMR_FAKE_SCHEDULER:-}\" = \"1\" ]; then
        printf '%s\\n' scheduler-container
      fi
      ;;
    *\" config --format json \"*)
      # Match docker-compose.yml ``name: mmr`` so db_data_volume() resolves
      # the project-prefixed live volume in helper tests.
      printf '%s\\n' '{\"name\":\"mmr\"}'
      ;;
  esac
  exit 0
fi
# Pretend the compose-prefixed DB volume exists; the unprefixed sibling does not.
if [ \"$1\" = \"volume\" ] && [ \"${2:-}\" = \"inspect\" ]; then
  case \"${3:-}\" in
    mmr_mmr_db_data) exit 0 ;;
    *) exit 1 ;;
  esac
fi
exit 0
"""
        )
        fake_docker.chmod(0o755)

    @property
    def compose_calls(self) -> list[list[str]]:
        """Arguments after ``compose -f <file>`` for each compose invocation."""
        calls = []
        for line in (self.log_path.read_text() if self.log_path.exists() else '').splitlines():
            words = shlex.split(line)
            if words[:2] == ["compose", "-f"]:
                calls.append(words[3:])
        return calls

    @property
    def rpc_dir(self) -> Path:
        return self.home / ".config/mmr/keys/rpc"

    def write_keys(self) -> None:
        write_keyset(self.rpc_dir)

    def run(self, *args: str, env: dict[str, str] | None = None,
            stdin: str = "") -> DockerHelperResult:
        process_env = os.environ.copy()
        process_env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin_dir}{os.pathsep}{process_env['PATH']}",
                "MMR_CONTAINER_RUNTIME": "docker",
                "MMR_FAKE_DOCKER_LOG": str(self.log_path),
            }
        )
        if env:
            process_env.update(env)
        completed = subprocess.run(
            [str(REPO_ROOT / "docker.sh"), *args],
            cwd=REPO_ROOT,
            env=process_env,
            text=True,
            input=stdin,
            capture_output=True,
            check=False,
        )
        return DockerHelperResult(completed, self.log_path, self.home)


@pytest.fixture
def fake_docker(tmp_path: Path):
    env_file = REPO_ROOT / ".env"
    original_env = env_file.read_bytes() if env_file.exists() else None
    env_file.write_text("TWS_USERID=test-user\nTWS_PASSWORD=test-password\nTRADING_MODE=paper\n")
    try:
        yield FakeDocker(tmp_path)
    finally:
        if original_env is None:
            env_file.unlink(missing_ok=True)
        else:
            env_file.write_bytes(original_env)


def test_exec_defaults_to_trader(fake_docker: FakeDocker):
    result = fake_docker.run("-e")

    assert result.returncode == 0, result.stdout
    assert "compose -f" in result.log
    assert "exec -it -u trader -w /home/trader/mmr trader bash -l" in result.log


def test_exec_accepts_allowlisted_service(fake_docker: FakeDocker):
    result = fake_docker.run("-e", "dashboard")

    assert result.returncode == 0, result.stdout
    assert "exec -it -u trader -w /home/trader/mmr dashboard bash -l" in result.log


def test_exec_rejects_unknown_service_before_runtime(fake_docker: FakeDocker):
    result = fake_docker.run("-e", "unknown")

    assert result.returncode == 1
    assert "Unknown exec service: unknown" in result.stdout
    assert result.log == ""


def test_build_does_not_target_retired_mmr_service(fake_docker: FakeDocker):
    result = fake_docker.run("-b")

    assert result.returncode == 0, result.stdout
    assert "compose -f" in result.log
    assert "build mmr" not in result.log


def test_build_reclaims_dangling_cache_after_success(fake_docker: FakeDocker):
    """Repeated -b must not accumulate dangling <none> images / BuildKit cache."""
    result = fake_docker.run("-b")

    assert result.returncode == 0, result.stdout
    assert "image prune --force" in result.log
    assert "builder prune --force" in result.log
    assert "builder prune --all" not in result.log


def test_sync_fails_loudly_for_read_only_images(fake_docker: FakeDocker):
    result = fake_docker.run("-s")

    assert result.returncode == 1
    assert "read-only images" in result.stdout


def test_up_starts_the_stack_when_every_rpc_key_exists(fake_docker: FakeDocker):
    fake_docker.write_keys()
    result = fake_docker.run("-u")

    assert result.returncode == 0, result.stdout
    assert result.config.exists()
    assert "compose -f" in result.log
    assert "up -d" in result.log


def test_up_is_idempotent_when_default_configs_already_exist(fake_docker: FakeDocker):
    fake_docker.write_keys()
    first = fake_docker.run("-u")
    second = fake_docker.run("-u")

    assert first.returncode == 0, first.stdout
    assert second.returncode == 0, second.stdout
    assert second.log.count("up -d") == 2


def test_up_refuses_when_an_rpc_key_is_missing(fake_docker: FakeDocker):
    fake_docker.write_keys()
    (fake_docker.rpc_dir / "strategy.pub").unlink()
    result = fake_docker.run("-u")

    assert result.returncode != 0
    assert "mmr keys init" in result.stdout and "strategy.pub" in result.stdout
    assert not fake_docker.compose_calls


def test_up_refuses_a_directory_in_place_of_a_key(fake_docker: FakeDocker):
    fake_docker.write_keys()
    (fake_docker.rpc_dir / "cli.key").unlink()
    (fake_docker.rpc_dir / "cli.key").mkdir()
    result = fake_docker.run("-u")

    assert result.returncode != 0 and "cli.key" in result.stdout
    assert not fake_docker.compose_calls


def test_up_never_deletes_an_existing_hmac_key_and_reminds_the_operator(fake_docker: FakeDocker):
    fake_docker.write_keys()
    key = fake_docker.home / ".config/mmr/service_hmac.key"
    key.write_text("old")
    result = fake_docker.run("-u")

    assert result.returncode == 0, result.stdout
    assert key.read_text() == "old"
    assert "delete it when the cutover is verified" in result.stdout
    helper = (REPO_ROOT / "docker.sh").read_text()
    for function in ("_require_rpc_keys", "_retire_hmac_config"):
        body = helper.split(f"{function}() {{")[1].split("\n}")[0]
        assert "rm " not in body, function


def test_up_no_longer_provisions_an_hmac_key_and_strips_the_yaml_line(fake_docker: FakeDocker):
    fake_docker.write_keys()
    config_dir = fake_docker.home / ".config/mmr"
    (config_dir / "trader.yaml").write_text(
        "ib_server_address: 127.0.0.1\nservice_hmac_key_file: ~/.config/mmr/service_hmac.key\n")
    result = fake_docker.run("-u")

    assert result.returncode == 0, result.stdout
    assert not result.key.exists()
    assert "service_hmac_key_file" not in result.config.read_text()
    assert "ib_server_address" in result.config.read_text()
    assert (config_dir / "trader.yaml.bak").exists()


def test_k_runs_keygen_in_the_one_shot_container_as_the_host_user(fake_docker: FakeDocker):
    result = fake_docker.run("-k")

    assert result.returncode == 0, result.stdout
    call = fake_docker.compose_calls[-1]
    assert call[:3] == ["run", "--rm", "--no-deps"] and "keygen" in call and call[-1] == "init"
    assert f"{os.getuid()}:{os.getgid()}" in call
    assert stat.S_IMODE(os.lstat(fake_docker.rpc_dir).st_mode) == 0o700


def test_k_rotate_passes_the_principal(fake_docker: FakeDocker):
    fake_docker.write_keys()
    result = fake_docker.run("-k", "--rotate", "dashboard")

    assert result.returncode == 0, result.stdout
    assert fake_docker.compose_calls[-1][-3:] == ["init", "--rotate", "dashboard"]


@pytest.mark.parametrize("principal", ["../x", "scheduler", "telegram_bridge", "CLI"])
def test_k_rejects_an_unknown_principal_before_any_docker_call(fake_docker: FakeDocker, principal):
    result = fake_docker.run("-k", "--rotate", principal)
    assert result.returncode != 0
    assert not fake_docker.compose_calls


def test_k_fails_when_keygen_leaves_a_loose_mode(fake_docker: FakeDocker):
    fake_docker.write_keys()
    os.chmod(fake_docker.rpc_dir / "cli.key", 0o644)
    result = fake_docker.run("-k")
    assert result.returncode != 0 and "cli.key" in result.stdout


def test_k_backup_runs_in_keygen_with_only_the_recipient_and_a_private_backup_dir(fake_docker: FakeDocker):
    fake_docker.write_keys()
    (fake_docker.home / ".config/mmr/keys/rpc_backup_recipient.txt").write_text("age1xyz")
    result = fake_docker.run("-k", "--backup")

    assert result.returncode == 0, result.stdout
    call = " ".join(fake_docker.compose_calls[-1])
    assert "keygen backup" in call and "identity" not in call
    assert "/recipient.txt:ro" in call
    backup_dir = fake_docker.home / ".local/share/mmr/backups/rpc_keys"
    assert stat.S_IMODE(os.lstat(backup_dir).st_mode) == 0o700


def test_k_backup_without_a_recipient_fails_with_instructions(fake_docker: FakeDocker):
    result = fake_docker.run("-k", "--backup")
    assert result.returncode != 0 and "age" in result.stdout
    assert not fake_docker.compose_calls


def test_k_restore_streams_the_identity_from_stdin_and_never_writes_it(fake_docker: FakeDocker, tmp_path):
    archive = tmp_path / "rpc_keys.tar.age"
    archive.write_bytes(b"ciphertext")
    result = fake_docker.run("-k", "--restore", str(archive), stdin="AGE-SECRET-KEY-TEST\n")

    assert result.returncode == 0, result.stdout
    assert "AGE-SECRET-KEY-TEST" not in result.stdout + result.completed.stderr
    call = fake_docker.compose_calls[-1]
    assert "AGE-SECRET-KEY-TEST" not in " ".join(call)
    assert "-T" in call and call[-1] == "--identity-stdin"
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and b"AGE-SECRET" in p.read_bytes()
                and p != fake_docker.log_path]


def test_k_restore_with_an_identity_file_deletes_it_only_when_asked(fake_docker: FakeDocker, tmp_path):
    archive = tmp_path / "rpc_keys.tar.age"
    archive.write_bytes(b"ciphertext")
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-TEST")
    assert fake_docker.run("-k", "--restore", str(archive), "--identity-file", str(identity)).returncode == 0
    assert identity.exists()
    assert fake_docker.run("-k", "--restore", str(archive), "--identity-file", str(identity),
                           "--delete-identity-file").returncode == 0
    assert not identity.exists()


def test_db_backup_helper_excludes_the_rpc_key_directory(tmp_path):
    from trader.data.db_backup import run_backup

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "mmr.duckdb").write_bytes(b"db")
    backup_dir = tmp_path / "backups"
    rpc_backups = backup_dir / "rpc_keys"
    rpc_backups.mkdir(parents=True)
    (rpc_backups / "rpc_keys_20261006T000000Z.tar.age").write_bytes(b"x")
    write_keyset(tmp_path / "config/keys/rpc")

    for _ in range(3):
        run_backup(data_dir, backup_dir, keep=1)
    assert not list(backup_dir.rglob("*.key")) and not list(backup_dir.rglob("*.pub"))
    assert (rpc_backups / "rpc_keys_20261006T000000Z.tar.age").exists()


def test_backup_uses_scheduler_when_running(fake_docker: FakeDocker):
    result = fake_docker.run("-B", env={"MMR_FAKE_SCHEDULER": "1"})

    assert result.returncode == 0, result.stdout
    assert "exec -T scheduler python3 -m trader.mmr_cli data backup --keep 30" in result.log


def test_backup_fallback_uses_compose_project_volume(fake_docker: FakeDocker):
    """Compose ``name: mmr`` + volume ``mmr_db_data`` → ``mmr_mmr_db_data``.

    Backing up the unprefixed sibling leaves Portfolios on the live volume
    unsaved and made empty-volume seed skip restore after ``down --volumes``.
    """
    result = fake_docker.run("-B")

    assert result.returncode == 0, result.stdout
    assert "-v mmr_mmr_db_data:/src:ro" in result.log
    # Must not target the stale unprefixed sibling volume.
    assert "-v mmr_db_data:/src:ro" not in result.log
