"""Strategy state announcement + split-container transport wiring.

Covers the coupled gaps that left the command center's Strategies
panel permanently empty against a split-container deployment:

1. Nothing ever *seeded* loaded strategies into the acknowledgement outbox —
   strategy rows only reached the trader's domain journal via control-command
   acks, so a fresh system had zero strategies to even send a command to
   (``STRATEGY_NOT_FOUND`` chicken-and-egg). ``_announce_strategy_states``
   fixes this: every loaded strategy's observable state is announced once
   per state value, via the same outbox/drain path control commands use.

2. The runtime's *outbound* typed clients toward the trader were constructed
   with ``typed_bind_address`` (the runtime's OWN bind address —
   ``tcp://0.0.0.0`` in a container), so acks could never reach a trader on
   another host. ``trader_typed_address`` makes the connect target explicit.

3. ``run()``'s startup instrument-subscription loop had no per-strategy
   exception isolation — one dead legacy RPC aborted startup for every
   strategy. ``_subscribe_all_strategies`` isolates each strategy.

4. Startup announced into the outbox but deferred drain until the 30s
   reconcile loop, which only starts *after* ``get_historical_data()``.
   A long IB backfill left the Trading-tab Strategies panel empty for
   minutes. ``_schedule_startup_ack_drain`` runs drain in the background
   concurrent with historical fetch.
"""

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.strategy.strategy_revisions import StrategyRevisionStore
from trader.strategy.strategy_runtime import StrategyRuntime
from trader.trading.strategy import StrategyState


def _make_runtime(tmp_path, **overrides) -> StrategyRuntime:
    kwargs = dict(
        ib_server_address='127.0.0.1', ib_server_port=4002,
        strategy_runtime_ib_client_id=99,
        duckdb_path=str(tmp_path / 'x.duckdb'),
        universe_library='u',
        zmq_pubsub_server_address='tcp://127.0.0.1', zmq_pubsub_server_port=1,
        zmq_rpc_server_address='tcp://127.0.0.1', zmq_rpc_server_port=2,
        zmq_strategy_rpc_server_address='tcp://127.0.0.1',
        zmq_strategy_rpc_server_port=3,
        zmq_messagebus_server_address='tcp://127.0.0.1',
        zmq_messagebus_server_port=4,
        strategies_directory=str(tmp_path),
        strategy_config_file=str(tmp_path / 'strategy_runtime.yaml'),
    )
    kwargs.update(overrides)
    return StrategyRuntime(**kwargs)


class _StubStrategy:
    """Just enough surface for _announce_strategy_states / _state_payload /
    _subscribe_all_strategies: name, state, conids, universe."""

    def __init__(self, name, state=StrategyState.INSTALLED, conids=None, universe=None):
        self.name = name
        self.state = state
        self.conids = conids or []
        self.universe = universe
        self.errors = []

    def on_error(self, ex):
        self.errors.append(ex)


@pytest.fixture
def runtime_with_revisions(tmp_path):
    rt = _make_runtime(tmp_path)
    rt._revisions = StrategyRevisionStore(
        DuckDBConnection(str(tmp_path / 'revisions.duckdb')))
    rt._revisions.migrate()
    return rt


def _outbox_rows(rt):
    return rt._revisions.db.execute(
        'SELECT strategy_name, state_revision, payload FROM strategy_ack_outbox '
        'ORDER BY ack_id', fetch='all')


class TestAnnounceStrategyStates:
    def test_seeds_outbox_for_every_loaded_strategy(self, runtime_with_revisions):
        rt = runtime_with_revisions
        rt.strategy_implementations = [
            _StubStrategy('alpha', StrategyState.INSTALLED),
            _StubStrategy('beta', StrategyState.RUNNING),
        ]
        rt._announce_strategy_states()
        rows = _outbox_rows(rt)
        assert [r[0] for r in rows] == ['alpha', 'beta']
        assert all(r[1] == 1 for r in rows)  # first state_revision for each
        import json
        payloads = {r[0]: json.loads(r[2]) for r in rows}
        assert payloads['alpha']['state'] == 'INSTALLED'
        assert payloads['alpha']['strategy_state'] == 'INSTALLED'
        assert payloads['beta']['state'] == 'RUNNING'
        assert payloads['beta']['strategy_state'] == 'RUNNING'
        assert payloads['alpha']['strategy_name'] == 'alpha'

    def test_idempotent_while_state_unchanged(self, runtime_with_revisions):
        rt = runtime_with_revisions
        rt.strategy_implementations = [_StubStrategy('alpha')]
        rt._announce_strategy_states()
        rt._announce_strategy_states()
        assert len(_outbox_rows(rt)) == 1

    def test_reannounces_on_state_change(self, runtime_with_revisions):
        rt = runtime_with_revisions
        strat = _StubStrategy('alpha', StrategyState.INSTALLED)
        rt.strategy_implementations = [strat]
        rt._announce_strategy_states()
        strat.state = StrategyState.RUNNING
        rt._announce_strategy_states()
        rows = _outbox_rows(rt)
        assert len(rows) == 2
        import json
        assert json.loads(rows[1][2])['state'] == 'RUNNING'
        assert rows[1][1] == 2  # state_revision advanced

    def test_noop_without_revision_store(self, tmp_path):
        rt = _make_runtime(tmp_path)
        rt.strategy_implementations = [_StubStrategy('alpha')]
        assert rt._revisions is None
        rt._announce_strategy_states()  # must not raise


class TestTraderTypedAddress:
    def test_explicit_address_wins(self, tmp_path):
        rt = _make_runtime(
            tmp_path,
            typed_bind_address='tcp://0.0.0.0',
            trader_typed_address='tcp://trader',
        )
        assert rt.trader_typed_address == 'tcp://trader'

    def test_defaults_to_typed_bind_address_for_single_host(self, tmp_path):
        rt = _make_runtime(tmp_path, typed_bind_address='tcp://127.0.0.1')
        assert rt.trader_typed_address == 'tcp://127.0.0.1'


class TestStartupSubscriptionIsolation:
    def test_one_dead_rpc_does_not_abort_other_strategies(self, tmp_path):
        rt = _make_runtime(tmp_path)
        attempted = []

        class _Gateway:
            def resolve_instrument(self, conId):
                attempted.append(conId)
                raise ConnectionError('no route to server')

        rt._trader_gateway = _Gateway()
        rt.strategy_implementations = [
            _StubStrategy('alpha', conids=[111]),
            _StubStrategy('beta', conids=[222]),
        ]
        rt._subscribe_all_strategies()  # must not raise
        assert attempted == [111, 222]  # beta still attempted after alpha failed


class TestAnnounceDoesNotCallbackIntoTrader:
    """Regression: enable/disable used to call ``_drain_ack_outbox`` inline,
    which dials the trader's typed command socket. When disable is answering
    a trader→strategy forward, that callback deadlocks the command path and
    logs ``record_state_acknowledged ... timed out after 10000ms``.
    """

    def test_disable_announces_without_calling_trader_command(self, runtime_with_revisions):
        rt = runtime_with_revisions

        class _Toggleable(_StubStrategy):
            def disable(self):
                self.state = StrategyState.DISABLED
                return self.state

            def enable(self):
                self.state = StrategyState.RUNNING
                return self.state

        strat = _Toggleable('vwap_reclaim_cat', StrategyState.RUNNING)
        rt.strategy_implementations = [strat]

        class _BoomClient:
            def call(self, *args, **kwargs):
                raise AssertionError(
                    'disable must not call trader command client (deadlock risk)'
                )

        rt._trader_command_client = _BoomClient()
        state = rt.disable_strategy('vwap_reclaim_cat')
        assert state == StrategyState.DISABLED
        assert strat.state == StrategyState.DISABLED
        # Outbox seeded for reconcile drain; trader is never dialed here.
        rows = _outbox_rows(rt)
        assert any(r[0] == 'vwap_reclaim_cat' for r in rows)

    def test_enable_announces_without_calling_trader_command(self, runtime_with_revisions):
        rt = runtime_with_revisions

        class _Toggleable(_StubStrategy):
            def disable(self):
                self.state = StrategyState.DISABLED
                return self.state

            def enable(self):
                self.state = StrategyState.RUNNING
                return self.state

        strat = _Toggleable('orb', StrategyState.DISABLED)
        rt.strategy_implementations = [strat]

        class _BoomClient:
            def call(self, *args, **kwargs):
                raise AssertionError(
                    'enable must not call trader command client (deadlock risk)'
                )

        rt._trader_command_client = _BoomClient()
        state = rt.enable_strategy('orb')
        assert state == StrategyState.RUNNING
        assert any(r[0] == 'orb' for r in _outbox_rows(rt))

    def test_enable_returns_before_slow_persist(self, runtime_with_revisions):
        """G1: DuckDB persist must not block the enable RPC reply."""
        import threading
        import time

        rt = runtime_with_revisions

        class _Toggleable(_StubStrategy):
            def enable(self):
                self.state = StrategyState.RUNNING
                return self.state

        rt.strategy_implementations = [_Toggleable('orb', StrategyState.DISABLED)]
        started = threading.Event()
        release = threading.Event()
        calls = []

        def _slow_persist(name, enabled):
            calls.append((name, enabled))
            started.set()
            assert release.wait(timeout=5), 'test release never set'

        rt._persist_enabled = _slow_persist
        t0 = time.monotonic()
        state = rt.enable_strategy('orb')
        elapsed = time.monotonic() - t0
        assert state == StrategyState.RUNNING
        assert elapsed < 0.5, f'enable blocked on persist ({elapsed:.2f}s)'
        assert started.wait(timeout=2), 'persist thread never started'
        release.set()
        # Give the daemon a moment to record the call.
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == [('orb', True)]

    def test_disable_schedules_persist(self, runtime_with_revisions):
        import threading
        import time

        rt = runtime_with_revisions

        class _Toggleable(_StubStrategy):
            def disable(self):
                self.state = StrategyState.DISABLED
                return self.state

        rt.strategy_implementations = [_Toggleable('orb', StrategyState.RUNNING)]
        done = threading.Event()
        calls = []

        def _persist(name, enabled):
            calls.append((name, enabled))
            done.set()

        rt._persist_enabled = _persist
        assert rt.disable_strategy('orb') == StrategyState.DISABLED
        assert done.wait(timeout=2)
        assert calls == [('orb', False)]


class TestDrainAckOutboxFailFast:
    def test_aborts_batch_after_trader_timeout(self, runtime_with_revisions):
        rt = runtime_with_revisions
        rt.strategy_implementations = [
            _StubStrategy('a'), _StubStrategy('b'), _StubStrategy('c'),
        ]
        rt._announce_strategy_states()
        calls = []

        class _TimeoutClient:
            def call(self, method, body, response_model, timeout=None):
                calls.append((method, body.get('strategy_name'), timeout))
                raise TimeoutError(f'typed RPC call to {method!r} timed out')

        rt._trader_command_client = _TimeoutClient()
        rt._drain_ack_outbox()
        # One attempt then abort — must not walk every outbox row at 10s each.
        assert len(calls) == 1
        assert calls[0][0] == 'record_state_acknowledged'
        assert calls[0][2] == 3.0


class TestStartupAckDrain:
    """Trading-tab strategies come from journaled strategy.updated rows.
    Startup used to wait for get_historical_data() before the reconcile
    loop drained the ack outbox — a long IB backfill left the panel empty.
    Background drain must run concurrently and not block the event loop.
    """

    @pytest.mark.asyncio
    async def test_startup_drain_empties_outbox(self, runtime_with_revisions):
        rt = runtime_with_revisions
        rt.strategy_implementations = [
            _StubStrategy('alpha'), _StubStrategy('beta'),
        ]
        rt._announce_strategy_states()
        assert _outbox_rows(rt)  # seeded

        class _OkClient:
            def call(self, method, body, response_model, timeout=None):
                return {'entity_revision': body['state_revision']}

        rt._trader_command_client = _OkClient()
        await rt._startup_drain_ack_outbox(attempts=3, interval_s=0.0)
        assert rt._revisions.unacknowledged_outbox(10) == []

    @pytest.mark.asyncio
    async def test_startup_drain_retries_after_timeout(self, runtime_with_revisions):
        rt = runtime_with_revisions
        rt.strategy_implementations = [_StubStrategy('alpha')]
        rt._announce_strategy_states()
        calls = {'n': 0}

        class _FlakyClient:
            def call(self, method, body, response_model, timeout=None):
                calls['n'] += 1
                if calls['n'] < 2:
                    raise TimeoutError('trader not ready')
                return {'entity_revision': 1}

        rt._trader_command_client = _FlakyClient()
        await rt._startup_drain_ack_outbox(attempts=5, interval_s=0.0)
        assert calls['n'] >= 2
        assert rt._revisions.unacknowledged_outbox(10) == []

    @pytest.mark.asyncio
    async def test_schedule_startup_drain_is_non_blocking(self, runtime_with_revisions):
        rt = runtime_with_revisions
        rt.strategy_implementations = [_StubStrategy('alpha')]
        rt._announce_strategy_states()
        gate = {'entered': False, 'release': False}

        class _SlowClient:
            def call(self, method, body, response_model, timeout=None):
                gate['entered'] = True
                while not gate['release']:
                    import time
                    time.sleep(0.01)
                return {'entity_revision': 1}

        rt._trader_command_client = _SlowClient()
        task = rt._schedule_startup_ack_drain(attempts=1, interval_s=0.0)
        assert task is not None
        # Returns immediately even though the drain thread is blocked.
        assert not task.done()
        gate['release'] = True
        await task
        assert rt._revisions.unacknowledged_outbox(10) == []
