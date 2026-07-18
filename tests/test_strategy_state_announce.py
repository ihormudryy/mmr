"""Strategy state announcement + split-container transport wiring.

Covers the three coupled gaps that left the command center's Strategies
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
        assert payloads['beta']['state'] == 'RUNNING'
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

        class _Rpc:
            def resolve_symbol(self, conId):
                attempted.append(conId)
                raise ConnectionError('no route to server')

        class _Client:
            def rpc(self):
                return _Rpc()

        rt.trader_client = _Client()
        rt.strategy_implementations = [
            _StubStrategy('alpha', conids=[111]),
            _StubStrategy('beta', conids=[222]),
        ]
        rt._subscribe_all_strategies()  # must not raise
        assert attempted == [111, 222]  # beta still attempted after alpha failed
