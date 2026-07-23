"""Paper-trading guardrails in the mmr-skill helper layer.

The 2026-07-23 paper LLM evaluate-then-approve spec requires the checklist
(``proposal_show`` → ``portfolio_risk`` → decide) before any paper approve.
The server cannot see whether the LLM actually evaluated, so the helper layer
enforces it client-side: ``approve()`` refuses with CHECKLIST_INCOMPLETE until
both checklist calls happened recently. ``propose()`` additionally dedupes
against existing PENDING proposals (the server only dedupes ``strategy:``
sources) and exposes the CLI's protective-exit flags.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

_HELPERS_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills" / "mmr-skill" / "scripts" / "mmr_helpers.py"
)


@pytest.fixture()
def helpers(monkeypatch):
    """Import mmr_helpers fresh and stub its CLI runners.

    Returns a namespace with the module, the MMRHelpers class, and the
    recorded CLI calls. ``json_router`` maps the first CLI arg to a canned
    response for ``_run_cli_json``.
    """
    spec = importlib.util.spec_from_file_location("mmr_helpers_under_test", _HELPERS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = {"cli": [], "cli_json": []}
    json_router = {
        "proposals": {"data": [], "title": "Proposals"},
        "propose": {"data": {"proposal_id": 77}, "title": "Proposal"},
        "portfolio-risk": {"data": {"hhi": 0.05, "warnings": []}},
    }

    async def fake_run_cli(*args, timeout=30):
        calls["cli"].append(args)
        return f"OK {' '.join(args)}"

    async def fake_run_cli_json(*args, timeout=30):
        calls["cli_json"].append(args)
        return json_router.get(args[0], {"data": {}})

    monkeypatch.setattr(mod, "_run_cli", fake_run_cli)
    monkeypatch.setattr(mod, "_run_cli_json", fake_run_cli_json)

    class NS:
        pass

    ns = NS()
    ns.mod = mod
    ns.H = mod.MMRHelpers
    ns.calls = calls
    ns.json_router = json_router
    return ns


def _cli_commands(ns):
    return [c[0] for c in ns.calls["cli"]]


def _cli_json_commands(ns):
    return [c[0] for c in ns.calls["cli_json"]]


# ---------------------------------------------------------------------------
# approve(): enforced evaluate-then-approve checklist
# ---------------------------------------------------------------------------

def test_approve_refused_without_checklist(helpers):
    result = asyncio.run(helpers.H.approve(42))

    assert "CHECKLIST_INCOMPLETE" in result
    assert "proposal_show(42)" in result
    assert "portfolio_risk()" in result
    # The refusal is client-side: the approve CLI must never have run.
    assert "approve" not in _cli_commands(helpers)


def test_approve_allowed_after_checklist(helpers):
    async def flow():
        await helpers.H.proposal_show(42)
        await helpers.H.portfolio_risk()
        return await helpers.H.approve(42)

    result = asyncio.run(flow())

    assert "CHECKLIST_INCOMPLETE" not in result
    assert ("approve", "42") in helpers.calls["cli"]


def test_approve_requires_show_of_that_proposal(helpers):
    async def flow():
        await helpers.H.proposal_show(41)  # a different proposal
        await helpers.H.portfolio_risk()
        return await helpers.H.approve(42)

    result = asyncio.run(flow())

    assert "CHECKLIST_INCOMPLETE" in result
    assert "approve" not in _cli_commands(helpers)


def test_approve_checklist_evidence_expires(helpers):
    async def flow():
        await helpers.H.proposal_show(42)
        await helpers.H.portfolio_risk()

    asyncio.run(flow())
    # Advance the module clock past the freshness window.
    base = helpers.mod._now()
    helpers.mod._now = lambda: base + helpers.mod._EVAL_WINDOW_S + 1

    result = asyncio.run(helpers.H.approve(42))

    assert "CHECKLIST_INCOMPLETE" in result
    assert "approve" not in _cli_commands(helpers)


def test_failed_risk_check_does_not_count(helpers):
    helpers.json_router["portfolio-risk"] = {"data": None, "error": "timed out", "timed_out": True}

    async def flow():
        await helpers.H.proposal_show(42)
        await helpers.H.portfolio_risk()
        return await helpers.H.approve(42)

    result = asyncio.run(flow())

    assert "CHECKLIST_INCOMPLETE" in result
    assert "portfolio_risk()" in result


def test_reject_is_never_gated(helpers):
    result = asyncio.run(helpers.H.reject(42, reason="stale thesis"))

    assert "CHECKLIST_INCOMPLETE" not in result
    assert ("reject", "42", "--reason", "stale thesis") in helpers.calls["cli"]


# ---------------------------------------------------------------------------
# propose(): client-side dedupe against PENDING proposals
# ---------------------------------------------------------------------------

def _pending_row(symbol="AAPL", action="BUY", pid=9):
    return {"id": pid, "symbol": symbol, "action": action, "storage_status": "PENDING"}


def test_propose_duplicate_pending_refused(helpers):
    helpers.json_router["proposals"] = {"data": [_pending_row("AAPL", "BUY", pid=9)]}

    result = asyncio.run(helpers.H.propose("AAPL", "BUY", amount=500.0))

    assert result.get("error_code") == "DUPLICATE_PENDING"
    assert result.get("existing_proposal_id") == 9
    assert "propose" not in _cli_json_commands(helpers)


def test_propose_different_side_is_not_a_duplicate(helpers):
    helpers.json_router["proposals"] = {"data": [_pending_row("AAPL", "SELL")]}

    result = asyncio.run(helpers.H.propose("AAPL", "BUY", amount=500.0))

    assert result.get("error_code") != "DUPLICATE_PENDING"
    assert "propose" in _cli_json_commands(helpers)


def test_propose_allow_duplicate_bypasses(helpers):
    helpers.json_router["proposals"] = {"data": [_pending_row("AAPL", "BUY")]}

    result = asyncio.run(helpers.H.propose("AAPL", "BUY", amount=500.0, allow_duplicate=True))

    assert result.get("error_code") != "DUPLICATE_PENDING"
    assert "propose" in _cli_json_commands(helpers)


def test_propose_dedupe_check_fails_open(helpers):
    helpers.json_router["proposals"] = {"data": None, "error": "timed out", "timed_out": True}

    result = asyncio.run(helpers.H.propose("AAPL", "BUY", amount=500.0))

    # A broken pending-list read must not block proposal creation (which is
    # itself safe — nothing executes without approve).
    assert "propose" in _cli_json_commands(helpers)
    assert result.get("error_code") != "DUPLICATE_PENDING"


# ---------------------------------------------------------------------------
# propose(): protective-exit flags
# ---------------------------------------------------------------------------

def _propose_args(ns):
    return next(c for c in ns.calls["cli_json"] if c[0] == "propose")


def test_propose_bracket_maps_tp_and_sl(helpers):
    asyncio.run(helpers.H.propose(
        "AAPL", "BUY", amount=500.0, take_profit=210.0, stop_loss=180.0))

    args = _propose_args(helpers)
    i = args.index("--bracket")
    assert args[i + 1] == "210.0" and args[i + 2] == "180.0"


def test_propose_stop_loss_only(helpers):
    asyncio.run(helpers.H.propose("AAPL", "BUY", amount=500.0, stop_loss=180.0))

    args = _propose_args(helpers)
    assert "--stop-loss" in args and args[args.index("--stop-loss") + 1] == "180.0"
    assert "--bracket" not in args


def test_propose_trailing_stop_and_tif(helpers):
    asyncio.run(helpers.H.propose(
        "AAPL", "BUY", amount=500.0, trailing_stop_pct=2.0, tif="GTC"))

    args = _propose_args(helpers)
    assert "--trailing-stop-pct" in args
    assert args[args.index("--trailing-stop-pct") + 1] == "2.0"
    assert args[args.index("--tif") + 1] == "GTC"


def test_propose_take_profit_alone_is_refused(helpers):
    result = asyncio.run(helpers.H.propose("AAPL", "BUY", amount=500.0, take_profit=210.0))

    assert result.get("error_code") == "INVALID_EXIT"
    assert "propose" not in _cli_json_commands(helpers)


def test_propose_trailing_conflicts_with_fixed_exits(helpers):
    result = asyncio.run(helpers.H.propose(
        "AAPL", "BUY", amount=500.0, trailing_stop_pct=2.0, stop_loss=180.0))

    assert result.get("error_code") == "INVALID_EXIT"
    assert "propose" not in _cli_json_commands(helpers)
