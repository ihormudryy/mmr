"""Static checks of the per-principal RPC key mounts in docker-compose.yml (spec 5.3)."""
import pytest

from tests.compose_rpc_helpers import (
    COMPOSE_PATH, CONTAINER_RPC_DIR, RETIRED_HMAC_TARGET, ROOT, load_compose, mounts_config_dir,
    visible_rpc_files, volumes,
)
from trader.messaging.principals import KNOWN_PRINCIPALS, SERVICE_PRINCIPAL, rpc_files_for


@pytest.fixture(scope="module")
def compose():
    return load_compose()


@pytest.mark.parametrize("name,principal", sorted(SERVICE_PRINCIPAL.items()))
def test_each_service_sees_only_its_own_key_pair_and_needed_public_keys(compose, name, principal):
    assert visible_rpc_files(compose["services"][name]) == rpc_files_for(principal)


@pytest.mark.parametrize("name,principal", sorted(
    (n, p) for n, p in SERVICE_PRINCIPAL.items() if p is not None))
def test_each_signing_service_mounts_its_own_public_key(compose, name, principal):
    assert f"{principal}.pub" in visible_rpc_files(compose["services"][name])


def test_every_long_lived_service_is_classified(compose):
    for name, svc in compose["services"].items():
        if name in ("ib-gateway", "fullstack-tests", "keygen"):
            continue
        assert name in SERVICE_PRINCIPAL, name


def test_no_service_mounts_another_principals_private_key(compose):
    for name, svc in compose["services"].items():
        if name == "keygen":
            continue
        files = visible_rpc_files(svc)
        assert files is not None, f"{name} sees the whole keys/rpc directory"
        own = SERVICE_PRINCIPAL.get(name)
        assert {f for f in files if f.endswith(".key")} <= ({f"{own}.key"} if own else set()), name


def test_every_key_bind_is_read_only_and_from_the_host_keys_dir(compose):
    for name, svc in compose["services"].items():
        for v in volumes(svc):
            if v["target"].startswith(CONTAINER_RPC_DIR + "/"):
                assert v["read_only"], (name, v)
                basename = v["target"].rsplit("/", 1)[1]
                assert v["source"] == "${HOME}/.config/mmr/keys/rpc/" + basename, (name, v)
                assert basename.rsplit(".", 1)[0] in KNOWN_PRINCIPALS, (name, v)


def test_retired_hmac_key_is_hidden_in_every_container(compose):
    for name, svc in compose["services"].items():
        if mounts_config_dir(svc):
            hides = [v for v in volumes(svc)
                     if v["target"] == RETIRED_HMAC_TARGET and v["source"] == "/dev/null" and v["read_only"]]
            assert hides, name
    for name, svc in compose["services"].items():
        for v in volumes(svc):
            assert not ("service_hmac" in v["source"] and v["source"] != "/dev/null"), (name, v)


def test_cli_private_key_is_mounted_only_in_the_short_lived_cli_service(compose):
    holders = [n for n, svc in compose["services"].items()
               if n != "keygen" and "cli.key" in (visible_rpc_files(svc) or set())]
    assert holders == ["cli"]


def test_keygen_service_is_one_shot_offline_and_the_only_writable_keys_mount(compose):
    keygen = compose["services"]["keygen"]
    assert keygen["profiles"] == ["tools"] and keygen["restart"] == "no" and keygen["network_mode"] == "none"
    assert keygen["environment"]["MMR_KEYGEN_CONTAINER"] == "1"
    assert [parse["target"] for parse in volumes(keygen)] == ["/keys"]
    writable = [n for n, svc in compose["services"].items()
                if any("keys/rpc" in v["source"] and v["type"] == "bind" and not v["read_only"]
                       for v in volumes(svc))]
    assert writable == ["keygen"]
    cli = compose["services"]["cli"]
    assert cli.get("profiles") == ["tools"] and cli.get("restart") == "no" and not cli.get("ports")


def test_trader_container_has_cli_pub_but_never_cli_key(compose):
    files = visible_rpc_files(compose["services"]["trader"])
    assert "cli.pub" in files and "cli.key" not in files


def test_runbook_never_execs_the_cli_in_the_trader_container():
    text = (ROOT / "docs/OPERATIONAL_STATE.md").read_text()
    assert "exec trader python -m trader.mmr_cli" not in text
    assert "docker compose run --rm cli" in text


def test_no_service_references_the_retired_hmac_key_except_to_hide_it():
    for line in COMPOSE_PATH.read_text().splitlines():
        if "service_hmac" in line and not line.lstrip().startswith("#"):
            assert "/dev/null:" in line, line
    assert "MMR_SERVICE_HMAC_KEY_FILE" not in COMPOSE_PATH.read_text()
