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


# ---------------------------------------------------------------------------
# snapshot helpers: --source is always forwarded so the CLI config default cannot win
# ---------------------------------------------------------------------------

def test_snapshot_forwards_ib_source_by_default(helpers):
    asyncio.run(helpers.H.snapshot("BHP", exchange="ASX", currency="AUD"))

    args = helpers.calls["cli_json"][-1]
    assert args[args.index("--source") + 1] == "ib"


def test_snapshots_batch_forwards_ib_source_by_default(helpers):
    asyncio.run(helpers.H.snapshots_batch(["BHP", "CBA"], exchange="ASX", currency="AUD"))

    args = helpers.calls["cli_json"][-1]
    assert args[args.index("--source") + 1] == "ib"


def test_snapshot_forwards_explicit_rest_source(helpers):
    asyncio.run(helpers.H.snapshot("AAPL", source="alpaca"))

    args = helpers.calls["cli_json"][-1]
    assert args[args.index("--source") + 1] == "alpaca"


# ---------------------------------------------------------------------------
# ideas(): the provider default comes from the CLI, not the helper
# ---------------------------------------------------------------------------

def test_ideas_leaves_source_to_cli_default(helpers):
    asyncio.run(helpers.H.ideas("momentum", tickers=["AAPL"]))
    args = helpers.calls["cli_json"][-1]
    assert args[0] == "ideas" and "--source" not in args


def test_ideas_passes_explicit_source(helpers):
    asyncio.run(helpers.H.ideas("momentum", source="massive"))
    args = helpers.calls["cli_json"][-1]
    assert args[args.index("--source") + 1] == "massive"


# ---------------------------------------------------------------------------
# forex_convert(): free default unless a source is named
# ---------------------------------------------------------------------------

def test_forex_convert_uses_cli_default_source(helpers):
    asyncio.run(helpers.H.forex_convert("EUR", "USD", 100.0))
    assert helpers.calls["cli"][-1] == ("forex", "convert", "EUR", "USD", "100.0")


def test_forex_convert_passes_explicit_source(helpers):
    asyncio.run(helpers.H.forex_convert("EUR", "USD", 100.0, source="massive"))
    assert helpers.calls["cli"][-1] == ("forex", "convert", "EUR", "USD", "100.0", "--source", "massive")


def _straddle_rows(provider, feed):
    return {'data': [
        {'type': 'call', 'strike': 100.0, 'bid': 2.0, 'ask': 2.2, 'mid': 2.1, 'provider': provider, 'feed': feed},
        {'type': 'put', 'strike': 100.0, 'bid': 1.8, 'ask': 2.0, 'mid': 1.9, 'provider': provider, 'feed': feed},
    ], 'title': 'Options Chain'}


def _implied_move(helpers, monkeypatch, provider, feed):
    import datetime as dt

    async def spot(symbol):
        return 100.0

    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', spot)
    helpers.json_router['options'] = _straddle_rows(provider, feed)
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    return asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration, prefer='polygon'))


def test_implied_move_labels_indicative_chain(helpers, monkeypatch):
    result = _implied_move(helpers, monkeypatch, 'alpaca', 'indicative')
    assert result['method'] == 'atm_straddle'
    assert (result['provider'], result['feed'], result['confidence']) == ('alpaca', 'indicative', 'medium')
    assert 'not OPRA NBBO' in result['notes']
    assert result['implied_move_pct'] == pytest.approx(4.0)


def test_implied_move_opra_chain_keeps_high_confidence(helpers, monkeypatch):
    result = _implied_move(helpers, monkeypatch, 'massive', 'opra')
    assert (result['method'], result['provider'], result['confidence']) == ('atm_straddle', 'massive', 'high')


NOT_CONFIGURED = ('alpaca is not configured: set alpaca_api_key_id (env ALPACA_API_KEY_ID), '
                  'alpaca_api_secret_key (env ALPACA_API_SECRET_KEY) in trader.yaml or the environment')


@pytest.mark.parametrize('prefer', ['auto', 'polygon'])
def test_implied_move_returns_the_cli_error_instead_of_realized_vol(helpers, monkeypatch, prefer):
    import datetime as dt

    async def spot(symbol):
        return 100.0

    async def no_history(symbol, days):
        raise AssertionError('realized-vol fallback must not run when the chain call failed')

    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', spot)
    monkeypatch.setattr(helpers.mod, '_daily_closes', no_history)
    helpers.json_router['options'] = {'success': False, 'message': NOT_CONFIGURED}
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()

    result = asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration, prefer=prefer))

    assert result['error'] == NOT_CONFIGURED
    assert result['implied_move_pct'] is None


def test_implied_move_auto_still_falls_back_when_not_authorized(helpers, monkeypatch):
    import datetime as dt

    async def spot(symbol):
        return 100.0

    async def closes(symbol, days):
        return [100.0 + (i % 3) for i in range(40)], 'local'

    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', spot)
    monkeypatch.setattr(helpers.mod, '_daily_closes', closes)
    helpers.json_router['options'] = {
        'success': False,
        'message': 'massive refused options chain (NOT_AUTHORIZED: no options data); use --source alpaca'}
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()

    result = asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration))

    assert result['method'] == 'realized_vol'
    assert 'NOT_AUTHORIZED' in result['fallback_reason']


EMPTY_CHAIN = 'No chain data for AAPL expiring 2099-01-01 from alpaca'


def _implied_move_with_empty_chain(helpers, monkeypatch, prefer):
    import datetime as dt

    async def spot(symbol):
        return 100.0

    async def closes(symbol, days):
        return [100.0 + (i % 3) for i in range(40)], 'local'

    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', spot)
    monkeypatch.setattr(helpers.mod, '_daily_closes', closes)
    helpers.json_router['options'] = {'success': False, 'message': EMPTY_CHAIN}
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    return asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration, prefer=prefer))


def test_implied_move_auto_falls_back_to_realized_vol_for_an_empty_chain(helpers, monkeypatch):
    result = _implied_move_with_empty_chain(helpers, monkeypatch, 'auto')

    assert result['method'] == 'realized_vol'
    assert result['fallback_reason'] == EMPTY_CHAIN
    assert 'error' not in result


def test_implied_move_chain_only_reports_an_empty_chain_as_error(helpers, monkeypatch):
    result = _implied_move_with_empty_chain(helpers, monkeypatch, 'polygon')

    assert result['error'] == EMPTY_CHAIN
    assert result['implied_move_pct'] is None



def test_implied_move_unknown_feed_is_medium_confidence(helpers, monkeypatch):
    result = _implied_move(helpers, monkeypatch, 'alpaca', None)
    assert (result['method'], result['feed'], result['confidence']) == ('atm_straddle', 'unknown', 'medium')


def _implied_move_auto(helpers, monkeypatch, chain_reply, spot=100.0):
    import datetime as dt

    async def last_close(symbol):
        return spot

    async def closes(symbol, days):
        return [100.0 + (i % 3) for i in range(40)], 'local'

    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', last_close)
    monkeypatch.setattr(helpers.mod, '_daily_closes', closes)
    helpers.json_router['options'] = chain_reply
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    return asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration))


def test_implied_move_auto_says_why_when_spot_is_missing(helpers, monkeypatch):
    result = _implied_move_auto(helpers, monkeypatch, _straddle_rows('alpaca', 'indicative'), spot=None)
    assert result['method'] == 'realized_vol'
    assert 'spot' in result['fallback_reason']
    assert ('options', 'chain') not in [c[:2] for c in helpers.calls['cli_json']]


def test_implied_move_auto_says_why_when_no_strike_has_call_and_put(helpers, monkeypatch):
    calls_only = {'data': [r for r in _straddle_rows('alpaca', 'indicative')['data'] if r['type'] == 'call']}
    result = _implied_move_auto(helpers, monkeypatch, calls_only)
    assert result['method'] == 'realized_vol'
    assert 'call and put' in result['fallback_reason']


def test_implied_move_auto_says_why_when_the_chain_call_times_out(helpers, monkeypatch):
    timed_out = {'data': None, 'error': 'timed out after 30s', 'timed_out': True}
    result = _implied_move_auto(helpers, monkeypatch, timed_out)
    assert result['method'] == 'realized_vol'
    assert 'timed out after 30s' in result['fallback_reason']
    assert 'call and put' not in result['fallback_reason']


def test_implied_move_auto_says_why_when_the_reply_is_not_json(helpers, monkeypatch):
    garbage = {'data': 'Traceback (most recent call last): ...', 'title': None, 'error': 'Failed to parse JSON'}
    result = _implied_move_auto(helpers, monkeypatch, garbage)
    assert result['method'] == 'realized_vol'
    assert 'Failed to parse JSON' in result['fallback_reason']


def test_implied_move_auto_says_why_when_the_chain_step_raises(helpers, monkeypatch):
    async def broken_spot(symbol):
        raise RuntimeError('duckdb locked')

    async def closes(symbol, days):
        return [100.0 + (i % 3) for i in range(40)], 'local'

    import datetime as dt
    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', broken_spot)
    monkeypatch.setattr(helpers.mod, '_daily_closes', closes)
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    result = asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration))
    assert result['method'] == 'realized_vol'
    assert 'RuntimeError: duckdb locked' in result['fallback_reason']


@pytest.mark.parametrize('call, expected', [
    (lambda h, s: h.options_expirations('AAPL', source=s), ('options', 'expirations', 'AAPL')),
    (lambda h, s: h.options_chain('AAPL', expiration='2026-11-20', source=s),
     ('options', 'chain', 'AAPL', '-e', '2026-11-20')),
    (lambda h, s: h.options_snapshot('AAPL261120C00250000', source=s),
     ('options', 'snapshot', 'AAPL261120C00250000')),
    (lambda h, s: h.options_implied('AAPL', '2026-11-20', source=s),
     ('options', 'implied', 'AAPL', '-e', '2026-11-20', '--risk-free-rate', '0.05')),
])
def test_options_wrappers_pass_source_only_when_set(helpers, call, expected):
    asyncio.run(call(helpers.H, None))
    assert helpers.calls['cli'][-1] == expected

    asyncio.run(call(helpers.H, 'massive'))
    assert helpers.calls['cli'][-1] == expected + ('--source', 'massive')

def _preflight_with(helpers, expirations, chain):
    helpers.json_router['status'] = {'data': {'connected': False}}
    helpers.json_router['options'] = None

    async def route(*args, timeout=30):
        if args[0] == 'options':
            return expirations if args[1] == 'expirations' else chain
        return helpers.json_router.get(args[0], {'data': {}})

    helpers.mod._run_cli_json = route
    return asyncio.run(helpers.H.preflight())


def test_preflight_empty_probe_range_means_chain_is_reachable(helpers):
    report = _preflight_with(
        helpers,
        {'data': [{'expiration': '2026-11-20', 'DTE': 47}], 'title': 'Expirations: QQQ (alpaca)', 'provider': 'alpaca'},
        {'success': False, 'message': 'No chain data for QQQ from alpaca'},
    )
    options = report['polygon_options']
    assert options['chain_endpoint'] is True
    assert (options['provider'], options['feed']) == ('alpaca', 'indicative')
    assert not any('NOT_AUTHORIZED' in rec or 'Polygon' in rec for rec in report['recommendations'])


def test_preflight_not_configured_is_unavailable_and_says_why(helpers):
    report = _preflight_with(
        helpers,
        {'success': False, 'message': NOT_CONFIGURED},
        {'success': False, 'message': NOT_CONFIGURED},
    )
    options = report['polygon_options']
    assert options['chain_endpoint'] is False
    assert options['tier'] == 'none'
    assert any('ALPACA_API_KEY_ID' in rec for rec in report['recommendations'])
    assert not any('NOT_AUTHORIZED' in rec for rec in report['recommendations'])


def test_preflight_not_authorized_names_the_provider_and_alpaca(helpers):
    message = 'massive refused options chain (NOT_AUTHORIZED: this Massive plan has no options data); use --source alpaca'
    report = _preflight_with(
        helpers,
        {'data': [{'expiration': '2026-11-20', 'DTE': 47}], 'title': 'Expirations: QQQ (massive)', 'provider': 'massive'},
        {'success': False, 'message': message},
    )
    assert report['polygon_options']['chain_endpoint'] is False
    assert report['polygon_options']['tier'] == 'free'
    assert any('NOT_AUTHORIZED' in rec and '--source alpaca' in rec for rec in report['recommendations'])
