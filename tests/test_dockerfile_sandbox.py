"""Static checks on the sandbox identities, the build id and the /store directory.

The tests read the build files as text. They never run Docker.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
DOCKERIGNORE = (ROOT / ".dockerignore").read_text().splitlines()
COMPOSE = (ROOT / "docker-compose.yml").read_text()


def test_sbxchild_group_and_uid_range_exist():
    assert "groupadd -g 20000 sbxchild" in DOCKERFILE
    assert "seq 20001 20007" in DOCKERFILE  # seven slots: live 3 + research 4 (ruling 26)
    assert re.search(r'useradd -u "\$uid" -g 20000 -M -N', DOCKERFILE)


def test_sandbox_uids_match_the_child_module():
    from trader.sandbox.child import SBXCHILD_GID, SLOT_UIDS

    assert f"groupadd -g {SBXCHILD_GID} sbxchild" in DOCKERFILE
    assert f"seq {SLOT_UIDS.start} {SLOT_UIDS.stop - 1}" in DOCKERFILE


def test_sandbox_uids_are_not_in_sudo_or_trader():
    line = next(text for text in DOCKERFILE.splitlines() if 'useradd -u "$uid"' in text)
    assert "-G" not in line and "sudo" not in line and "trader" not in line


def test_the_build_id_is_baked_and_labelled():
    assert "ARG MMR_BUILD_ID" in DOCKERFILE
    assert "/etc/mmr-build-id" in DOCKERFILE
    assert "LABEL mmr.build_id" in DOCKERFILE


def test_the_build_id_arg_is_declared_in_the_runtime_stage_before_use():
    arg_at = DOCKERFILE.index("ARG MMR_BUILD_ID")
    assert DOCKERFILE.index("FROM python") < arg_at < DOCKERFILE.index("/etc/mmr-build-id")


def test_compose_passes_the_build_id_to_every_build():
    anchor = COMPOSE[COMPOSE.index("x-mmr-build:"):]
    assert "MMR_BUILD_ID: ${MMR_BUILD_ID:-unset}" in anchor.split("\nx-", 1)[0].split("\nservices:", 1)[0]


def test_the_store_directory_is_created_for_the_read_only_mount():
    assert "mkdir -p /store" in DOCKERFILE
    assert "chown trader:trader /store" in DOCKERFILE
    assert "chmod 0755 /store" in DOCKERFILE


def test_the_escape_corpus_and_probes_are_not_dockerignored():
    ignored = {line.strip().rstrip("/") for line in DOCKERIGNORE if line.strip() and not line.startswith(("#", "!"))}
    needed = ("tests", "tests/authored", "tests/authored/escapes", "trader", "trader/sandbox", "trader/sandbox/probes")
    assert ignored.isdisjoint(needed)
    assert not any("escape" in pattern or "probes" in pattern for pattern in ignored)
