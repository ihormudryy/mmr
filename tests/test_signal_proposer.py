"""Tests for the signal → PENDING proposal bridge (``auto_execute: propose``).

[M1-F3] Task 8: ``SignalProposer`` is a thin typed adapter over the
command-authority coordinator now -- it holds no ``ProposalStore`` handle
and creates proposals ONLY through the typed ``create_proposal`` command,
reached via a fake ``TypedRpcClient`` double (``FakeTypedClient``, serving
both the ``command`` and ``query`` roles). Sizing, dedup, expiry, and
quote/risk checks all moved server-side (``ProposalCommandService``); these
tests cover what's left here: gating (paper-only, pause-aware), signal→body
translation, and the StrategyRuntime dispatch integration points (conid
stamping via ``_dispatch_signal``, load-time rejection of unsupported
``auto_execute`` values) which are unaffected by the adapter rewrite.

Spec: docs/superpowers/specs/2026-07-15-signal-propose-bridge-design.md
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from trader.domain.commands import CommandReceipt
from trader.objects import Action
from trader.strategy.signal_proposer import SignalProposer
from trader.strategy.strategy_runtime import StrategyRuntime
from trader.trading.strategy import Signal


# ---------------------------------------------------------------------------
# Fake typed RPC client (mirrors tests/test_sdk.py's FakeTypedClient --
# duplicated here since each test file in this task's edit scope is
# self-contained; no shared conftest fixture was added).
# ---------------------------------------------------------------------------

@dataclass
class _RecordedTypedCall:
    method: str
    body: dict


class FakeTypedClient:
    """Serves BOTH the ``query`` and ``command`` roles from one instance --
    ``SignalProposer(command_client=typed, query_client=typed, ...)`` wires
    the SAME fake into both constructor params, exactly like the real
    trader_service exposes both roles on the same host (different ports)."""

    def __init__(self):
        self._query_queue: dict = {}
        self._command_queue: dict = {}
        self.queries: list[_RecordedTypedCall] = []
        self.commands: list[_RecordedTypedCall] = []
        self.store_writes: list = []

    def queue_query(self, method, response):
        self._query_queue.setdefault(method, []).append(('ok', response))

    def queue_command(self, method, receipt):
        self._command_queue.setdefault(method, []).append(('ok', receipt))

    def fail_next_query(self, method, exc):
        self._query_queue.setdefault(method, []).append(('err', exc))

    def fail_next_command(self, method, exc):
        self._command_queue.setdefault(method, []).append(('err', exc))

    def call(self, method, body, response_model=None, timeout=None):
        if self._command_queue.get(method):
            kind, payload = self._command_queue[method].pop(0)
            self.commands.append(_RecordedTypedCall(method=method, body=dict(body)))
            if kind == 'err':
                raise payload
            return payload
        if self._query_queue.get(method):
            kind, payload = self._query_queue[method].pop(0)
            self.queries.append(_RecordedTypedCall(method=method, body=dict(body)))
            if kind == 'err':
                raise payload
            return payload
        raise AssertionError(f'FakeTypedClient.call({method!r}, ...) with no queued response')


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _frame(n=30, start='2026-07-15 09:31', freq='1min', last_time=None):
    """OHLCV frame of n 1-min bars. If last_time is given, the index is laid
    out so the final bar lands exactly on that timestamp."""
    if last_time is not None:
        end = pd.Timestamp(last_time)
        idx = pd.date_range(end=end, periods=n, freq=freq)
    else:
        idx = pd.date_range(start=start, periods=n, freq=freq)
    base = 100.0
    return pd.DataFrame({
        'open': base, 'high': base + 1.0, 'low': base - 1.0,
        'close': base + 0.5, 'volume': 10_000,
    }, index=idx.rename('date'))


def _signal(action=Action.BUY, conid=4391, probability=0.6, **kwargs):
    return Signal(source_name='orb_test', action=action, probability=probability,
                  risk=0.4, conid=conid, **kwargs)


@pytest.fixture
def typed():
    return FakeTypedClient()


@pytest.fixture
def proposer(typed):
    return SignalProposer(
        command_client=typed,
        query_client=typed,
        paper_trading=True,
        account_id='DU111111',
        proposal_ttl_minutes=30,
    )


# ---------------------------------------------------------------------------
# on_signal: BUY path (gated by the pause check) + SELL path (exempt, §9.4)
# ---------------------------------------------------------------------------

class TestOnSignalCreatesViaTypedApi:
    def test_signal_proposer_creates_via_typed_api(self, proposer, typed):
        typed.queue_query('get_trading_control', {'new_exposure_paused': False, 'revision': 1})
        typed.queue_command('create_proposal', CommandReceipt(
            's1', 's1', 'RESOLVED', {'proposal_id': 41, 'revision': 1}, None, False))
        pid = proposer.on_signal('orb', _signal(conid=265598, action=Action.BUY,
                                                probability=0.8), _frame())
        assert pid == 41
        body = typed.commands[0].body
        assert body['conid'] == 265598 and body['source'] == 'strategy:orb'
        assert body['command_id'].startswith('strategy-')
        assert body['action'] == 'BUY'
        assert body['confidence'] == 0.8

    def test_sell_signal_is_exempt_from_the_pause_gate(self, proposer, typed):
        """§9.4: SELL (position-reducing) never checks get_trading_control at
        all -- only a BUY (new exposure) does."""
        typed.queue_command('create_proposal', CommandReceipt(
            's2', 's2', 'RESOLVED', {'proposal_id': 7, 'revision': 1}, None, False))
        pid = proposer.on_signal('orb', _signal(action=Action.SELL), _frame())
        assert pid == 7
        assert typed.queries == []

    def test_server_refusal_returns_none(self, proposer, typed):
        typed.queue_query('get_trading_control', {'new_exposure_paused': False, 'revision': 1})
        typed.queue_command('create_proposal', CommandReceipt(
            's3', 's3', 'REJECTED', None, 'DUPLICATE_PENDING', False))
        assert proposer.on_signal('orb', _signal(action=Action.BUY), _frame()) is None

    def test_create_proposal_rpc_failure_returns_none(self, proposer, typed):
        typed.queue_query('get_trading_control', {'new_exposure_paused': False, 'revision': 1})
        typed.fail_next_command('create_proposal', ConnectionError('trader down'))
        assert proposer.on_signal('orb', _signal(action=Action.BUY), _frame()) is None

    def test_unstamped_conid_creates_no_proposal_without_any_rpc(self, proposer, typed):
        pid = proposer.on_signal('orb', _signal(conid=0), _frame())
        assert pid is None
        assert typed.commands == [] and typed.queries == []

    def test_unknown_action_returns_none(self, proposer, typed):
        sig = _signal()
        sig.action = None
        assert proposer.on_signal('orb', sig, _frame()) is None
        assert typed.commands == [] and typed.queries == []


def test_signal_proposer_suppresses_entries_when_paused_stale_or_unavailable(proposer, typed):
    typed.queue_query('get_trading_control', {'new_exposure_paused': True, 'revision': 2})
    assert proposer.on_signal('orb', _signal(action=Action.BUY), _frame()) is None
    typed.fail_next_query('get_trading_control', ConnectionError('trader down'))
    assert proposer.on_signal('orb', _signal(action=Action.BUY), _frame()) is None  # fail closed
    # Verified exit proposals remain allowed while paused (§9.4).
    typed.queue_query('get_trading_control', {'new_exposure_paused': True, 'revision': 2})
    typed.queue_command('create_proposal', CommandReceipt(
        's2', 's2', 'RESOLVED', {'proposal_id': 42, 'revision': 1}, None, False))
    assert proposer.on_signal('orb', _signal(action=Action.SELL), _frame()) == 42


# ---------------------------------------------------------------------------
# Gating: paper-only
# ---------------------------------------------------------------------------

class TestGating:
    def test_live_mode_is_noop(self, typed):
        proposer = SignalProposer(
            command_client=typed, query_client=typed,
            paper_trading=False, account_id='DU111111',
        )
        pid = proposer.on_signal('orb', _signal(Action.BUY), _frame())
        assert pid is None
        assert typed.commands == [] and typed.queries == []


# ---------------------------------------------------------------------------
# expire_stale: now trader-owned; this bridge is an inert no-op
# ---------------------------------------------------------------------------

class TestExpireStaleIsNowServerOwned:
    def test_expire_stale_is_an_inert_noop(self, proposer, typed):
        """[M1-F3] Task 2 moved expiry ownership to the trader service
        (``ProposalCommandService.run_expiry_loop``, which trader_service
        already runs on its own timer against the journal DB it owns). This
        bridge has no ``ProposalStore`` handle to sweep with anymore --
        ``expire_stale`` is a documented no-op, kept only so
        ``strategy_runtime._reconcile()``'s existing call site doesn't
        require touching."""
        assert proposer.expire_stale() == []
        assert typed.commands == [] and typed.queries == []


# ---------------------------------------------------------------------------
# check_exits: reads executed bridge entries via list_proposals
# ---------------------------------------------------------------------------

class TestCheckExits:
    def test_reads_executed_entries_via_list_proposals(self, proposer, typed):
        typed.queue_query('list_proposals', {'proposals': [
            {'id': 5, 'conid': 4391, 'action': 'BUY', 'source': 'strategy:orb'},
        ]})
        # _exit_reason is a documented no-op today (see its docstring: the
        # typed wire has nowhere to persist/echo max_hold_bars/close_by_time
        # back through list_proposals) -- this pins the current, honest
        # behaviour: the entry is found and considered, but no exit fires.
        assert proposer.check_exits('orb', 4391, _frame()) is None
        assert typed.commands == []

    def test_empty_frame_short_circuits_without_a_query(self, proposer, typed):
        assert proposer.check_exits('orb', 4391, pd.DataFrame()) is None
        assert typed.queries == []

    def test_list_proposals_failure_is_swallowed(self, proposer, typed):
        typed.fail_next_query('list_proposals', ConnectionError('down'))
        assert proposer.check_exits('orb', 4391, _frame()) is None

    def test_live_mode_short_circuits_without_a_query(self, typed):
        proposer = SignalProposer(
            command_client=typed, query_client=typed,
            paper_trading=False, account_id='DU111111',
        )
        assert proposer.check_exits('orb', 4391, _frame()) is None
        assert typed.queries == []


# ---------------------------------------------------------------------------
# StrategyRuntime integration: conid stamping + load-time validation
# (unaffected by the SignalProposer rewrite -- on_signal/check_exits keep
# the same public (strategy_name, signal, frame) / (strategy_name, conid,
# frame) shapes, so the runtime's dispatch plumbing doesn't change).
# ---------------------------------------------------------------------------

class _RecordingEventStore:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


class _RecordingBus:
    def __init__(self):
        self.written = []

    def write(self, topic, payload):
        self.written.append((topic, payload))


class _RecordingProposer:
    def __init__(self):
        self.signals = []
        self.exit_checks = []

    def on_signal(self, strategy_name, signal, frame):
        self.signals.append((strategy_name, signal, frame))
        return 1

    def check_exits(self, strategy_name, conid, frame):
        self.exit_checks.append((strategy_name, conid, frame))
        return None


def _make_runtime(tmp_path, paper_trading=True) -> StrategyRuntime:
    rt = StrategyRuntime.__new__(StrategyRuntime)  # skip __init__
    rt.strategies_directory = str(tmp_path)
    rt.strategy_config_file = str(tmp_path / 'strategy_runtime.yaml')
    rt.strategy_implementations = []
    rt.strategies = {}
    rt.streams = {}
    rt.storage = None  # type: ignore
    rt.universe_accessor = None  # type: ignore
    rt._config_mtime = 0.0
    rt.trader_client = None  # type: ignore
    rt.paper_trading = paper_trading
    rt.event_store = _RecordingEventStore()  # type: ignore
    rt.zmq_messagebus_client = _RecordingBus()  # type: ignore
    rt.signal_proposer = _RecordingProposer()  # type: ignore
    return rt


_STRATEGY_BODY = """
from trader.trading.strategy import Strategy, Signal
from trader.objects import Action

class Probe(Strategy):
    def on_prices(self, prices):
        return None
"""


def _write_strategy(tmp_path: Path, name: str) -> None:
    (tmp_path / f'{name}.py').write_text(_STRATEGY_BODY)


class TestRuntimeIntegration:
    def test_dispatch_signal_stamps_conid(self, tmp_path, installed_strategy):
        rt = _make_runtime(tmp_path)
        sig = Signal(source_name='s', action=Action.BUY, probability=0.5, risk=0.5)
        assert sig.conid == 0
        rt._dispatch_signal(installed_strategy, sig, conId=4391, frame=_frame())
        assert sig.conid == 4391
        assert rt.event_store.events[0].conid == 4391
        assert rt.zmq_messagebus_client.written[0][0] == 'signal'

    def test_dispatch_signal_calls_proposer_in_propose_mode(
            self, tmp_path, installed_strategy):
        rt = _make_runtime(tmp_path)
        installed_strategy.ctx.auto_execute = 'propose'
        sig = Signal(source_name='s', action=Action.BUY, probability=0.5, risk=0.5)
        rt._dispatch_signal(installed_strategy, sig, conId=4391, frame=_frame())
        assert len(rt.signal_proposer.signals) == 1
        name, passed_sig, _ = rt.signal_proposer.signals[0]
        assert name == installed_strategy.name
        assert passed_sig.conid == 4391

    def test_dispatch_signal_skips_proposer_when_off(self, tmp_path, installed_strategy):
        rt = _make_runtime(tmp_path)
        installed_strategy.ctx.auto_execute = False
        sig = Signal(source_name='s', action=Action.BUY, probability=0.5, risk=0.5)
        rt._dispatch_signal(installed_strategy, sig, conId=4391, frame=_frame())
        assert rt.signal_proposer.signals == []

    def test_maybe_check_exits_in_propose_mode(self, tmp_path, installed_strategy):
        rt = _make_runtime(tmp_path)
        installed_strategy.ctx.auto_execute = 'propose'
        rt._maybe_check_exits(installed_strategy, conId=4391, frame=_frame())
        assert len(rt.signal_proposer.exit_checks) == 1

    def test_maybe_check_exits_noop_when_off(self, tmp_path, installed_strategy):
        rt = _make_runtime(tmp_path)
        rt._maybe_check_exits(installed_strategy, conId=4391, frame=_frame())
        assert rt.signal_proposer.exit_checks == []

    def test_load_strategy_rejects_auto_execute_true(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _write_strategy(tmp_path, 'probe')
        rt.load_strategy(
            name='probe', bar_size_str='1 min', conids=[4391], universe=None,
            historical_days_prior=5, module='probe.py', class_name='Probe',
            description='', auto_execute=True,
        )
        assert rt.strategy_implementations == []

    def test_load_strategy_accepts_propose_mode(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _write_strategy(tmp_path, 'probe')
        rt.load_strategy(
            name='probe', bar_size_str='1 min', conids=[4391], universe=None,
            historical_days_prior=5, module='probe.py', class_name='Probe',
            description='', auto_execute='propose',
        )
        assert len(rt.strategy_implementations) == 1
        assert rt.strategy_implementations[0].ctx.auto_execute == 'propose'

    def test_load_strategy_accepts_false(self, tmp_path):
        rt = _make_runtime(tmp_path)
        _write_strategy(tmp_path, 'probe')
        rt.load_strategy(
            name='probe', bar_size_str='1 min', conids=[4391], universe=None,
            historical_days_prior=5, module='probe.py', class_name='Probe',
            description='', auto_execute=False,
        )
        assert len(rt.strategy_implementations) == 1
