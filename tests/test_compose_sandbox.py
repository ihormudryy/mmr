import pytest
import yaml

from tests.compose_rpc_helpers import ROOT, load_compose, volumes

FORBIDDEN_ENV_PREFIXES = ("ALPACA_", "MASSIVE_", "TWELVEDATA_", "IB_", "TWS_", "OPENROUTER_",
                          "AWS_", "AZURE_", "DASHBOARD_", "TELEGRAM", "TRADING_MODE")
ALLOWED_ENV = {"TZ", "PYTHONDONTWRITEBYTECODE", "HOME", "TMPDIR", "NUMBA_CACHE_DIR",
               "MPLCONFIGDIR"}   # the build id is baked at /etc/mmr-build-id, not an env var
SOCKET_VOLUMES = {"mmr_sandbox_live": "/run/sandbox-live", "mmr_sandbox_research": "/run/sandbox-research"}


@pytest.fixture(scope="module")
def sandbox():
    return load_compose()["services"]["sandbox"]


def _sandbox_block() -> str:
    text = (ROOT / "docker-compose.yml").read_text()
    return text.split("\n  sandbox:\n", 1)[1].split("\n  # ──", 1)[0].split("\nvolumes:", 1)[0]


def test_sandbox_runs_the_service_with_no_arguments(sandbox):
    # Only the no-argument start drops CAP_CHOWN (trader/sandbox_service.py), so no flag may be added.
    assert sandbox["command"] == ["python", "-m", "trader.sandbox_service"]
    assert sandbox["user"] in ("0", 0)


def test_sandbox_bypasses_the_image_entrypoint(sandbox):
    # docker-entrypoint.sh does root-only chown work and reads IB settings; the sandbox must not run it.
    assert sandbox["entrypoint"] == []


def test_sandbox_does_not_inherit_the_shared_anchors():
    block = _sandbox_block()
    assert "*mmr-common-env" not in block and "*mmr-hardening" not in block
    assert "<<:" not in block


def test_sandbox_is_opt_in_and_in_the_test_profile(sandbox):
    assert sandbox["profiles"] == ["ai", "test"]


def test_sandbox_has_no_network_and_no_published_port(sandbox):
    assert sandbox["network_mode"] == "none"
    assert sandbox["ipc"] == "none"
    assert "networks" not in sandbox
    assert "ports" not in sandbox and "expose" not in sandbox


def test_sandbox_drops_all_caps_and_adds_only_the_four(sandbox):
    assert sandbox["cap_drop"] == ["ALL"]
    assert set(sandbox["cap_add"]) == {"CHOWN", "SETUID", "SETGID", "KILL"}
    assert len(sandbox["cap_add"]) == 4


def test_sandbox_hardening_flags(sandbox):
    assert sandbox["read_only"] is True
    assert sandbox["init"] is True
    assert "no-new-privileges:true" in sandbox["security_opt"]
    assert "seccomp=./config_defaults/sandbox-seccomp.json" in sandbox["security_opt"]
    assert (ROOT / "config_defaults" / "sandbox-seccomp.json").is_file()
    assert sandbox["pids_limit"] == 64
    assert sandbox["cpus"] == 2
    assert str(sandbox["mem_limit"]) == "4g"


def test_sandbox_bounds_the_open_file_limit(sandbox):
    # The child closes inherited fds in a loop up to this limit, so it must be small and fixed.
    assert sandbox["ulimits"]["nofile"] == {"soft": 1024, "hard": 1024}


def test_sandbox_environment_is_minimal(sandbox):
    env = sandbox.get("environment") or {}
    assert set(env) <= ALLOWED_ENV
    assert not [name for name in env if name.startswith(FORBIDDEN_ENV_PREFIXES)]
    assert env["TZ"] == "UTC" and env["HOME"] == "/tmp" and env["TMPDIR"] == "/tmp"
    assert "env_file" not in sandbox


def test_sandbox_mounts_the_store_read_only_and_the_two_socket_volumes(sandbox):
    vols = {v["target"]: v for v in volumes(sandbox)}
    assert vols["/store"]["read_only"] is True and vols["/store"]["source"] == "mmr_authored_strategies"
    for source, target in SOCKET_VOLUMES.items():
        assert vols[target]["source"] == source and vols[target]["type"] == "volume"
        assert vols[target]["read_only"] is False   # the supervisor creates its socket there


def test_sandbox_mounts_no_key_no_config_and_nothing_under_home(sandbox):
    # The posture check `no_home_mounts` fails on any mount under /home or /root, so the baked
    # config is hidden by the image's file modes (see the Dockerfile test below), not by a mask mount.
    targets = [v["target"] for v in volumes(sandbox)]
    assert not [t for t in targets if "keys" in t or ".config" in t or "mmr" in t.split("/")]
    assert not [t for t in targets if t.startswith(("/home", "/root"))]
    assert sorted(targets) == ["/run/sandbox-live", "/run/sandbox-research", "/store"]


def test_the_baked_config_dir_is_closed_to_other_users():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "chmod 0700 /home/trader/.config" in dockerfile


def test_sandbox_tmpfs_is_capped_and_noexec(sandbox):
    entries = sandbox["tmpfs"]
    assert isinstance(entries, list) and len(entries) == 1
    path, _, options = entries[0].partition(":")
    flags = options.split(",")
    assert path == "/tmp"
    assert "size=67108864" in flags
    assert {"noexec", "nosuid", "nodev"} <= set(flags)


def test_sandbox_healthcheck_pings_the_service(sandbox):
    assert sandbox["healthcheck"]["test"] == ["CMD", "python", "-m", "trader.sandbox_service", "--health"]


def test_the_three_volumes_are_declared():
    compose = load_compose()
    for name in ("mmr_authored_strategies", "mmr_sandbox_live", "mmr_sandbox_research"):
        assert name in compose["volumes"]


def test_strategy_and_research_mount_their_socket_volumes():
    compose = load_compose()
    strat = {v["target"] for v in volumes(compose["services"]["strategy"])}
    research = {v["target"] for v in volumes(compose["services"]["research"])}
    assert "/run/sandbox-live" in strat
    assert "/run/sandbox-research" in research


def test_each_socket_volume_is_mounted_only_by_its_own_caller():
    services = load_compose()["services"]
    holders = {source: sorted(name for name, service in services.items()
                              if any(v["source"] == source for v in volumes(service)))
               for source in SOCKET_VOLUMES}
    assert holders == {"mmr_sandbox_live": ["sandbox", "strategy"],
                       "mmr_sandbox_research": ["research", "sandbox"]}


def test_only_the_sandbox_and_the_trader_side_touch_the_store():
    services = load_compose()["services"]
    readers = [name for name, service in services.items()
               if any(v["source"] == "mmr_authored_strategies" for v in volumes(service))]
    assert "sandbox" in readers
    assert not {"strategy", "research", "ai", "dashboard", "scheduler", "data", "cli"} & set(readers)


def test_the_test_override_makes_research_reachable_without_its_service():
    override = yaml.safe_load((ROOT / "docker-compose.test.override.yml").read_text())
    research = override["services"]["research"]
    assert research["profiles"] == ["ai", "test"]
    assert research["command"] == ["sleep", "infinity"]
