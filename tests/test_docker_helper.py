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
import subprocess

import pytest


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
if [ \"$1\" = \"compose\" ] && [ \"${MMR_FAKE_SCHEDULER:-}\" = \"1\" ]; then
  case \" $* \" in
    *\" ps -q scheduler \"*) printf '%s\\n' scheduler-container ;;
  esac
fi
exit 0
"""
        )
        fake_docker.chmod(0o755)

    def run(self, *args: str, env: dict[str, str] | None = None) -> DockerHelperResult:
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
            input="",
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


def test_sync_fails_loudly_for_read_only_images(fake_docker: FakeDocker):
    result = fake_docker.run("-s")

    assert result.returncode == 1
    assert "read-only images" in result.stdout


def test_up_bootstraps_a_portable_hmac_key(fake_docker: FakeDocker):
    result = fake_docker.run("-u")

    assert result.returncode == 0, result.stdout
    assert result.config.exists()
    assert "service_hmac_key_file: ~/.config/mmr/service_hmac.key" in result.config.read_text()
    assert result.key.stat().st_mode & 0o777 == 0o600
    assert "compose -f" in result.log
    assert "up -d" in result.log


def test_backup_uses_scheduler_when_running(fake_docker: FakeDocker):
    result = fake_docker.run("-B", env={"MMR_FAKE_SCHEDULER": "1"})

    assert result.returncode == 0, result.stdout
    assert "exec -T scheduler python3 -m trader.mmr_cli data backup --keep 30" in result.log


def test_backup_fallback_uses_split_volume(fake_docker: FakeDocker):
    result = fake_docker.run("-B")

    assert result.returncode == 0, result.stdout
    assert "-v mmr_db_data:/src:ro" in result.log
    assert "mmr_mmr_db_data" not in result.log
