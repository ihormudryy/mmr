from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Generator

import pytest

from tests.paper_e2e._clients import DashboardClient, TypedRpc
from tests.paper_e2e._probe import capability_from_remote_error, probe_command_registered
from trader.messaging.typed_rpc import TypedRpcRemoteError


@dataclass
class PaperStack:
    dashboard_url: str
    capabilities: frozenset[str]
    typed: TypedRpc
    live_orders: bool
    restart: bool
    e2e_id: str
    allocation_armed: bool = False
    paper_automation_armed: bool = False
    opened_positions: dict[int, float] = field(default_factory=dict)

    def register_opened_position(self, conid: int, quantity: float) -> None:
        """Record an E2E-opened position for run-scoped closeout."""
        self.opened_positions[conid] = quantity


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = str(getattr(item, "fspath", "") or item.path)
        if "/tests/paper_e2e/" not in path.replace("\\", "/"):
            continue
        item.add_marker(pytest.mark.paper_e2e)
        if not any(m.name == "timeout" for m in item.iter_markers()):
            item.add_marker(pytest.mark.timeout(120))


@pytest.fixture(scope="session")
def e2e_id() -> str:
    return f"e2e_{os.getpid()}_{int(time.time())}"


@pytest.fixture(scope="session")
def dashboard_client() -> Generator[DashboardClient, None, None]:
    url = os.environ.get("MMR_PAPER_E2E_DASHBOARD_URL", "http://127.0.0.1:7424")
    client = DashboardClient(url.rstrip("/"))
    yield client
    client.close()


@pytest.fixture(scope="session")
def typed_rpc(paper_stack: PaperStack) -> TypedRpc:
    return paper_stack.typed


def _query_registered(typed: TypedRpc, method: str) -> bool:
    try:
        typed.query.call(method, {}, dict)
    except TypedRpcRemoteError as exc:
        return capability_from_remote_error(exc.code) == "present"
    return True


def _capabilities(typed: TypedRpc) -> frozenset[str]:
    """Probe only registration; command probes deliberately send `{}`."""
    capabilities: set[str] = set()
    if _query_registered(typed, "get_trading_control") and probe_command_registered(
        typed.command, "pause_trading"
    ):
        capabilities.add("trading_control")
    if (
        _query_registered(typed, "list_proposals")
        and probe_command_registered(typed.command, "create_proposal")
        and probe_command_registered(typed.command, "reject_proposal")
    ):
        capabilities.add("proposals")
    if probe_command_registered(typed.command, "approve_proposal"):
        capabilities.add("approval")
    if probe_command_registered(typed.command, "enable_strategy"):
        capabilities.add("strategy_control")
    if (
        probe_command_registered(typed.command, "activate_allocation")
        and probe_command_registered(typed.command, "suspend_allocation")
    ):
        capabilities.add("allocation")
    if (
        _query_registered(typed, "get_paper_automation_status")
        and probe_command_registered(typed.command, "activate_paper_automation")
        and probe_command_registered(typed.command, "deactivate_paper_automation")
    ):
        capabilities.add("paper_automation")
    return frozenset(capabilities)


def _is_paper_mode(status: dict[str, Any], typed: TypedRpc) -> bool:
    mode = (
        status.get("trading_mode")
        or status.get("account_mode")
        or status.get("mode")
        or os.environ.get("TRADING_MODE")
        or typed._configured_trading_mode()
    )
    return str(mode).lower() == "paper"


def _record_belongs_to_run(record: dict[str, Any], run_id: str) -> bool:
    return run_id in str(record)


def _teardown(stack: PaperStack) -> None:
    typed = stack.typed
    try:
        if "proposals" in stack.capabilities:
            proposals = typed.query.call("list_proposals", {"status": "PENDING", "limit": 200}, dict)
            for proposal in proposals.get("proposals", []):
                if _record_belongs_to_run(proposal, stack.e2e_id):
                    typed.command.call(
                        "reject_proposal",
                        {
                            "command_id": f"{stack.e2e_id}_reject_{proposal['id']}",
                            "proposal_id": proposal["id"],
                            "reason": "paper e2e teardown",
                        },
                        dict,
                    )
        universes = typed.query.call("list_universes", {}, dict)
        for universe in universes.get("universes", []):
            name = universe.get("name", "")
            if name == stack.e2e_id or name.startswith(f"{stack.e2e_id}_"):
                typed.command.call(
                    "delete_universe",
                    {"name": name},
                    dict,
                )
        if stack.live_orders and "approval" in stack.capabilities:
            for conid, quantity in stack.opened_positions.items():
                close_id = f"{stack.e2e_id}_close_{conid}"
                receipt = typed.command.call(
                    "create_proposal",
                    {
                        "command_id": close_id,
                        "conid": conid,
                        "action": "SELL" if quantity > 0 else "BUY",
                        "quantity": abs(quantity),
                        "reasoning": f"{stack.e2e_id} paper e2e position closeout",
                        "source": stack.e2e_id,
                    },
                    dict,
                )
                proposal = (receipt.get("outcome") or {}).get("id")
                if proposal is not None:
                    typed.command.call(
                        "approve_proposal",
                        {
                            "command_id": f"{close_id}_approve",
                            "proposal_id": proposal,
                        },
                        dict,
                    )
        if stack.paper_automation_armed:
            typed.command.call(
                "deactivate_paper_automation",
                {"command_id": f"{stack.e2e_id}_deactivate_automation"},
                dict,
            )
        if stack.allocation_armed:
            typed.command.call(
                "suspend_allocation",
                {"command_id": f"{stack.e2e_id}_suspend_allocation"},
                dict,
            )
    except Exception:
        # Teardown is best effort: preserve the original test result while
        # never broadening cleanup beyond this run's exact prefix.
        pass
    finally:
        typed._close()


@pytest.fixture(scope="session", autouse=True)
def paper_stack(
    e2e_id: str, dashboard_client: DashboardClient
) -> Generator[PaperStack, None, None]:
    if os.environ.get("MMR_PAPER_E2E") != "1":
        pytest.skip("set MMR_PAPER_E2E=1 or use scripts/paper_e2e.sh")
    try:
        response = dashboard_client.get("/healthz")
        if response.status_code != 200 or response.json()["ok"] is not True:
            pytest.skip("paper dashboard healthz is unavailable")
        typed = TypedRpc()
        status = typed.query.call("get_status", {}, dict)
        if not _is_paper_mode(status, typed):
            typed._close()
            pytest.skip("paper E2E requires paper trading mode")
        if status.get("ib_upstream_connected") is not True:
            typed._close()
            pytest.skip("IB upstream is disconnected; check IB Gateway/VNC")
        capabilities = _capabilities(typed)
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        pytest.skip(f"paper stack unavailable: {exc}")

    stack = PaperStack(
        dashboard_url=dashboard_client.base_url.__str__().rstrip("/"),
        capabilities=capabilities,
        typed=typed,
        live_orders=os.environ.get("MMR_PAPER_E2E_LIVE_ORDERS") == "1",
        restart=os.environ.get("MMR_PAPER_E2E_RESTART") == "1",
        e2e_id=e2e_id,
    )
    yield stack
    _teardown(stack)


@pytest.fixture
def require_capability(paper_stack: PaperStack):
    def _require(name: str) -> None:
        if name not in paper_stack.capabilities:
            pytest.skip(f"paper stack does not provide capability {name!r}")
    return _require
