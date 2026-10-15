"""Issue #176: every service that runs as trader drops all capabilities, cannot gain privileges, and the image
holds no sudo. The sandbox keeps its own narrow capability set (SP3)."""
from __future__ import annotations

import re

from tests.compose_rpc_helpers import ROOT, load_compose


def _trader_services():
    services = load_compose()["services"]
    return {name: service for name, service in services.items() if service.get("user") == "trader"}


def test_every_trader_service_drops_all_capabilities_and_cannot_gain_privileges():
    services = _trader_services()
    assert {"trader", "strategy", "data", "dashboard", "scheduler", "ai", "research", "cli"} <= set(services)
    for name, service in services.items():
        assert service.get("cap_drop") == ["ALL"], name
        assert not service.get("cap_add"), name
        assert "no-new-privileges:true" in service.get("security_opt", []), name


def test_the_sandbox_keeps_its_own_narrow_capability_set():
    sandbox = load_compose()["services"]["sandbox"]
    assert sandbox["cap_drop"] == ["ALL"]
    assert sorted(sandbox["cap_add"]) == ["CHOWN", "KILL", "SETGID", "SETUID"]


def test_the_image_installs_no_sudo_and_trader_is_in_no_sudo_group():
    text = (ROOT / "Dockerfile").read_text()
    assert not re.search(r"(?m)^\s*[^#\n]*\bsudo\b[^\n]*\\$", text), "sudo is still in an apt-get install list"
    assert "-G sudo" not in text
