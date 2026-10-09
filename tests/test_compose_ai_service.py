"""SP2 Plan 5 Task 10: the ai container holds model credentials only and keeps its data (spec 4, 12)."""
from pathlib import Path, PurePosixPath

import pytest
import yaml

from tests.compose_rpc_helpers import CONTAINER_CONFIG, ROOT, load_compose, visible_rpc_files, volumes
from trader.messaging.principals import service_rpc_files

ALLOWED_ENV = {"TZ", "PYTHONDONTWRITEBYTECODE", "TRADER_TYPED_ADDRESS", "RESEARCH_TYPED_ADDRESS",
               "MMR_CONFIG_DEFAULTS",
               "OPENROUTER_API_KEY", "AWS_REGION",
               "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AZURE_OPENAI_ENDPOINT",
               "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION"}
FORBIDDEN_PREFIXES = ("ALPACA_", "IB_", "TWS_", "MASSIVE_", "TWELVEDATA_", "DASHBOARD_", "TELEGRAM")


@pytest.fixture(scope="module")
def ai():
    return load_compose()["services"]["ai"]


def test_ai_env_has_no_market_data_or_broker_credentials(ai):
    env = ai.get("environment") or {}
    assert not [name for name in env if name.startswith(FORBIDDEN_PREFIXES)]
    assert set(env) <= ALLOWED_ENV and "env_file" not in ai


def test_ai_service_does_not_merge_the_common_env():
    text = (ROOT / "docker-compose.yml").read_text()
    block = text.split("\n  ai:\n", 1)[1].split("\n  # ──", 1)[0]
    assert "*mmr-common-env" not in block                    # that anchor carries the Alpaca keys


def test_ai_reaches_the_research_server_by_its_compose_name(ai):
    assert ai["environment"]["RESEARCH_TYPED_ADDRESS"] == "tcp://research"
    assert "research" in load_compose()["services"]


def test_ai_sees_its_two_key_pairs_and_the_trader_public_key_only(ai):
    assert visible_rpc_files(ai) == service_rpc_files("ai") == {
        "ai_supervisor.key", "ai_supervisor.pub", "ai_research.key", "ai_research.pub", "trader.pub",
        "research.pub"}


def visible_config_files(service) -> set[str]:
    """Paths under ~/.config/mmr a container can read, from its mounts alone (PR #84 thread 4210307354).

    The image bakes config_defaults/*.yaml (trader.yaml among them) into that directory, so it must be masked
    by a tmpfs; then only the binds mounted inside it are visible.
    """
    vols = volumes(service)
    masked = any(v["type"] == "tmpfs" and v["target"] == CONTAINER_CONFIG for v in vols)
    assert masked, "the baked ~/.config/mmr (trader.yaml) would be visible"
    return {v["target"][len(CONTAINER_CONFIG) + 1:] for v in vols
            if v["type"] == "bind" and v["target"].startswith(CONTAINER_CONFIG + "/")}


def test_ai_sees_only_ai_yaml_its_keys_and_the_hmac_mask_under_the_config_dir(ai):
    assert visible_config_files(ai) == {
        "ai.yaml", "service_hmac.key", *(f"keys/rpc/{name}" for name in service_rpc_files("ai"))}
    mounted = {v["target"]: v for v in volumes(ai)}
    ai_yaml = mounted[f"{CONTAINER_CONFIG}/ai.yaml"]
    assert ai_yaml["read_only"] and ai_yaml["source"] == "${HOME}/.config/mmr/ai.yaml"
    assert not [v for v in volumes(ai) if "secrets" in v["source"] or v["source"] == "mmr_db_data"]
    assert not [v for v in volumes(ai) if v["type"] == "bind" and v["source"] == "${HOME}/.config/mmr"]


def test_ai_never_copies_the_config_templates_into_the_mask(ai):
    assert ai["environment"]["MMR_CONFIG_DEFAULTS"] == "off"


def test_the_config_defaults_switch_copies_nothing(tmp_path, monkeypatch):
    import trader.container as container
    target = tmp_path / "config"
    target.mkdir()
    monkeypatch.setattr(container, "MMR_CONFIG_DIR", target)
    monkeypatch.setenv("MMR_CONFIG_DEFAULTS", "off")
    assert container.ensure_config_dir() == target and list(target.iterdir()) == []
    monkeypatch.delenv("MMR_CONFIG_DEFAULTS")
    container.ensure_config_dir()
    assert (target / "trader.yaml").exists()                           # the default for every other process


def test_ai_data_is_a_named_volume_that_survives_recreation(ai):
    compose = load_compose()
    assert "mmr_ai_data" in compose["volumes"]
    data = [v for v in volumes(ai) if v["source"] == "mmr_ai_data"]
    assert len(data) == 1 and data[0]["type"] == "volume" and not data[0]["read_only"]
    default_path = yaml.safe_load((ROOT / "config_defaults" / "ai.yaml").read_text())["database_path"]
    assert str(PurePosixPath(default_path.replace("~", "/home/trader", 1)).parent) == data[0]["target"]
    assert data[0]["target"] not in (ai.get("tmpfs") or [])
    down = (ROOT / "docker.sh").read_text().split("\ndown() {", 1)[1].split("\n}", 1)[0]
    assert "--volumes" not in down and " -v" not in down     # ./docker.sh -d keeps every named volume


def test_the_image_creates_the_data_directory_so_a_fresh_volume_belongs_to_trader(ai):
    target = next(v["target"] for v in volumes(ai) if v["source"] == "mmr_ai_data")
    assert f"mkdir -p {target}" in (ROOT / "Dockerfile").read_text()


def test_ai_passes_the_key_mount_gate_with_the_retired_hmac_hidden(ai):
    hides = [v for v in volumes(ai) if v["target"] == f"{CONTAINER_CONFIG}/service_hmac.key"]
    assert len(hides) == 1 and hides[0]["source"] == "/dev/null" and hides[0]["read_only"]


def test_ai_is_opt_in_publishes_nothing_and_runs_the_service(ai):
    assert ai["profiles"] == ["ai"] and not ai.get("ports")
    assert ai["command"] == ["python", "-m", "trader.ai_service"]
    assert "healthcheck" in ai and "trader" in ai["depends_on"]


def test_ai_sees_the_same_baked_strategy_files_as_the_research_service(ai):
    """The research menu scans <working_dir>/strategies. The image bakes that tree (Dockerfile COPY ./), as it does
    for the research service, so both read one copy. A host bind on ai alone would let the two disagree, so there is none."""
    compose = load_compose()["services"]
    assert ai["working_dir"] == compose["research"]["working_dir"] == "/home/trader/mmr"
    assert "COPY --chown=trader:trader ./ /home/trader/mmr/" in (ROOT / "Dockerfile").read_text()
    ignored = {line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert "strategies" not in ignored and (ROOT / "strategies").is_dir()
    assert not [v for v in volumes(ai) if "strategies" in v["target"] or "strategies" in v["source"]]
