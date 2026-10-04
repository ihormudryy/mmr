"""G0 Task 5: Compose supervision / scheduler-ownership topology tests.

Pure structural assertions against the checked-in YAML — no Docker daemon
required. Confirms: (1) pycron's templates no longer declare the long-lived
service jobs (they're now one-supervisor-per-process Compose services), and
(2) docker-compose.yml gives each long-lived process its own service with
the hardening/volume/network/port-publishing rules the G0 plan requires.
"""
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

LONG_LIVED_JOB_NAMES = {"data_service", "trader_service", "strategy_service", "web_dashboard"}
PROCESS_SERVICES = ("data", "trader", "strategy", "dashboard", "scheduler")


def _load_yaml(relative_path: str) -> dict:
    return yaml.safe_load((REPO_ROOT / relative_path).read_text())


@pytest.mark.parametrize(
    "pycron_config_path",
    ["config_defaults/pycron.yaml", "config_defaults/no_docker_pycron.yaml"],
)
def test_long_lived_services_are_not_pycron_jobs(pycron_config_path):
    config = _load_yaml(pycron_config_path)
    names = {job["name"] for job in config["jobs"]}
    assert names.isdisjoint(LONG_LIVED_JOB_NAMES)


@pytest.mark.parametrize(
    "pycron_config_path",
    ["config_defaults/pycron.yaml", "config_defaults/no_docker_pycron.yaml"],
)
def test_one_shot_jobs_survive_the_long_lived_removal(pycron_config_path):
    """The removal must be surgical: refresh/backup one-shots stay declared."""
    config = _load_yaml(pycron_config_path)
    names = {job["name"] for job in config["jobs"]}
    assert {"data_refresh_us", "data_refresh_asx", "db_backup"}.issubset(names)


def test_compose_has_one_service_per_process():
    compose = _load_yaml("docker-compose.yml")
    for name in PROCESS_SERVICES:
        assert name in compose["services"]
        assert compose["services"][name]["restart"] == "unless-stopped"


def test_monolithic_mmr_service_is_gone():
    """The old all-in-one `mmr` service (one container running data/trader/
    strategy/dashboard via tmux+pycron) must not survive the split -- if it
    lingered it would double-bind ports and re-run everything the five
    dedicated services now own."""
    compose = _load_yaml("docker-compose.yml")
    assert "mmr" not in compose["services"]


def test_compose_services_use_one_built_image_with_per_service_command():
    compose = _load_yaml("docker-compose.yml")
    expected_commands = {
        "data": ["python", "-m", "trader.data_service"],
        "trader": ["python", "-m", "trader.trader_service"],
        "strategy": ["python", "-m", "trader.strategy_service"],
        "dashboard": ["python", "-m", "web.app"],
        "scheduler": ["python", "-m", "pycron.pycron", "--config", "/home/trader/.config/mmr/pycron.yaml"],
    }
    for name, command in expected_commands.items():
        service = compose["services"][name]
        assert service["command"] == command
        # one built image -- every service builds from the same Dockerfile
        # context and shares the same image tag, so `docker compose build`
        # produces exactly one image that each service's `command:` merely
        # runs differently, not five independently-built images.
        assert service["build"] == compose["services"]["trader"]["build"]
        assert service["image"] == compose["services"]["trader"]["image"]


def test_compose_services_are_hardened():
    compose = _load_yaml("docker-compose.yml")
    for name in PROCESS_SERVICES:
        service = compose["services"][name]
        assert service["user"] == "trader"
        assert service["read_only"] is True
        assert service["tmpfs"]
        assert "/tmp" in service["tmpfs"]
        assert "healthcheck" in service
        # explicit memory + CPU limits (non-swarm compose fields)
        assert "mem_limit" in service
        assert "cpus" in service
        assert "mmr-internal" in service["networks"]


def test_dashboard_has_no_database_volume():
    compose = _load_yaml("docker-compose.yml")
    dashboard_volumes = " ".join(compose["services"]["dashboard"].get("volumes", []))
    assert "mmr_db_data" not in dashboard_volumes


def test_trader_and_strategy_and_data_have_database_access():
    """trader owns the DB per the brief; data_service/strategy_service also
    write duckdb tables directly (history bars / events / proposals /
    universes -- confirmed against trader/data_service.py and
    trader/strategy/strategy_runtime.py), so they need it too."""
    compose = _load_yaml("docker-compose.yml")
    for name in ("trader", "data", "strategy"):
        volumes = " ".join(compose["services"][name].get("volumes", []))
        assert "mmr_db_data" in volumes


def test_scheduler_has_backup_and_config_paths():
    compose = _load_yaml("docker-compose.yml")
    scheduler = compose["services"]["scheduler"]
    volumes = " ".join(scheduler.get("volumes", []))
    assert ".config/mmr" in volumes
    assert "backups" in volumes


def test_only_dashboard_and_typed_query_command_ports_are_published():
    """Loopback-only publishing: dashboard HTTP + typed query(42101) and
    command(42102). The typed feed port (42103) and every legacy port
    (42001/42002/42003/42005/42006) must never be published to the host."""
    compose = _load_yaml("docker-compose.yml")
    published = []
    for name in PROCESS_SERVICES:
        for port_entry in compose["services"][name].get("ports", []):
            published.append(str(port_entry))

    joined = " ".join(published)
    for forbidden in ("42103", "42001", "42002", "42003", "42005", "42006"):
        assert f":{forbidden}:" not in joined, f"port {forbidden} must not be published"

    for entry in published:
        assert entry.startswith("127.0.0.1:"), f"{entry} must be loopback-only"

    assert any("42101" in p for p in published)
    assert any("42102" in p for p in published)


def test_strategy_binds_typed_rpc_on_all_interfaces():
    """Dashboard deploy tab calls strategy:42105 cross-container; loopback-only
    binds (the trader.yaml default) refuse those connections."""
    compose = _load_yaml("docker-compose.yml")
    env = compose["services"]["strategy"]["environment"]
    assert env.get("TYPED_BIND_ADDRESS") == "tcp://0.0.0.0"


def test_network_is_private_mmr_internal():
    compose = _load_yaml("docker-compose.yml")
    assert "mmr-internal" in compose["networks"]
