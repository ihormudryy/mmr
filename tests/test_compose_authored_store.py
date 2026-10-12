"""SP3 Plan 3 Task 4: the store volume is read-write in the trader only (spec 7.3; ruling 20)."""
from __future__ import annotations

import pytest

from tests.compose_rpc_helpers import ROOT, load_compose, volumes

TARGET = "/store"                     # Plan 2 ruling 22


def test_the_trader_mounts_the_store_read_write():
    compose = load_compose()
    assert "mmr_authored_strategies" in compose["volumes"]
    mounts = [v for v in volumes(compose["services"]["trader"]) if v["source"] == "mmr_authored_strategies"]
    assert mounts == [{"type": "volume", "source": "mmr_authored_strategies", "target": TARGET, "read_only": False}]
    assert compose["services"]["trader"]["environment"]["MMR_AUTHORED_STORE_DIR"] == TARGET


@pytest.mark.parametrize("name", ["ai", "research", "strategy", "dashboard", "data", "scheduler", "cli"])
def test_no_other_service_of_this_plan_mounts_the_store(name):
    service = load_compose()["services"][name]
    assert not [v for v in volumes(service) if v["source"] == "mmr_authored_strategies"]


def test_the_image_creates_the_store_directory_owned_by_trader():
    assert "mkdir -p /store && chown trader:trader /store" in (ROOT / "Dockerfile").read_text()


def test_the_key_check_removes_its_own_store_volume():
    assert 'volume rm "${KEYCHECK_PROJECT}_mmr_authored_strategies"' in (ROOT / "docker.sh").read_text()
