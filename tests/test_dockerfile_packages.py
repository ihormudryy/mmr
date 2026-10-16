"""Issue #206: the runtime image carries no compilers, no unused shell tools and no Debian `age` (Go 1.19.8)."""
from __future__ import annotations

import re

from tests.compose_rpc_helpers import ROOT

DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _stage(name: str) -> str:
    match = re.search(rf"^FROM \S+ AS {name}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.S | re.M)
    assert match, f"no {name} stage"
    return match.group(1)


def _apt_packages(stage: str) -> set[str]:
    packages = set()
    for run in re.findall(r"apt-get install -y --no-install-recommends(.*?)&& rm -rf", stage.replace("\\\n", " "), re.S):
        packages |= {word for word in run.split() if re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", word)}
    return packages


def test_the_runtime_stage_installs_no_compiler_and_no_unused_shell_tool():
    banned = {"build-essential", "libssl-dev", "libffi-dev", "vim", "tmux", "rsync", "wget", "dialog",
              "apt-utils", "lnav", "net-tools", "iputils-ping", "age", "sudo"}
    assert not _apt_packages(_stage("runtime")) & banned


def test_compilers_live_only_in_the_build_stage_and_the_venv_is_copied():
    assert "build-essential" in _apt_packages(_stage("build"))
    assert "COPY --from=build --chown=trader:trader /home/trader/.venv /home/trader/.venv" in _stage("runtime")


def test_both_stages_apply_debian_security_updates():
    for name in ("build", "runtime"):
        assert "apt-get upgrade -y" in _stage(name), name


def test_age_is_the_pinned_official_release_checked_by_sha256_for_both_architectures():
    runtime = _stage("runtime")
    assert "github.com/FiloSottile/age/releases/download/v${AGE_VERSION}" in runtime
    assert re.search(r"amd64\) sum=[0-9a-f]{64}", runtime) and re.search(r"arm64\) sum=[0-9a-f]{64}", runtime)
    assert "sha256sum -c -" in runtime
