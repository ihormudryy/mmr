"""SP1 Plan 5: only the trader can read the Telegram secrets directory (static compose checks)."""
import pytest

from tests.compose_rpc_helpers import CONTAINER_CONFIG, load_compose, mounts_config_dir, volumes

SECRETS = f"{CONTAINER_CONFIG}/secrets"
HOST_SECRETS = "${HOME}/.config/mmr/secrets"


@pytest.fixture(scope="module")
def compose():
    return load_compose()


def test_only_the_trader_mounts_the_secrets_dir_read_only(compose):
    binds = {name: [v for v in volumes(svc) if v["type"] == "bind" and v["target"] == SECRETS]
             for name, svc in compose["services"].items()}
    assert [(v["source"], v["read_only"]) for v in binds.pop("trader")] == [(HOST_SECRETS, True)]
    assert not any(binds.values()), binds


def test_no_other_service_can_read_the_secrets_dir(compose):
    for name, svc in compose["services"].items():
        if name == "trader":
            continue
        for v in volumes(svc):
            assert "secrets" not in v["source"], (name, v)
        if mounts_config_dir(svc):
            assert any(v["type"] == "tmpfs" and v["target"] == SECRETS for v in volumes(svc)), name


def test_the_secrets_bind_comes_after_the_config_dir_mount(compose):
    targets = [v["target"] for v in volumes(compose["services"]["trader"])]
    assert targets.index(CONTAINER_CONFIG) < targets.index(SECRETS)
