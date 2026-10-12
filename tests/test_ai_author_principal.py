"""SP3 Plan 3 Task 1: the ai_author principal, its rights and its key mounts (spec 5, 10 Keys)."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

from tests.compose_rpc_helpers import ROOT, load_compose, visible_rpc_files
from tests.rpc_identity_fixtures import write_keyset
from trader.messaging.keys_cli import main as keys_main
from trader.messaging.principals import (
    CALLS, CLIENT_PRINCIPALS, KNOWN_PRINCIPALS, SERVER_ACCEPTS, SERVER_PRINCIPALS, SERVICE_PRINCIPAL, TRADER_ACL,
    service_principals, service_rpc_files,
)
from trader.messaging.rpc_keys import RESTART_ON_ROTATE, init_keys

AUTHORED_RIGHTS = {
    ("command", "submit_authored_strategy"): {"ai_author"},
    ("query", "get_authored_strategies"): {"ai_author"},
    ("query", "list_authored_strategies"): {"cli", "dashboard"},
    ("command", "report_authored_evaluation"): {"research"},
    ("command", "retire_authored_strategy"): {"strategy"},
    ("command", "record_isolation_proof"): {"cli"},
    ("query", "get_isolation_proof"): {"cli", "strategy"},
}


@pytest.fixture(autouse=True)
def rpc_keys_dir(tmp_path, monkeypatch):
    """check-mount loads keys; it must look at this test's keys, never the owner's."""
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "rpc"))


def test_ai_author_is_a_client_that_calls_only_the_trader():
    assert "ai_author" in KNOWN_PRINCIPALS and "ai_author" in CLIENT_PRINCIPALS
    assert "ai_author" not in SERVER_PRINCIPALS
    assert CALLS["ai_author"] == {"trader"}
    assert "ai_author" in SERVER_ACCEPTS["trader"]
    assert all("ai_author" not in accepted for server, accepted in SERVER_ACCEPTS.items() if server != "trader")


def test_authored_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in AUTHORED_RIGHTS} == AUTHORED_RIGHTS


def test_ai_author_holds_exactly_its_two_rights():
    rights = {key for key, allowed in TRADER_ACL.items() if "ai_author" in allowed}
    assert rights == {("command", "submit_authored_strategy"), ("query", "get_authored_strategies")}


def test_only_the_ai_service_holds_the_ai_author_private_key():
    assert service_principals("ai") == ("ai_supervisor", "ai_research", "ai_author")
    holders = {service for service in SERVICE_PRINCIPAL if "ai_author.key" in service_rpc_files(service)}
    assert holders == {"ai"}
    readers = {service for service in SERVICE_PRINCIPAL if "ai_author.pub" in service_rpc_files(service)}
    assert readers == {"ai", "trader"}


def test_compose_mounts_follow_the_principal_tables():
    services = load_compose()["services"]
    assert {"ai_author.key", "ai_author.pub"} <= visible_rpc_files(services["ai"])
    assert "ai_author.pub" in visible_rpc_files(services["trader"])
    for name, service in services.items():
        if name not in ("ai", "keygen"):
            visible = visible_rpc_files(service)
            assert visible is not None, f"{name} sees the whole host key directory"
            assert "ai_author.key" not in visible, name


def test_rotation_restarts_the_ai_service_and_the_trader():
    assert RESTART_ON_ROTATE["ai_author"] == ("ai", "trader")
    assert RESTART_ON_ROTATE["ai_research"] == ("ai", "research", "trader")
    assert RESTART_ON_ROTATE["trader"] == ("ai", "dashboard", "research", "strategy", "trader")


def test_keys_init_creates_the_ai_author_pair(tmp_path):
    rows = {row.principal: row.status for row in init_keys(tmp_path / "keys")}
    assert rows["ai_author"] == "created"
    assert (tmp_path / "keys" / "ai_author.key").stat().st_mode & 0o777 == 0o600


def _container_view(tmp_path: Path, service: str, *, extra: tuple[str, ...] = ()) -> list[str]:
    host = tmp_path / "host_rpc"
    write_keyset(host)
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    for name in service_rpc_files(service) | set(extra):
        shutil.copy2(host / name, rpc / name)
    hmac_file = tmp_path / "service_hmac.key"
    hmac_file.write_bytes(b"")
    return ["check-mount", service, "--keys-dir", str(rpc), "--hmac-file", str(hmac_file)]


@pytest.mark.parametrize("service", ["trader", "strategy", "dashboard"])
def test_check_mount_fails_when_the_ai_author_key_is_mounted_outside_ai(tmp_path, service):
    out = io.StringIO()
    code = keys_main(_container_view(tmp_path, service, extra=("ai_author.key",)), in_container=lambda: True,
                     out=out)
    assert code == 1
    assert "unexpected ai_author.key" in out.getvalue()


def test_check_mount_passes_for_the_ai_service_with_its_three_pairs(tmp_path):
    out = io.StringIO()
    code = keys_main(_container_view(tmp_path, "ai"), in_container=lambda: True, out=out)
    assert code == 0, out.getvalue()


def test_the_key_check_gate_covers_the_ai_service_and_the_trader():
    line = next(line for line in (ROOT / "docker.sh").read_text().splitlines()
                if line.startswith("KEYCHECK_SERVICES="))
    assert {"ai", "trader"} <= set(line.split("=", 1)[1].strip('"').split())
