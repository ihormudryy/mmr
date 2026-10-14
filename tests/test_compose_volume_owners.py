"""Issue #180: Docker fills a new named volume from the image folder at its mount point, with that folder's
owner. A mount point the image does not create comes up root-owned, and a service running as trader cannot
write it (the research service crash-looped on a fresh deploy)."""
from __future__ import annotations

import re

from tests.compose_rpc_helpers import ROOT, load_compose, volumes

# The sandbox supervisor (root, CAP_CHOWN) sets the owner of its socket volumes at start (SP3).
SANDBOX_SOCKET_VOLUMES = {"mmr_sandbox_live", "mmr_sandbox_research"}


def _run_instructions() -> list[str]:
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return [line[len("RUN "):] for line in text.splitlines() if line.startswith("RUN ")]


def _created_for_trader(path: str) -> bool:
    for run in _run_instructions():
        steps = [step.split() for step in run.split("&&")]
        created = {item for step in steps if step[:2] == ["mkdir", "-p"] for item in step[2:]}
        owned = re.findall(r"chown (?:-R )?trader:trader (\S+)", run)
        if path in created and any(path == root or path.startswith(root.rstrip("/") + "/") for root in owned):
            return True
    return False


def _trader_volume_mounts():
    compose = load_compose()
    named = set(compose.get("volumes", {}))
    for name, service in compose["services"].items():
        if service.get("user") != "trader":
            continue
        for mount in volumes(service):
            if mount["source"] in named and mount["source"] not in SANDBOX_SOCKET_VOLUMES:
                yield name, mount["source"], mount["target"]


def test_every_named_volume_of_a_trader_service_is_created_for_trader_in_the_image():
    missing = [(service, source, target) for service, source, target in _trader_volume_mounts()
               if not _created_for_trader(target)]
    assert missing == []


def test_the_check_sees_the_research_volume():
    assert ("research", "mmr_research_data", "/home/trader/.local/share/mmr/research") in set(_trader_volume_mounts())
