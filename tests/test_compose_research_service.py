from pathlib import Path

import pytest

from tests.compose_rpc_helpers import CONTAINER_CONFIG, ROOT, load_compose, visible_rpc_files, volumes
from trader.messaging.keys_cli import signing_key_problems
from trader.messaging.principals import service_rpc_files
from trader.research.signing import generate_private_key_pem

PRIVATE_DIR = f"{CONTAINER_CONFIG}/keys/private"
CONFIG_MOUNTERS = ("data", "trader", "strategy", "dashboard", "scheduler", "cli")


@pytest.fixture(scope="module")
def services():
    return load_compose()["services"]


def test_research_sees_its_keys_and_no_credentials(services):
    research = services["research"]
    assert visible_rpc_files(research) == service_rpc_files("research")
    env = research["environment"]
    assert not [k for k in env if k.startswith(("ALPACA_", "OPENROUTER", "AWS_", "AZURE_", "MASSIVE_", "TELEGRAM"))]
    assert research["profiles"] == ["ai"] and not research.get("ports")
    assert research["command"] == ["python", "-m", "trader.research_service"]


def test_only_research_mounts_the_signing_key(services):
    for name, svc in services.items():
        binds = [v for v in volumes(svc) if v["target"].startswith(PRIVATE_DIR + "/")]
        if name == "research":
            assert [(v["target"], v["read_only"]) for v in binds] == [(f"{PRIVATE_DIR}/signing.pem", True)]
        else:
            assert not binds, name
    for name in CONFIG_MOUNTERS:
        assert any(v["type"] == "tmpfs" and v["target"] == PRIVATE_DIR for v in volumes(services[name])), name


def test_research_mounts(services):
    targets = {v["target"]: v for v in volumes(services["research"])}
    for name in ("trader.yaml", "execution_costs.yaml"):
        assert targets[f"{CONTAINER_CONFIG}/{name}"]["read_only"]
    assert targets["/home/trader/.local/share/mmr/research"]["source"] == "mmr_research_data"
    assert targets["/home/trader/.local/share/mmr/data"]["source"] == "mmr_db_data"     # ruling 13
    assert not targets["/home/trader/.local/share/mmr/artifacts"]["read_only"]
    assert not [v for v in volumes(services["research"]) if "secrets" in v["source"]]


def test_trader_verify_dir_is_read_only_and_ai_sees_research_pub(services):
    verify = [v for v in volumes(services["trader"]) if v["target"] == f"{CONTAINER_CONFIG}/keys/verify"]
    assert len(verify) == 1 and verify[0]["read_only"]
    assert "research.pub" in visible_rpc_files(services["ai"])
    assert not [v for v in volumes(services["ai"]) if v["target"].startswith(PRIVATE_DIR)]


def test_keycheck_and_startup_gate_cover_research():
    text = (ROOT / "docker.sh").read_text()
    assert 'KEYCHECK_SERVICES="trader strategy dashboard cli scheduler data ai research"' in text
    assert "_compose_private_key_files" in text and 'volume rm "${KEYCHECK_PROJECT}_mmr_research_data"' in text


def test_research_service_never_names_the_journal():
    for path in (ROOT / "trader" / "research_service.py", *sorted((ROOT / "trader" / "research").glob("*.py"))):
        assert "journal_duckdb_path" not in path.read_text(), path


def test_signing_key_rule(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "rpc"))
    key = tmp_path / "signing.pem"
    key.write_bytes(generate_private_key_pem())
    key.chmod(0o600)
    assert signing_key_problems("research", tmp_path) == []
    assert signing_key_problems("trader", tmp_path) == [f"unexpected {tmp_path / 'signing.pem'}"]
    assert signing_key_problems("research", tmp_path / "missing") == [f"missing {tmp_path / 'missing' / 'signing.pem'}"]
