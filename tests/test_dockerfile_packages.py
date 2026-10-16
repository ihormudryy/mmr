"""Issue #206: the runtime image carries no compilers, no unused shell tools and no Debian `age` (Go 1.19.8)."""
from __future__ import annotations

import re

from tests.compose_rpc_helpers import ROOT

DOCKERFILE = (ROOT / "Dockerfile").read_text()


def _stage(name: str) -> str:
    match = re.search(rf"^FROM \S+ AS {re.escape(name)}\n(.*?)(?=^FROM |\Z)", DOCKERFILE, re.S | re.M)
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


def test_age_is_built_from_its_checked_release_source_with_patched_go_and_crypto():
    """#208: the official binaries were built with Go 1.27.0 and x/crypto 0.55.0."""
    age = _stage("age-build")
    assert re.search(r"^ARG AGE_SOURCE_SHA256=[0-9a-f]{64}$", age, re.M) and "sha256sum -c -" in age
    assert "go get golang.org/x/crypto@${X_CRYPTO_VERSION}" in age and "GOTOOLCHAIN=local" in age
    assert "COPY --from=age-build /out/age /out/age-keygen /usr/local/bin/" in _stage("runtime")


def test_the_runtime_image_carries_neither_pip_nor_uv():
    """pip's vendored packages carried CVEs; uv is mounted for the install steps only."""
    runtime = _stage("runtime")
    assert "pip install" not in runtime.replace("uv pip install", "")
    assert "COPY --from=ghcr.io/astral-sh/uv" not in runtime


def test_every_base_image_is_pinned_by_digest():
    for line in re.findall(r"^FROM (\S+)", DOCKERFILE, re.M):
        assert line == "runtime" or "@sha256:" in line, line


def test_the_virtualenv_is_installed_from_uv_lock_not_loose_requirements():
    """CI tests uv.lock; requirements.txt has only minimum versions, so the image ran untested ones."""
    build = _stage("build")
    assert "uv sync --frozen --no-install-project" in build and "--seed" not in build
    assert "COPY --chown=trader:trader pyproject.toml uv.lock /home/trader/mmr/" in build
    assert "requirements.txt" not in build
