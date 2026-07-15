"""Tests for the signal → PENDING proposal bridge (``auto_execute: propose``).

Covers the SignalProposer unit behaviour (proposal creation, dedup, TTL
expiry, long-only SELL semantics, time-based exit proposals) and the
StrategyRuntime integration points (conid stamping via _dispatch_signal,
load-time rejection of unsupported auto_execute values).

Spec: docs/superpowers/specs/2026-07-15-signal-propose-bridge-design.md
"""

import datetime as dt
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
from ib_async import Contract, PortfolioItem

from trader.data.data_access import SecurityDefinition
from trader.data.proposal_store import ProposalStore
from trader.objects import Action
from trader.strategy.signal_proposer import SignalProposer
from trader.strategy.strategy_runtime import StrategyRuntime
from trader.trading.position_sizing import PositionSizingConfig
from trader.trading.proposal import ProposalStatus
from trader.trading.strategy import Signal


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

ACCOUNT_VALUES = {
    'NetLiquidation': {'value': 100_000.0, 'currency': 'USD'},
    'AvailableFunds': {'value': 80_000.0, 'currency': 'USD'},
    'GrossPositionValue': {'value': 20_000.0, 'currency': 'USD'},
}


def _make_secdef(conid=4391, symbol='AMD', exchange='SMART',
                 primary='NASDAQ', currency='USD') -> SecurityDefinition:
    return SecurityDefinition(
        symbol=symbol, exchange=exchange, conId=conid,
        secType='STK', primaryExchange=primary,
        currency=currency, tradingClass=symbol,
        includeExpired=False, secIdType='', secId='',
        description='', minTick=0.01, orderTypes='',
        validExchanges='', priceMagnifier=1, longName='',
        category='', subcategory='', tradingHours='',
        timeZoneId='', liquidHours='', stockType='',
        minSize=1.0, sizeIncrement=1.0, suggestedSizeIncrement=1.0,
        bondType='', couponType='', callable=False, putable=False,
        coupon=0.0, convertable=False, maturity='', issueDate='',
        nextOptionDate='', nextOptionPartial=False, nextOptionType='',
        marketRuleIds='',
    )


def _make_portfolio_item(conid=4391, symbol='AMD', position=100.0):
    c = Contract(conId=conid, symbol=symbol, secType='STK', currency='USD')
    return PortfolioItem(
        account='DU123', contract=c, position=position,
        marketPrice=150.0, marketValue=position * 150.0,
        averageCost=140.0, unrealizedPNL=0.0, realizedPNL=0.0,
    )


class _FakeRpc:
    def __init__(self, owner):
        self._o = owner

    def get_account_values(self):
        if self._o.fail_account:
            raise ConnectionError('trader_service unreachable')
        return self._o.account_values

    def get_portfolio(self):
        if self._o.fail_portfolio:
            raise ConnectionError('trader_service unreachable')
        return self._o.portfolio_items

    def resolve_symbol(self, conid):
        return self._o.secdefs.get(conid, [])


class FakeTraderClient:
    def __init__(self, secdefs=None, portfolio=None, account_values=None):
        self.secdefs = secdefs or {}
        self.portfolio_items = portfolio or []
        self.account_values = account_values if account_values is not None else dict(ACCOUNT_VALUES)
        self.fail_account = False
        self.fail_portfolio = False

    def rpc(self, **kwargs):
        return _FakeRpc(self)


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
def trader_client():
    return FakeTraderClient(secdefs={4391: [_make_secdef()]})


@pytest.fixture
def proposer(proposal_store, trader_client):
    return SignalProposer(
        proposal_store=proposal_store,
        trader_client=trader_client,
        paper_trading=True,
        sizing_config=PositionSizingConfig(),
        proposal_ttl_minutes=30,
    )


# ---------------------------------------------------------------------------
# BUY path
# ---------------------------------------------------------------------------

class TestBuyPath:
    def test_buy_signal_creates_pending_proposal(self, proposer, proposal_store):
        pid = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert pid is not None
        p = proposal_store.get(pid)
        assert p is not None
        assert p.status == ProposalStatus.PENDING.value
        assert p.symbol == 'AMD'
        assert p.action == 'BUY'
        assert p.source == 'strategy:orb_test'
        assert p.amount is not None and p.amount > 0
        assert p.confidence == 0.6

    def test_proposal_metadata_records_strategy_conid_and_expiry(self, proposer, proposal_store):
        pid = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        p = proposal_store.get(pid)
        assert p.metadata['strategy'] == 'orb_test'
        assert p.metadata['conid'] == 4391
        assert 'expires_at' in p.metadata

    def test_buy_records_exit_conditions_in_metadata(self, proposer, proposal_store):
        sig = _signal(Action.BUY, max_hold_bars=180, close_by_time=dt.time(15, 45))
        pid = proposer.on_signal('orb_test', sig, _frame())
        p = proposal_store.get(pid)
        assert p.metadata['max_hold_bars'] == 180
        assert p.metadata['close_by_time'] == '15:45:00'

    def test_buy_dedup_while_pending(self, proposer, proposal_store):
        pid1 = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        pid2 = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert pid1 is not None
        assert pid2 is None
        assert len(proposal_store.query(status='PENDING')) == 1

    def test_sizing_blocked_creates_no_proposal(self, proposal_store, trader_client):
        proposer = SignalProposer(
            proposal_store=proposal_store,
            trader_client=trader_client,
            paper_trading=True,
            sizing_config=PositionSizingConfig(max_positions=0),
            proposal_ttl_minutes=30,
        )
        pid = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert pid is None
        assert proposal_store.query(status='PENDING') == []

    def test_account_state_unavailable_skips_buy(self, proposer, trader_client, proposal_store):
        trader_client.fail_account = True
        pid = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert pid is None
        assert proposal_store.query(status='PENDING') == []

    def test_unresolvable_conid_creates_no_proposal(self, proposer, proposal_store):
        pid = proposer.on_signal('orb_test', _signal(Action.BUY, conid=999), _frame())
        assert pid is None
        assert proposal_store.query(status='PENDING') == []

    def test_unstamped_conid_creates_no_proposal(self, proposer, proposal_store):
        pid = proposer.on_signal('orb_test', _signal(Action.BUY, conid=0), _frame())
        assert pid is None


# ---------------------------------------------------------------------------
# SELL path (long-only close, matching backtester semantics)
# ---------------------------------------------------------------------------

class TestSellPath:
    def test_sell_when_flat_creates_no_proposal(self, proposer, proposal_store):
        pid = proposer.on_signal('orb_test', _signal(Action.SELL), _frame())
        assert pid is None
        assert proposal_store.query(status='PENDING') == []

    def test_sell_when_long_proposes_full_close(self, proposer, trader_client, proposal_store):
        trader_client.portfolio_items = [_make_portfolio_item(position=100.0)]
        pid = proposer.on_signal('orb_test', _signal(Action.SELL), _frame())
        assert pid is not None
        p = proposal_store.get(pid)
        assert p.action == 'SELL'
        assert p.quantity == 100.0
        assert p.amount is None

    def test_sell_when_short_creates_no_proposal(self, proposer, trader_client, proposal_store):
        trader_client.portfolio_items = [_make_portfolio_item(position=-50.0)]
        pid = proposer.on_signal('orb_test', _signal(Action.SELL), _frame())
        assert pid is None

    def test_sell_portfolio_unavailable_creates_no_proposal(self, proposer, trader_client):
        trader_client.portfolio_items = [_make_portfolio_item(position=100.0)]
        trader_client.fail_portfolio = True
        pid = proposer.on_signal('orb_test', _signal(Action.SELL), _frame())
        assert pid is None


# ---------------------------------------------------------------------------
# Gating and TTL
# ---------------------------------------------------------------------------

class TestGatingAndTtl:
    def test_live_mode_is_noop(self, proposal_store, trader_client):
        proposer = SignalProposer(
            proposal_store=proposal_store,
            trader_client=trader_client,
            paper_trading=False,
            sizing_config=PositionSizingConfig(),
        )
        pid = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert pid is None
        assert proposal_store.query(status='PENDING') == []

    def test_stale_pending_proposal_expires_and_new_signal_proposes(
            self, proposal_store, trader_client):
        proposer = SignalProposer(
            proposal_store=proposal_store,
            trader_client=trader_client,
            paper_trading=True,
            sizing_config=PositionSizingConfig(),
            proposal_ttl_minutes=0,   # everything is stale immediately
        )
        pid1 = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        pid2 = proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert pid1 is not None and pid2 is not None and pid2 != pid1
        assert proposal_store.get(pid1).status == ProposalStatus.EXPIRED.value
        assert proposal_store.get(pid2).status == ProposalStatus.PENDING.value

    def test_ttl_does_not_expire_foreign_proposals(self, proposal_store, trader_client):
        from trader.trading.proposal import TradeProposal
        manual_id = proposal_store.add(TradeProposal(symbol='AAPL', action='BUY',
                                                     amount=1000.0, source='manual'))
        proposer = SignalProposer(
            proposal_store=proposal_store,
            trader_client=trader_client,
            paper_trading=True,
            sizing_config=PositionSizingConfig(),
            proposal_ttl_minutes=0,
        )
        proposer.on_signal('orb_test', _signal(Action.BUY), _frame())
        assert proposal_store.get(manual_id).status == ProposalStatus.PENDING.value


# ---------------------------------------------------------------------------
# Periodic (reconciliation-driven) expiry sweep
# ---------------------------------------------------------------------------

class TestExpireStale:
    def test_expire_stale_delegates_without_limit(self, proposer, proposal_store):
        proposal_store.expire_stale_pending = Mock(return_value=[4])
        now = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)
        assert proposer.expire_stale(now) == [4]
        # Delegates the whole sweep with only the effective `now` — no
        # limit=/source= kwargs. Asserting the exact call catches a future
        # regression that would silently scope or cap the sweep.
        proposal_store.expire_stale_pending.assert_called_once_with(now)


# ---------------------------------------------------------------------------
# Time-based exits (close_by_time / max_hold_bars)
# ---------------------------------------------------------------------------

def _executed_entry(proposal_store, proposer, trader_client, **signal_kwargs):
    """Create a bridge BUY proposal and walk it to EXECUTED, returning its id."""
    sig = _signal(Action.BUY, **signal_kwargs)
    pid = proposer.on_signal('orb_test', sig, _frame())
    assert pid is not None
    proposal_store.update_status(pid, ProposalStatus.APPROVED.value)
    proposal_store.update_status(pid, ProposalStatus.EXECUTED.value)
    # The position now exists
    trader_client.portfolio_items = [_make_portfolio_item(position=100.0)]
    return pid


class TestTimeBasedExits:
    def test_close_by_time_proposes_close_once(self, proposer, proposal_store, trader_client):
        entry_id = _executed_entry(proposal_store, proposer, trader_client,
                                   close_by_time=dt.time(15, 45))
        late_frame = _frame(last_time='2026-07-15 15:45')
        exit_id = proposer.check_exits('orb_test', 4391, late_frame)
        assert exit_id is not None
        p = proposal_store.get(exit_id)
        assert p.action == 'SELL'
        assert p.quantity == 100.0
        assert p.metadata['exit_reason'] == 'close_by_time'
        # Entry proposal flagged; second check does not re-propose
        assert proposal_store.get(entry_id).metadata.get('exit_proposed') is True
        assert proposer.check_exits('orb_test', 4391, late_frame) is None

    def test_close_by_time_not_yet_reached_no_proposal(
            self, proposer, proposal_store, trader_client):
        _executed_entry(proposal_store, proposer, trader_client,
                        close_by_time=dt.time(15, 45))
        early_frame = _frame(last_time='2026-07-15 12:00')
        assert proposer.check_exits('orb_test', 4391, early_frame) is None

    def test_max_hold_bars_proposes_close(self, proposer, proposal_store, trader_client):
        _executed_entry(proposal_store, proposer, trader_client, max_hold_bars=30)
        # 40 bars strictly after the entry's execution timestamp
        future_start = dt.datetime.now() + dt.timedelta(minutes=1)
        held_frame = _frame(n=40, start=future_start)
        exit_id = proposer.check_exits('orb_test', 4391, held_frame)
        assert exit_id is not None
        assert proposal_store.get(exit_id).metadata['exit_reason'] == 'max_hold_bars'

    def test_max_hold_bars_not_reached_no_proposal(
            self, proposer, proposal_store, trader_client):
        _executed_entry(proposal_store, proposer, trader_client, max_hold_bars=30)
        future_start = dt.datetime.now() + dt.timedelta(minutes=1)
        short_frame = _frame(n=10, start=future_start)
        assert proposer.check_exits('orb_test', 4391, short_frame) is None

    def test_exit_when_already_flat_flags_without_proposal(
            self, proposer, proposal_store, trader_client):
        entry_id = _executed_entry(proposal_store, proposer, trader_client,
                                   close_by_time=dt.time(15, 45))
        trader_client.portfolio_items = []   # closed manually in the meantime
        late_frame = _frame(last_time='2026-07-15 15:45')
        assert proposer.check_exits('orb_test', 4391, late_frame) is None
        assert proposal_store.get(entry_id).metadata.get('exit_proposed') is True

    def test_entry_without_exit_conditions_never_exit_checked(
            self, proposer, proposal_store, trader_client):
        _executed_entry(proposal_store, proposer, trader_client)
        late_frame = _frame(last_time='2026-07-15 15:45')
        assert proposer.check_exits('orb_test', 4391, late_frame) is None


# ---------------------------------------------------------------------------
# StrategyRuntime integration: conid stamping + load-time validation
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
