from ib_async import Contract
from ib_async.ib import IB
from ib_async.ticker import Ticker
from pydantic import BaseModel, ConfigDict
from reactivex.observer import AutoDetachObserver
from trader.common.exceptions import TraderConnectionException, TraderException
from trader.common.helpers import dateify
from trader.common.logging_helper import get_callstack, log_method, setup_logging
from trader.data.duckdb_store import DuckDBConnection
from trader.data.market_data import normalize_ticker
from trader.data.store import DateRange

from trader.data.data_access import SecurityDefinition, TickStorage
from trader.data.universe import UniverseAccessor
from trader.listeners.ib_history_worker import IBHistoryWorker, IBConnectivityError, IBNoDataError
from trader.messaging.clientserver import (
    MessageBusClient,
    MultithreadedTopicPubSub,
    RPCClient,
    RPCServer,
    TopicPubSub
)
from trader.messaging.typed_rpc import (
    HmacServiceAuthenticator,
    TypedRpcClient,
    TypedRpcRegistry,
    TypedRpcServer,
    _DispatchProblem,
    load_service_hmac_key,
)
from trader.objects import Action, BarSize, WhatToShow
from trader.data.event_store import EventStore, EventType, TradingEvent
from trader.strategy.signal_proposer import SignalProposer
from trader.strategy.strategy_revisions import StrategyCommandReceipt, StrategyRevisionStore
from trader.trading.strategy import Signal, Strategy, StrategyConfig, StrategyContext, StrategyState
from typing import Any, cast, Dict, List, Optional

import asyncio
import backoff
import datetime as dt
import exchange_calendars
import importlib
import importlib.util
import inspect
import os
import pandas as pd
import sys
import trader.messaging.strategy_service_api as bus
import yaml


logging = setup_logging(module_name='strategy_runtime')


error_table = {
    'trader.common.exceptions.TraderException': TraderException,
    'trader.common.exceptions.TraderConnectionException': TraderConnectionException
}


def _whattoshow_for_contract(contract: Contract) -> WhatToShow:
    """Pick the right IB whatToShow for an instrument's secType.

    For OHLCV history:
      * STK / FUT / IND / OPT  -> TRADES (real prints, larger IB chunk caps,
        not throttled the way MIDPOINT is, and matches what humans see on
        a chart).
      * CASH (FX spot)         -> MIDPOINT (FX has no consolidated tape,
        so trade prints don't exist; MIDPOINT is the only sensible bar).
      * anything else / unset  -> TRADES (safe default; IB will surface a
        162 "no data" if it's the wrong choice and the caller can retry).

    Override at the call site only if the strategy genuinely needs
    quote-mid history (e.g. spread modeling).
    """
    sec_type = (contract.secType or '').upper()
    if sec_type == 'CASH':
        return WhatToShow.MIDPOINT
    return WhatToShow.TRADES


class ControlRevisionConflict(Exception):
    """[M1-F3] Task 7. The caller's ``expected_control_revision`` did not
    match the strategy's CURRENT ``control_revision`` -- a stale forwarded
    command, rejected rather than acted on."""

    def __init__(self, strategy_name: str, current_control_revision: int):
        self.strategy_name = strategy_name
        self.current_control_revision = current_control_revision
        super().__init__(
            f'control revision conflict for strategy {strategy_name!r}: '
            f'current control_revision is {current_control_revision}'
        )


class StartupConfigRecoveryError(Exception):
    """[M1-F3] Task 7. Startup recovery could not restore ``prior_config``
    into the live YAML for one or more crashed staged-config revisions.

    Raised (fail-loud) instead of swallowing the failure and reporting the
    revisions as cleanly recovered: a genuinely failed restore must abort
    startup rather than let the service come up on a config whose real state
    is unknown while the DB claims it was rolled back (a falsified audit
    trail)."""

    def __init__(self, failures: list[tuple[int, str]]):
        self.failures = failures
        detail = '; '.join(f'revision {rid}: {err}' for rid, err in failures)
        super().__init__(
            f'startup config recovery failed to restore prior_config for '
            f'{len(failures)} revision(s): {detail}'
        )


# ---------------------------------------------------------------------------
# [M1-F3] Task 7 -- typed request models + handlers for the strategy-service
# command/query sockets (42104/42105). Registered onto their own
# TypedRpcRegistry instances by _register_strategy_control_authority, wired
# up from StrategyRuntime.connect()/run().
# ---------------------------------------------------------------------------

class _EnableStrategyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str
    strategy_name: str
    expected_control_revision: int


class _DisableStrategyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str
    strategy_name: str
    expected_control_revision: int


class _UpdateStrategyParamsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str
    strategy_name: str
    expected_control_revision: int
    params: Dict[str, Any] = {}


class _GetStrategyReceiptRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str


def _strategy_receipt_to_dict(receipt: StrategyCommandReceipt) -> Dict[str, Any]:
    import dataclasses
    return dataclasses.asdict(receipt)


def _control_command_handler(runtime: 'StrategyRuntime', action: str):
    def _handler(parsed) -> Dict[str, Any]:
        params = parsed.params if action == 'update_strategy_params' else None
        try:
            receipt = runtime.apply_control_command(
                parsed.command_id, parsed.strategy_name, action,
                parsed.expected_control_revision, params,
            )
        except ControlRevisionConflict as exc:
            raise _DispatchProblem('CONTROL_REVISION_CONFLICT', str(exc)) from exc
        return _strategy_receipt_to_dict(receipt)
    return _handler


def _get_strategy_receipt_handler(runtime: 'StrategyRuntime'):
    def _handler(parsed: _GetStrategyReceiptRequest) -> Dict[str, Any]:
        receipt = runtime._revisions.get_receipt(parsed.command_id)
        if receipt is None:
            raise _DispatchProblem('COMMAND_NOT_FOUND', f'no receipt for command {parsed.command_id!r}')
        return _strategy_receipt_to_dict(receipt)
    return _handler


def register_strategy_control_authority(
    command_registry: TypedRpcRegistry, query_registry: TypedRpcRegistry, runtime: 'StrategyRuntime',
) -> None:
    """Wire the strategy-control typed surface onto the strategy-service's
    own command/query registries: ``enable_strategy``, ``disable_strategy``,
    and ``update_strategy_params`` (the SAME three frozen names the trader's
    coordinator forwards) on ``command``; ``get_strategy_receipt`` on
    ``query``. Every handler here is fully self-contained -- it applies
    locally via ``runtime.apply_control_command`` (which itself commits the
    receipt + revision bump + outbox row in ONE transaction) and returns --
    NEVER calls back into the trader while handling the request (see
    CLAUDE.md memory: dashboard-strategy-controls, the deadlock this exact
    pattern already caused once).
    """
    command_registry.register(
        'command', 'enable_strategy', _EnableStrategyRequest, dict,
        _control_command_handler(runtime, 'enable_strategy'),
    )
    command_registry.register(
        'command', 'disable_strategy', _DisableStrategyRequest, dict,
        _control_command_handler(runtime, 'disable_strategy'),
    )
    command_registry.register(
        'command', 'update_strategy_params', _UpdateStrategyParamsRequest, dict,
        _control_command_handler(runtime, 'update_strategy_params'),
    )
    query_registry.register(
        'query', 'get_strategy_receipt', _GetStrategyReceiptRequest, dict,
        _get_strategy_receipt_handler(runtime),
    )


class StrategyRuntime():
    def __init__(
        self,
        ib_server_address: str,
        ib_server_port: int,
        strategy_runtime_ib_client_id: int,
        duckdb_path: str,
        universe_library: str,
        zmq_pubsub_server_address: str,
        zmq_pubsub_server_port: int,
        zmq_rpc_server_address: str,
        zmq_rpc_server_port: int,
        zmq_strategy_rpc_server_address: str,
        zmq_strategy_rpc_server_port: int,
        zmq_messagebus_server_address: str,
        zmq_messagebus_server_port: int,
        strategies_directory: str,
        strategy_config_file: str,
        history_duckdb_path: str = '',
        paper_trading: bool = False,
        simulation: bool = False,
        typed_bind_address: str = 'tcp://127.0.0.1',
        strategy_typed_command_port: int = 42104,
        strategy_typed_query_port: int = 42105,
        typed_command_port: int = 42102,
        typed_query_port: int = 42101,
        service_hmac_key_file: str = '',
        ib_account: str = '',
    ):
        self.ib_server_address = ib_server_address
        self.ib_server_port = ib_server_port
        self.strategy_runtime_ib_client_id: int = strategy_runtime_ib_client_id
        self.duckdb_path = duckdb_path
        self.history_duckdb_path = history_duckdb_path or duckdb_path
        self.universe_library = universe_library
        self.simulation: bool = simulation
        self.paper_trading = paper_trading
        self.zmq_pubsub_server_address = zmq_pubsub_server_address
        self.zmq_pubsub_server_port = zmq_pubsub_server_port
        self.zmq_rpc_server_address = zmq_rpc_server_address
        self.zmq_rpc_server_port = zmq_rpc_server_port
        self.zmq_strategy_rpc_server_address = zmq_strategy_rpc_server_address
        self.zmq_strategy_rpc_server_port = zmq_strategy_rpc_server_port
        self.zmq_messagebus_server_address = zmq_messagebus_server_address
        self.zmq_messagebus_server_port = zmq_messagebus_server_port

        # [M1-F3] Task 7: typed, HMAC-authenticated command/query sockets --
        # reuses the SAME typed_bind_address/service_hmac_key_file config
        # keys the trader's own typed sockets use (shared HMAC secret is
        # what lets the two sides authenticate each other); only the ports
        # are new.
        self.typed_bind_address = typed_bind_address
        self.strategy_typed_command_port = strategy_typed_command_port
        self.strategy_typed_query_port = strategy_typed_query_port
        # The TRADER's own typed command/query ports (same config
        # keys/values trader_service uses for its own typed_command_port/
        # typed_query_port) -- this is where _drain_ack_outbox's backstop
        # record_state_acknowledged calls AND SignalProposer's
        # create_proposal/get_trading_control/list_proposals calls go, NOT
        # this service's own strategy_typed_command_port/
        # strategy_typed_query_port above.
        self.typed_command_port = typed_command_port
        self.typed_query_port = typed_query_port
        self.service_hmac_key_file = service_hmac_key_file
        # [M1-F3] Task 8: the account SignalProposer reads the pause gate
        # for (get_trading_control has no account_id in its request body --
        # the SERVER derives it from ITS OWN configured account -- but the
        # bridge still stamps its own copy on the outbound query body for
        # symmetry/logging; harmless either way since the server ignores it).
        self.ib_account = ib_account
        self._revisions: Optional[StrategyRevisionStore] = None

        self.strategies_directory = strategies_directory
        self.strategy_config_file = strategy_config_file
        self.startup_time: dt.datetime = dt.datetime.now()
        self.last_connect_time: dt.datetime

        self.zmq_strategy_rpc_server: RPCServer[bus.StrategyServiceApi]
        self.zmq_messagebus_client: MessageBusClient

        # todo: this is wrong as we'll have a whole bunch of different tickdata libraries for
        # different bartypes etc.
        self.storage: TickStorage

        self.universe_accessor: UniverseAccessor

        self.strategies: Dict[int, List[Strategy]] = {}
        self.strategy_implementations: List[Strategy] = []
        self.streams: Dict[int, pd.DataFrame] = {}
        # Historical OHLCV bars per (conId, bar_size), loaded from the DB on
        # subscribe. These PRIME the live frame so bar-based strategies have
        # warmup + today's opening bars — the live tick stream alone only holds
        # ticks since subscription.
        self._hist_bars: Dict[tuple, pd.DataFrame] = {}
        # Last completed bar timestamp dispatched per (conId, strategy name), so
        # a strategy sees each bar once (not on every tick).
        self._last_dispatched_bar: Dict[tuple, pd.Timestamp] = {}
        # Config-file change detection. Must exist from construction:
        # reload_strategies (RPC → _reconcile) can fire while run() is still
        # in its initial historical fetch, long before run() stamps the real
        # mtime — 0.0 makes that early reload load the config instead of
        # crashing on a missing attribute.
        self._config_mtime: float = 0.0
        # Keep at most this many days of raw ticks per conid (bounds compute).
        self._tick_retention_days: int = 2

        self.historical_data_client: IBHistoryWorker

    def create_strategy_exception(self, exception_type: type, message: str, inner: Optional[Exception]):
        # todo use reflection here to automatically populate trader runtime vars that we care about
        # given a particular exception type
        data = self.storage if hasattr(self, 'data') else None
        last_connect_time = self.last_connect_time if hasattr(self, 'last_connect_time') else dt.datetime.min

        exception = exception_type(
            message,
            data is not None,
            False,
            self.startup_time,
            last_connect_time,
            inner,
            get_callstack(10)
        )
        logging.exception(exception)
        return exception

    @backoff.on_exception(backoff.expo, ConnectionRefusedError, max_tries=10, max_time=120)
    def connect(self):
        """Synchronous setup: wire up dependencies that don't need the loop.

        Anything that binds ZMQ sockets or creates asyncio tasks is deferred
        to ``run()``, which is async. Calling ``asyncio.run(coro)`` from this
        method used to spin up a throwaway loop and orphan the server task —
        the socket was bound, no task ever ran, requests silently piled up.
        """
        # avoids circular import
        from trader.messaging.trader_service_api import TraderServiceApi
        try:
            self.storage = TickStorage(self.history_duckdb_path)
            self.universe_accessor = UniverseAccessor(self.duckdb_path, self.universe_library)
            self.event_store = EventStore(self.duckdb_path)
            self.trader_client = RPCClient[TraderServiceApi](
                zmq_server_address=self.zmq_rpc_server_address,
                zmq_server_port=self.zmq_rpc_server_port,
                error_table=error_table
            )
            self.last_connect_time = dt.datetime.now()

            self.zmq_strategy_rpc_server = RPCServer[bus.StrategyServiceApi](
                instance=bus.StrategyServiceApi(self),
                zmq_rpc_server_address=self.zmq_strategy_rpc_server_address,
                zmq_rpc_server_port=self.zmq_strategy_rpc_server_port,
            )

            self.zmq_messagebus_client = MessageBusClient(
                zmq_address=self.zmq_messagebus_server_address,
                zmq_port=self.zmq_messagebus_server_port,
            )

            # [M1-F3] Task 7: strategy revisions/receipts/outbox -- the SAME
            # DuckDB file this runtime already uses for strategy_state
            # (self.duckdb_path), not the trader's dedicated journal file.
            self._revisions = StrategyRevisionStore(DuckDBConnection.get_instance(self.duckdb_path))
            self._revisions.migrate()

            # Typed, HMAC-authenticated command/query sockets (42104/42105 by
            # default) -- the strategy-service side of the coordinator's
            # one-way forwarding boundary. Every handler registered here is
            # fully self-contained (apply locally via
            # apply_control_command, which itself commits the receipt +
            # revision bump + outbox row in ONE transaction) and NEVER calls
            # back into the trader while handling a request.
            hmac_key = load_service_hmac_key(self.service_hmac_key_file)
            self._typed_authenticator = HmacServiceAuthenticator(hmac_key)
            self._typed_command_registry = TypedRpcRegistry()
            self._typed_query_registry = TypedRpcRegistry()
            register_strategy_control_authority(
                self._typed_command_registry, self._typed_query_registry, self,
            )
            self.typed_command_server = TypedRpcServer(
                'command', self._typed_command_registry, self._typed_authenticator,
                address=self.typed_bind_address, port=self.strategy_typed_command_port,
            )
            self.typed_query_server = TypedRpcServer(
                'query', self._typed_query_registry, self._typed_authenticator,
                address=self.typed_bind_address, port=self.strategy_typed_query_port,
            )
            # Outbound-only client toward the TRADER's own typed command
            # socket -- used exclusively by _drain_ack_outbox's backstop
            # record_state_acknowledged calls (never inside a handler
            # responding to an inbound forwarded command; see that method's
            # docstring), AND [M1-F3] Task 8's signal→proposal bridge
            # (SignalProposer's create_proposal calls, below).
            self._trader_command_client = TypedRpcClient(
                'command', self._typed_authenticator,
                address=self.typed_bind_address, port=self.typed_command_port,
            )
            # Outbound-only client toward the TRADER's own typed QUERY
            # socket -- used by SignalProposer to read the pause gate
            # (get_trading_control) and executed bridge entries
            # (list_proposals). Same host/HMAC secret as the command client
            # above; only the port differs.
            self._trader_query_client = TypedRpcClient(
                'query', self._typed_authenticator,
                address=self.typed_bind_address, port=self.typed_query_port,
            )

            # [M1-F3] Task 8: signal → PENDING proposal bridge for
            # auto_execute: 'propose' strategies (paper mode only; see
            # signal_proposer.py). Thin typed adapter -- holds no
            # ProposalStore handle, routes every mutation through the
            # trader's command-authority coordinator via the typed clients
            # just constructed above.
            self.signal_proposer = SignalProposer(
                command_client=self._trader_command_client,
                query_client=self._trader_query_client,
                paper_trading=self.paper_trading,
                account_id=self.ib_account,
            )

        except Exception as ex:
            raise self.create_strategy_exception(
                TraderConnectionException,
                message='strategy_runtime.connect() exception', inner=ex
            )

    @log_method
    def _persist_enabled(self, name: str, enabled: bool) -> None:
        """Persist a strategy's enabled/disabled state so it survives a restart
        (otherwise a runtime disable is silently undone when the config reloads)."""
        try:
            from trader.data.duckdb_store import DuckDBConnection
            db = DuckDBConnection.get_instance(self.duckdb_path)

            def _w(conn):
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS strategy_state "
                    "(name VARCHAR PRIMARY KEY, enabled BOOLEAN, updated_at TIMESTAMP)")
                conn.execute("DELETE FROM strategy_state WHERE name = ?", [name])
                conn.execute(
                    "INSERT INTO strategy_state (name, enabled, updated_at) VALUES (?, ?, ?)",
                    [name, bool(enabled), dt.datetime.now()])
            db.execute_atomic(_w)
        except Exception as ex:
            logging.warning('could not persist enabled-state for %s: %s', name, ex)

    def _load_enabled(self, name: str):
        """Return the persisted enabled state for *name* (True/False), or None if
        it was never explicitly enabled/disabled."""
        try:
            from trader.data.duckdb_store import DuckDBConnection
            db = DuckDBConnection.get_instance(self.duckdb_path)

            def _r(conn):
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS strategy_state "
                    "(name VARCHAR PRIMARY KEY, enabled BOOLEAN, updated_at TIMESTAMP)")
                row = conn.execute(
                    "SELECT enabled FROM strategy_state WHERE name = ?", [name]).fetchone()
                return row[0] if row else None
            return db.execute_atomic(_r)
        except Exception as ex:
            logging.warning('could not read enabled-state for %s: %s', name, ex)
            return None

    def enable_strategy(self, name: str, paper_only: bool = False) -> StrategyState:
        """Enable a strategy.

        ``paper_only`` is a load-time safety gate (see ``load_strategy``); it's
        ignored here. The param is retained for wire compatibility but has no
        effect — routing is determined by the trader_service's account, not
        per-strategy flags.
        """
        for implementation in self.strategy_implementations:
            if name == implementation.name:
                state = implementation.enable()
                self._persist_enabled(name, True)
                return state
        return StrategyState.ERROR

    @log_method
    def disable_strategy(self, name: str) -> StrategyState:
        for implementation in self.strategy_implementations:
            if name == implementation.name:
                state = implementation.disable()
                self._persist_enabled(name, False)
                return state
        return StrategyState.ERROR

    @log_method
    def get_strategy(self, name: str) -> Optional[Strategy]:
        for strategy in self.strategy_implementations:
            if strategy.name == name:
                return strategy
        return None

    @staticmethod
    def _coerce_param_value(value):
        """Form values arrive as strings — coerce to bool/int/float where the
        text is unambiguous, otherwise keep the string. Non-strings pass
        through untouched (RPC callers may send native types)."""
        if not isinstance(value, str):
            return value
        text = value.strip()
        low = text.lower()
        if low == 'true':
            return True
        if low == 'false':
            return False
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        return text

    def update_strategy_params(self, name: str, params: Dict) -> Dict:
        """Persist new params for ``name`` into the YAML config and hot-swap
        the live instance so they take effect immediately — no service
        restart. An empty-string value deletes that key.

        Raises ``ValueError`` for an unknown strategy (nothing written) and
        ``RuntimeError`` if the reload after a successful write fails (the
        config IS persisted at that point; a restart converges).
        """
        with open(self.strategy_config_file) as f:
            cfg = yaml.safe_load(f) or {}
        entries = cfg.get('strategies') or []
        entry = next((e for e in entries if e.get('name') == name), None)
        if entry is None:
            raise ValueError(
                f'strategy {name!r} not found in {self.strategy_config_file}')

        merged = dict(entry.get('params') or {})
        for key, raw in params.items():
            if not key:
                continue
            if isinstance(raw, str) and raw.strip() == '':
                merged.pop(key, None)
                continue
            merged[key] = self._coerce_param_value(raw)
        if merged:
            entry['params'] = merged
        else:
            entry.pop('params', None)

        # Atomic write: reconcile reading a half-written YAML retries safely,
        # but a crash mid-write must never leave a torn config behind.
        tmp_path = self.strategy_config_file + '.tmp'
        with open(tmp_path, 'w') as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        os.replace(tmp_path, self.strategy_config_file)

        # Hot-swap: unload the live instance, re-load from the updated entry.
        # Primed history survives (keyed by (conId, bar_size)) and
        # load_strategy restores the persisted enabled/disabled state (D2).
        old = self.get_strategy(name)
        if old is not None:
            self.strategy_implementations.remove(old)
            for lst in self.strategies.values():
                if old in lst:
                    lst.remove(old)
            self._last_dispatched_bar = {
                k: v for k, v in self._last_dispatched_bar.items() if k[1] != name}
            sys.modules.pop(f'_mmr_strategy_{name}', None)

        self.load_strategy(
            name=name,
            bar_size_str=entry.get('bar_size', '1 min'),
            conids=entry.get('conids'),
            universe=entry.get('universe'),
            historical_days_prior=entry.get('historical_days_prior', 0),
            module=entry.get('module', ''),
            class_name=entry.get('class_name', ''),
            description=entry.get('description', ''),
            paper_only=entry.get('paper_only', False),
            auto_execute=entry.get('auto_execute', False),
            params=merged,
        )
        new = self.get_strategy(name)
        if new is None:
            raise RuntimeError(
                f'strategy {name!r} failed to reload after params update — the '
                'config is persisted; restart strategy_service to converge')

        # Re-attach the new instance to the conid dispatch lists its
        # predecessor occupied. Deliberately NO RPC here: this method runs
        # while trader_service's event loop is blocked awaiting our reply, so
        # calling resolve_symbol back into it deadlocks until timeout. The
        # conIds are already subscribed/published (the old instance put them
        # there); a conId with no existing list is genuinely new and is
        # published properly by the reconcile loop within 30s.
        for conId in (new.conids or []):
            bucket = self.strategies.get(conId)
            if bucket is not None and new not in bucket:
                bucket.append(new)

        logging.info('strategy %s params updated to %s (hot-swapped)', name, merged)
        return {'name': name, 'params': merged}

    # ------------------------------------------------------------------ #
    # [M1-F3] Task 7 -- coordinator-forwarded strategy control.
    #
    # apply_control_command is the ONE entry point every forwarded
    # enable_strategy/disable_strategy/update_strategy_params command runs
    # through: idempotent-by-command_id, CAS-guarded on control_revision,
    # and -- for update_strategy_params -- a crash-safe staged YAML swap
    # (stage -> validate+instantiate the replacement -> rename onto the
    # live path; any failure anywhere in that sequence restores the PRIOR
    # config and rolls back, never leaving a half-applied strategy).
    # ------------------------------------------------------------------ #

    _CONTROL_ACTIONS = frozenset({'enable_strategy', 'disable_strategy', 'update_strategy_params'})

    def apply_control_command(
        self,
        command_id: str,
        strategy_name: str,
        action: str,
        expected_control_revision: int,
        params: Optional[Dict] = None,
    ) -> StrategyCommandReceipt:
        """Self-contained application of one coordinator-forwarded
        strategy-control command. NEVER calls back into the trader (see
        CLAUDE.md memory: dashboard-strategy-controls).

        Idempotent: a retry of a ``command_id`` already recorded in the
        receipt ledger returns the SAME receipt without re-running the
        mutation (§9.1). Otherwise CAS-guards on ``control_revision`` --
        raises ``ControlRevisionConflict`` on a stale caller-supplied
        ``expected_control_revision``.
        """
        if action not in self._CONTROL_ACTIONS:
            raise ValueError(f'unknown strategy control action: {action!r}')

        existing = self._revisions.get_receipt(command_id)
        if existing is not None:
            return existing

        current = self._revisions.control_revision(strategy_name)
        if expected_control_revision != current:
            raise ControlRevisionConflict(strategy_name, current)

        if action == 'update_strategy_params':
            prior_entry, proposed_entry = self._config_entries(strategy_name, params or {})
            revision_id = self._revisions.prepare_config_revision(
                strategy_name, current, prior_entry, proposed_entry, command_id,
            )
            try:
                self._stage_yaml(proposed_entry)          # write .tmp -- not yet renamed
                self._swap_runtime(strategy_name, proposed_entry)  # validate + instantiate replacement
                os.replace(self._staged_path(), self.strategy_config_file)
            except Exception as ex:
                restore_error = self._restore_runtime(strategy_name, prior_entry)
                self._unstage_yaml()
                self._revisions.mark_rolled_back(revision_id, str(ex))
                if self.get_strategy(strategy_name) is None:
                    # Double failure: the swap failed AND the restore ALSO
                    # failed, so the strategy is now UNLOADED with nothing
                    # replacing it. Fail loudly with a DISTINCT terminal state
                    # instead of a plain ROLLED_BACK that would imply the prior
                    # configuration was cleanly restored.
                    logging.error(
                        'strategy %r left UNLOADED after a failed params swap AND '
                        'a failed restore (command %s) -- restart/manual '
                        'intervention required', strategy_name, command_id,
                    )
                    return self._record(
                        command_id, strategy_name, action, 'ROLLBACK_FAILED', current,
                        error=f'swap failed: {ex}; restore failed: {restore_error}; '
                              f'strategy left unloaded',
                    )
                return self._record(command_id, strategy_name, action, 'ROLLED_BACK', current, error=str(ex))
            self._revisions.mark_committed(revision_id)
        elif action == 'enable_strategy':
            if self.get_strategy(strategy_name) is None:
                raise ValueError(f'strategy {strategy_name!r} not found')
            self.enable_strategy(strategy_name)
        elif action == 'disable_strategy':
            if self.get_strategy(strategy_name) is None:
                raise ValueError(f'strategy {strategy_name!r} not found')
            self.disable_strategy(strategy_name)

        def _commit(conn):
            control = self._revisions.bump_control_revision_in_tx(conn, strategy_name)
            state = self._revisions.bump_state_revision_in_tx(
                conn, strategy_name, self._state_payload(strategy_name, control),
            )
            return self._revisions.record_receipt_in_tx(
                conn, command_id, strategy_name, action, 'COMMITTED', control, state,
            )

        return self._revisions.db.transaction(_commit)

    def _config_entries(self, strategy_name: str, params: Dict) -> tuple[Dict, Dict]:
        """Return ``(prior_entry, proposed_entry)`` -- the strategy's CURRENT
        YAML block and a COPY with ``params`` merged in, mirroring
        ``update_strategy_params``'s own merge semantics (empty-string value
        deletes that key)."""
        with open(self.strategy_config_file) as f:
            cfg = yaml.safe_load(f) or {}
        entries = cfg.get('strategies') or []
        entry = next((e for e in entries if e.get('name') == strategy_name), None)
        if entry is None:
            raise ValueError(f'strategy {strategy_name!r} not found in {self.strategy_config_file}')

        prior_entry = dict(entry)
        merged = dict(entry.get('params') or {})
        for key, raw in (params or {}).items():
            if not key:
                continue
            if isinstance(raw, str) and raw.strip() == '':
                merged.pop(key, None)
                continue
            merged[key] = self._coerce_param_value(raw)

        proposed_entry = dict(entry)
        if merged:
            proposed_entry['params'] = merged
        else:
            proposed_entry.pop('params', None)
        return prior_entry, proposed_entry

    def _staged_path(self) -> str:
        return self.strategy_config_file + '.tmp'

    def _stage_yaml(self, proposed_entry: Dict) -> None:
        """Write the FULL config, with ``proposed_entry`` swapped in for its
        strategy, to the staged ``.tmp`` path -- NOT yet renamed onto the
        live config file."""
        with open(self.strategy_config_file) as f:
            cfg = yaml.safe_load(f) or {}
        entries = cfg.get('strategies') or []
        name = proposed_entry.get('name')
        cfg['strategies'] = [proposed_entry if e.get('name') == name else e for e in entries]
        with open(self._staged_path(), 'w') as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

    def _unstage_yaml(self) -> None:
        try:
            os.remove(self._staged_path())
        except OSError:
            pass

    def _swap_runtime(self, strategy_name: str, config_entry: Dict) -> None:
        """Validate + instantiate the replacement strategy instance from
        ``config_entry`` -- reuses ``update_strategy_params``'s own hot-swap
        body. Raises ``RuntimeError`` if the replacement fails to load
        (``load_strategy`` swallows load failures internally and simply
        never appends the new instance -- this is what turns that silent
        failure into a loud one the caller can catch and roll back)."""
        merged = config_entry.get('params') or {}
        old = self.get_strategy(strategy_name)
        if old is not None:
            self.strategy_implementations.remove(old)
            for lst in self.strategies.values():
                if old in lst:
                    lst.remove(old)
            self._last_dispatched_bar = {
                k: v for k, v in self._last_dispatched_bar.items() if k[1] != strategy_name}
            sys.modules.pop(f'_mmr_strategy_{strategy_name}', None)

        self.load_strategy(
            name=strategy_name,
            bar_size_str=config_entry.get('bar_size', '1 min'),
            conids=config_entry.get('conids'),
            universe=config_entry.get('universe'),
            historical_days_prior=config_entry.get('historical_days_prior', 0),
            module=config_entry.get('module', ''),
            class_name=config_entry.get('class_name', ''),
            description=config_entry.get('description', ''),
            paper_only=config_entry.get('paper_only', False),
            auto_execute=config_entry.get('auto_execute', False),
            params=merged,
        )
        new = self.get_strategy(strategy_name)
        if new is None:
            raise RuntimeError(
                f'strategy {strategy_name!r} failed to reload with the proposed '
                f'configuration -- rolling back')

        # Deliberately NO RPC here -- see update_strategy_params's identical
        # rationale: this runs while the coordinator's forward() call is
        # in flight, so calling back into the trader would deadlock.
        for conId in (new.conids or []):
            bucket = self.strategies.get(conId)
            if bucket is not None and new not in bucket:
                bucket.append(new)

    def _restore_runtime(self, strategy_name: str, prior_entry: Dict) -> Optional[str]:
        """Best-effort restoration of the PRIOR configuration after a failed
        swap. Never re-raises -- the caller is already inside a rollback
        path building a receipt; a secondary failure here is logged loudly
        rather than masking the original error. Returns ``None`` on success,
        or the restore-error text so the caller can surface a distinct
        ROLLBACK_FAILED receipt when the strategy is left unloaded."""
        try:
            self._swap_runtime(strategy_name, prior_entry)
            return None
        except Exception as ex:
            logging.error(
                'failed to restore prior configuration for %s after a rejected '
                'params update -- the strategy may be left unloaded until the '
                'next service restart', strategy_name, exc_info=True,
            )
            return str(ex)

    def _state_payload(self, strategy_name: str, control_revision: int) -> Dict:
        strategy = self.get_strategy(strategy_name)
        state_name = strategy.state.name if strategy is not None else 'UNKNOWN'
        return {
            'strategy_name': strategy_name,
            'state': state_name,
            'control_revision': control_revision,
        }

    def _record(
        self, command_id: str, strategy_name: str, action: str, state: str,
        control_revision: int, error: Optional[str] = None,
    ) -> StrategyCommandReceipt:
        """Record a receipt OUTSIDE the shared commit transaction -- the
        ROLLED_BACK path, where no revision is minted (``control_revision``
        is whatever the caller already had; ``state_revision`` is read as
        the CURRENT value, unchanged since nothing bumped it)."""
        current_state = self._revisions.state_revision(strategy_name)

        def _tx(conn):
            return self._revisions.record_receipt_in_tx(
                conn, command_id, strategy_name, action, state,
                control_revision, current_state, error=error,
            )
        return self._revisions.db.transaction(_tx)

    def recover_startup_config(self) -> list[int]:
        """[M1-F3] Task 7 startup recovery: every staged config revision left
        ``PREPARED`` by a crash between staging and commit is restored to
        its ``prior_config`` in the live YAML and marked ``ROLLED_BACK``.
        MUST run before the service reports ready / the first
        ``config_loader`` call (see ``run()``).

        Restore-then-flip: a revision is flipped to ``ROLLED_BACK`` ONLY after
        its live YAML has been successfully restored. A failed restore leaves
        the revision ``PREPARED`` (retried on the next startup) and raises
        ``StartupConfigRecoveryError`` so THIS startup refuses to come up on a
        config whose real state is unknown -- never eagerly rolling back
        (which would lose the retry and falsify the audit trail)."""
        if self._revisions is None:
            return []
        recovered: list[int] = []
        failures: list[tuple[int, str]] = []
        for revision_id in self._revisions.prepared_config_revision_ids():
            row = self._revisions.get_config_revision(revision_id)
            if row is None:
                continue
            try:
                self._stage_yaml(row['prior_config'])
                os.replace(self._staged_path(), self.strategy_config_file)
            except Exception as ex:
                # Fail loud, not silently inconsistent: leave this revision
                # PREPARED (do NOT flip it) so the next startup retries the
                # restore, best-effort clean up the stale .tmp, record the
                # failure, and re-raise below so the service refuses to come
                # up on a live YAML still holding un-committed proposed values.
                self._unstage_yaml()
                logging.error(
                    'failed to restore prior_config for strategy %s (revision %s) '
                    'during startup recovery -- left PREPARED for retry',
                    row['strategy_name'], revision_id, exc_info=True,
                )
                failures.append((revision_id, str(ex)))
                continue
            # Live YAML restored -> only NOW is it safe to record the rollback.
            self._revisions.mark_rolled_back(revision_id)
            recovered.append(revision_id)
        if failures:
            raise StartupConfigRecoveryError(failures)
        return recovered

    def __get_enabled_strategies(self, conid: int) -> List[Strategy]:
        if conid in self.strategies:
            return [strategy for strategy in self.strategies[conid]
                    if strategy.state == StrategyState.RUNNING or strategy.state == StrategyState.WAITING_HISTORICAL_DATA]
        return []

    @log_method
    def get_strategies(self) -> List[Strategy]:
        return self.strategy_implementations

    def _cap_tick_stream(self, conId: int) -> None:
        """Bound the raw tick buffer to the retention window so resampling stays
        cheap over a long session."""
        df = self.streams.get(conId)
        if df is None or df.empty:
            return
        try:
            cutoff = df.index[-1] - pd.Timedelta(days=self._tick_retention_days)
            if df.index[0] < cutoff:
                self.streams[conId] = df.loc[df.index >= cutoff]
        except Exception:
            pass

    def _prime_hist_bars(self, conId: int, bar_size: BarSize) -> None:
        """One-time load of recent historical OHLCV bars for (conId, bar_size)
        from the DB into the priming cache, normalized to the live schema so it
        concatenates cleanly with resampled ticks. Marks the key as primed even
        on no-data so we don't re-read the DB on every tick."""
        from trader.data.duckdb_store import DuckDBDataStore
        from trader.data.market_data import normalize_historical
        key = (conId, bar_size)
        self._hist_bars[key] = pd.DataFrame()   # mark primed (default empty)
        try:
            ds = DuckDBDataStore(self.history_duckdb_path)
            end = dt.datetime.now(dt.timezone.utc)
            start = end - dt.timedelta(days=max(self._tick_retention_days, 5) + 5)
            df = ds.read(str(conId), start=start, end=end, bar_size=str(bar_size))
            if df is not None and not df.empty:
                norm = normalize_historical(df)
                idx = norm.index
                norm.index = idx.tz_localize('UTC') if idx.tz is None else idx.tz_convert('UTC')
                self._hist_bars[key] = norm
        except Exception as ex:
            logging.warning('could not prime hist bars for conId %s %s: %s', conId, bar_size, ex)

    def _strategy_frame(self, conId: int, bar_size: BarSize) -> Optional[pd.DataFrame]:
        """The OHLCV frame a bar-based strategy should see: historical priming
        bars + the live tick stream resampled to `bar_size` (completed bars only).
        This is what makes bar strategies work live — previously they were handed
        the raw per-tick, cumulative-volume stream and couldn't compute bars."""
        from trader.data.market_data import resample_ticks_to_bars
        key = (conId, bar_size)
        if key not in self._hist_bars:
            self._prime_hist_bars(conId, bar_size)
        try:
            freq = BarSize.to_pandas_freq(bar_size)
        except Exception:
            return self.streams.get(conId)   # unknown freq: legacy raw stream

        def _utc(df):
            if df is None or df.empty:
                return None
            idx = df.index
            if idx.tz is None:
                df = df.copy(); df.index = idx.tz_localize('UTC')
            return df

        hist = self._hist_bars.get(key)
        ticks = self.streams.get(conId)
        live = resample_ticks_to_bars(ticks, freq) if ticks is not None and not ticks.empty else None
        frames = [f for f in (_utc(hist), _utc(live)) if f is not None and not f.empty]
        if not frames:
            return None
        if len(frames) == 1:
            return frames[0].sort_index()
        combined = pd.concat(frames)
        return combined[~combined.index.duplicated(keep='last')].sort_index()

    def on_ticker_next(self, ticker: Ticker):
        if ticker.contract:
            logging.debug('StrategyRuntime.on_ticker_next({} {})'.format(ticker.contract.symbol, ticker.contract.conId))
        else:
            logging.debug('StrategyRuntime.on_ticker_next()')

        conId = 0

        if not ticker.contract:
            logging.debug('no contract associated with Ticker')
            return
        else:
            conId = ticker.contract.conId

        # populate the raw tick buffer, then bound it so resampling stays cheap
        normalized = normalize_ticker(ticker)
        if conId not in self.streams:
            self.streams[conId] = normalized
        else:
            self.streams[conId] = pd.concat([self.streams[conId], normalized], axis=0, copy=False)
        self._cap_tick_stream(conId)

        # Execute the strategies attached to the conId. CRITICAL: each strategy
        # is isolated in its own try/except. Without this, one strategy raising
        # (e.g. a pandas IndexError on a short window) propagates all the way up
        # to the pubsub subscriber loop, which calls on_error and permanently
        # DETACHES this observer from the ticker subject — every subsequent tick
        # for ALL strategies is then silently dropped and open positions go
        # unmanaged. A single misbehaving strategy must not take down the feed.
        for strategy in self.__get_enabled_strategies(conId):
            try:
                # Hand the strategy proper OHLCV bars (historical priming +
                # resampled live ticks), and only when a NEW completed bar has
                # formed — so bar-based strategies see each bar once, matching
                # the backtest, instead of the raw per-tick cumulative-volume
                # stream re-evaluated on every tick.
                frame = self._strategy_frame(conId, strategy.bar_size)
                if frame is None or frame.empty:
                    continue
                last_bar = frame.index[-1]
                dkey = (conId, strategy.name)
                if self._last_dispatched_bar.get(dkey) == last_bar:
                    continue
                self._last_dispatched_bar[dkey] = last_bar
                signal = strategy.on_prices(frame)
            except Exception as ex:
                logging.exception(
                    'strategy %s raised on_prices for conId %s; disabling it and '
                    'continuing the tick feed', getattr(strategy, 'name', '?'), conId)
                try:
                    strategy.state = StrategyState.ERROR
                except Exception:
                    pass
                continue

            # Time-based exits (max_hold_bars / close_by_time) are checked on
            # every new completed bar, signal or not — a VwapReclaim-style
            # position must flatten at 15:45 even if no fresh signal fires.
            self._maybe_check_exits(strategy, conId, frame)

            if not signal:
                continue
            try:
                self._dispatch_signal(strategy, signal, conId=conId, frame=frame)
            except Exception:
                # A failure persisting/publishing one signal must not kill the
                # feed or the other strategies either.
                logging.exception(
                    'failed to record/publish signal from %s for conId %s',
                    getattr(strategy, 'name', '?'), conId)

    def _dispatch_signal(self, strategy: Strategy, signal, conId: int,
                         frame: pd.DataFrame) -> None:
        """Record, publish, and (in propose mode) bridge one signal."""
        if signal.action == Action.BUY:
            logging.info('BUY signal from %s', strategy.name)
        elif signal.action == Action.SELL:
            logging.info('SELL signal from %s', strategy.name)

        # Stamp the instrument on the signal — downstream consumers (event
        # store, MessageBus subscribers, proposal bridge) need to know WHICH
        # contract fired, and strategies don't set it themselves.
        if not signal.conid:
            signal.conid = conId

        # Persist signal to event store
        event = TradingEvent(
            event_type=EventType.SIGNAL,
            timestamp=dt.datetime.now(),
            strategy_name=signal.source_name,
            conid=conId,
            action=str(signal.action),
            signal_probability=signal.probability,
            signal_risk=signal.risk,
        )
        self.event_store.append(event)

        # Publish signal via MessageBus for cross-strategy use and subscribers
        self.zmq_messagebus_client.write('signal', signal)

        # auto_execute: propose — signal becomes a PENDING proposal awaiting
        # human approval (dashboard / `mmr approve`). Guarded separately so a
        # bridge failure never blocks the record/publish path above.
        proposer = getattr(self, 'signal_proposer', None)
        if proposer is not None and strategy.auto_execute == 'propose':
            try:
                proposer.on_signal(strategy.name, signal, frame)
            except Exception:
                logging.exception(
                    'signal→proposal bridge failed for %s conId %s',
                    getattr(strategy, 'name', '?'), conId)

    def _maybe_check_exits(self, strategy: Strategy, conId: int,
                           frame: pd.DataFrame) -> None:
        """Bridge hook: propose time-based exit closes for propose-mode
        strategies. Self-guarded — never propagates into the tick feed."""
        proposer = getattr(self, 'signal_proposer', None)
        if proposer is None or strategy.auto_execute != 'propose':
            return
        try:
            proposer.check_exits(strategy.name, conId, frame)
        except Exception:
            logging.exception(
                'exit check failed for %s conId %s',
                getattr(strategy, 'name', '?'), conId)

    def on_ticker_error(self, ex: Exception):
        logging.error('StrategyRuntime ticker stream error: %s', ex, exc_info=True)

    def on_ticker_completed(self):
        logging.debug('StrategyRuntime.on_completed')

    def subscribe(self, strategy: Strategy, contract: Contract) -> None:
        logging.debug('strategy_runtime.subscribe() contract: {} strategy: {}'.format(contract, strategy))
        if contract.conId not in self.strategies:
            self.strategies[contract.conId] = []
            self.strategies[contract.conId].append(strategy)
            self.trader_client.rpc().publish_contract(contract=contract, delayed=False)
        elif contract.conId in self.strategies and strategy not in self.strategies[contract.conId]:
            self.strategies[contract.conId].append(strategy)

    def subscribe_universe(self, strategy: Strategy, universe_name: str) -> None:
        logging.debug('strategy_runtime.subscribe_universe() universe: {} strategy: {}'.format(universe_name, strategy))
        universe = self.universe_accessor.get(universe_name)

        for security in universe.security_definitions:
            self.subscribe(strategy, SecurityDefinition.to_contract(security))

    def load_strategy(
        self,
        name: str,
        bar_size_str: str,
        conids: Optional[List[int]],
        universe: Optional[str],
        historical_days_prior: int,
        module: str,
        class_name: str,
        description: str,
        paper_only: bool = False,
        auto_execute: 'bool | str' = False,
        params: Optional[Dict] = None,
    ) -> None:

        # Skip if strategy with this name already loaded
        if any(s.name == name for s in self.strategy_implementations):
            logging.debug('strategy {} already loaded, skipping'.format(name))
            return

        if not name or not class_name or not module or not bar_size_str:
            raise ValueError('invalid config. need name, bar_size, class_name and module specified')

        # auto_execute is a safety-relevant knob: accepting a value we don't
        # implement (and silently doing nothing) violates fail-loudly. Only
        # 'propose' (signal → PENDING proposal, human approves) is supported.
        if auto_execute not in (False, None, '', 'propose'):
            logging.error(
                'refusing to load strategy %s: auto_execute=%r is not supported. '
                "Full auto-execution is not implemented — use auto_execute: 'propose' "
                '(signal creates a PENDING proposal for dashboard approval) or remove '
                'the flag.', name, auto_execute,
            )
            return

        # paper_only gate: refuse to load strategies marked paper_only when the
        # trader_service is bound to a live account. Routing is service-level
        # (one trader_service → one IB account), so this is the only place it
        # makes sense to enforce the flag.
        if paper_only and not self.paper_trading:
            logging.error(
                'refusing to load strategy %s: paper_only=True but trader_service '
                'is running in LIVE mode', name,
            )
            return

        strategies_dir = os.path.abspath(os.path.expanduser(self.strategies_directory))

        def load_class_from_file(filename, classname):
            # Reject absolute paths and path traversal. Strategy modules must
            # live under ``strategies_directory`` — otherwise a malicious YAML
            # could load any .py on disk.
            requested = os.path.expanduser(filename)
            if os.path.isabs(requested):
                # Allow absolute paths only if they resolve inside strategies_dir
                filepath = os.path.abspath(requested)
            else:
                filepath = os.path.abspath(os.path.join(strategies_dir, requested))
                # Also accept a project-root-relative path like "strategies/foo.py"
                if not os.path.exists(filepath):
                    filepath = os.path.abspath(requested)

            if not filepath.startswith(strategies_dir + os.sep) and filepath != strategies_dir:
                raise ValueError(
                    f'strategy module {filename!r} resolves outside strategies '
                    f'directory {strategies_dir!r}; refusing to load'
                )

            if not os.path.exists(filepath):
                raise FileNotFoundError(f'strategy module not found: {filepath}')

            # Namespace the module key by the strategy NAME (unique) rather
            # than the filename, so two strategies with the same basename
            # (e.g. strategies/a/ma.py and strategies/b/ma.py) don't clobber
            # each other in sys.modules and reloads evict the previous copy.
            module_name = f'_mmr_strategy_{name}'
            sys.modules.pop(module_name, None)

            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if not spec or not spec.loader:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            return getattr(module, classname, None)

        try:
            class_object = load_class_from_file(module, class_name)
            if not class_object:
                return

            if inspect.isclass(class_object) and issubclass(class_object, Strategy) and class_object is not Strategy:
                logging.debug('found implementation of Strategy {}'.format(class_object))

                instance = class_object()
                context = StrategyContext(
                    name=name,
                    bar_size=BarSize.parse_str(bar_size_str),
                    conids=conids if conids else [],
                    universe=universe,
                    historical_days_prior=historical_days_prior if historical_days_prior else 0,
                    paper_only=paper_only,
                    storage=self.storage,
                    universe_accessor=self.universe_accessor,
                    logger=logging,
                    module=module,
                    class_name=class_name,
                    description=description,
                    auto_execute=auto_execute,
                    params=params if params else {},
                )
                instance.install(context)
                # Give the strategy a reference to the runtime for subscriptions
                instance.strategy_runtime = self

                # Restore the persisted enabled/disabled state so a runtime
                # enable/disable survives a service restart. Unset (None) leaves
                # the strategy INSTALLED, as before.
                persisted = self._load_enabled(name)
                if persisted is True:
                    instance.enable()
                elif persisted is False:
                    instance.disable()

                self.strategy_implementations.append(cast(Strategy, instance))

        except Exception as ex:
            # Load failures used to be swallowed at DEBUG; a config typo could
            # silently disable a strategy. Log at ERROR with the cause so the
            # operator sees it.
            logging.error('failed to load strategy %s (%s): %s', name, class_name, ex)

    def config_loader(self, config_file: str):
        config_file = os.path.expanduser(config_file)
        logging.debug('loading config file {}'.format(config_file))
        # safe_load refuses Python-object tags — YAML-injection hardening.
        with open(config_file, 'r') as conf_file:
            config = yaml.safe_load(conf_file)
        if not config or 'strategies' not in config:
            logging.warning('strategy config %s has no strategies section', config_file)
            return

        for strategy_config in config['strategies']:
            self.load_strategy(
                name=strategy_config['name'],
                bar_size_str=strategy_config['bar_size'],
                conids=strategy_config.get('conids'),
                universe=strategy_config.get('universe'),
                historical_days_prior=strategy_config.get('historical_days_prior', 1),
                module=strategy_config.get('module', ''),
                class_name=strategy_config.get('class_name', ''),
                description=strategy_config.get('description', ''),
                paper_only=strategy_config.get('paper_only', False),
                auto_execute=strategy_config.get('auto_execute', False),
                params=strategy_config.get('params', {}),
            )

    async def _reconcile(self):
        """Re-check config and subscriptions. Safe to call repeatedly (idempotent).

        The body is synchronous (blocking RPC to trader_service for config
        reload + per-conId resolve + per-contract publish). We offload the
        whole thing to a thread so the event loop stays responsive — a
        portfolio universe with ~10 conIds used to stall the loop for
        ~1s every 30s, which surfaced as an asyncio "slow callback"
        warning and stalled live ticker dispatch.

        Runs the stale-proposal expiry sweep first, on every invocation —
        this is what makes expiry time-driven (every ~30s reconcile tick)
        rather than only firing when a strategy happens to emit a fresh
        signal. This method is also the target of the ``reload_strategies``
        RPC and can fire before ``run()``'s initial historical fetch
        completes, so it doubles as the "startup reconciliation" path.

        The sweep is a synchronous DuckDB UPDATE whose ``execute_atomic``
        retries for up to ~45s under file-lock contention, so it is
        offloaded to a thread (like ``_reconcile_sync``) — running it on the
        loop would stall live ticker dispatch. It is also isolated in its
        own try/except: a proposal-store failure must not skip the config
        reload + re-subscription work that follows.
        """
        try:
            await asyncio.to_thread(self.signal_proposer.expire_stale)
        except Exception as ex:
            logging.warning('proposal expiry sweep failed (will retry next cycle): %s', ex)
        await asyncio.to_thread(self._reconcile_sync)

    def _reconcile_sync(self):
        """Synchronous reconcile body. Called from ``_reconcile`` via
        ``asyncio.to_thread``; safe to call directly from non-async contexts."""
        # 1. Check for config file changes. If the YAML is mid-write when we
        # try to parse it, keep the old mtime so we retry on the next tick
        # rather than accepting a partial load.
        try:
            current_mtime = os.path.getmtime(self.strategy_config_file)
        except OSError:
            current_mtime = self._config_mtime

        if current_mtime != self._config_mtime:
            logging.info('strategy config changed, reloading')
            try:
                self.config_loader(self.strategy_config_file)
            except (yaml.YAMLError, ValueError, FileNotFoundError) as ex:
                logging.error(
                    'failed to reload strategy config (will retry next cycle): %s', ex,
                )
                # Don't advance _config_mtime — re-try on next reconcile
                return
            self._config_mtime = current_mtime

        # 2. Re-subscribe all strategies (idempotent — only new conIds trigger publish_contract).
        # Only swallow the well-known transient failures (trader_service bouncing,
        # RPC timeout, socket not-yet-connected). Any other exception is a real
        # bug and should propagate to the run() error handler so it gets logged
        # at ERROR rather than silently masked at DEBUG.
        try:
            for strategy in self.strategy_implementations:
                if strategy.conids:
                    for conId in strategy.conids:
                        security_definitions = self.trader_client.rpc().resolve_symbol(conId)
                        if security_definitions:
                            self.subscribe(strategy, SecurityDefinition.to_contract(security_definitions[0]))

                if strategy.universe:
                    self.subscribe_universe(strategy, strategy.universe)
        except (TimeoutError, ConnectionError) as ex:
            logging.debug('reconciliation RPC failed (trader_service may be restarting): %s', ex)

        # 3. [M1-F3] Task 7: drain any acknowledgement-outbox rows the trader
        # might have missed (its record_state_acknowledged reply was lost, or
        # this process restarted before draining). Isolated: a drain failure
        # must not skip config reload/re-subscription above, and every row is
        # retried independently so one bad row doesn't block the rest.
        try:
            self._drain_ack_outbox()
        except Exception as ex:
            logging.warning('ack-outbox drain failed (will retry next cycle): %s', ex)

    def _drain_ack_outbox(self, limit: int = 50) -> None:
        """Push unacknowledged ``strategy_ack_outbox`` rows to the trader's
        typed ``record_state_acknowledged`` command. This is a BACKSTOP --
        the common case already acknowledges synchronously as part of the
        forwarded command's own reply (see
        ``command_coordinator.StrategyControlCommandService._forward``); this
        loop only matters when that reply was lost in transit. Per-row
        isolated: one row's failure must not block the rest, and an
        unacknowledged row is simply retried on the next 30s tick."""
        client = getattr(self, '_trader_command_client', None)
        if self._revisions is None or client is None:
            return
        for row in self._revisions.unacknowledged_outbox(limit):
            body = {
                'strategy_name': row.strategy_name,
                'state_revision': row.state_revision,
                'control_revision': row.control_revision,
                'payload': row.payload,
            }
            try:
                client.call('record_state_acknowledged', body, dict)
            except Exception as ex:
                logging.debug(
                    'record_state_acknowledged failed for %s state_revision %s '
                    '(will retry next cycle): %s', row.strategy_name, row.state_revision, ex,
                )
                continue
            self._revisions.mark_acknowledged(row.ack_id)

    async def _reconnect_historical_client(self):
        """Disconnect and reconnect the IB historical data client."""
        logging.info('reconnecting historical data IB client')
        try:
            self.historical_data_client.shutdown()
        except Exception:
            pass
        await self.historical_data_client.connect_async()

    @staticmethod
    def _try_get_exchange_calendar(security: Optional[SecurityDefinition]):
        """Best-effort lookup of an exchange_calendars Calendar for a security.

        Tries primaryExchange first (e.g. NASDAQ, ARCA) then falls back to
        the IB exchange field (often SMART, which has no calendar). Returns
        None if neither resolves — callers should treat None as "no
        calendar, skip the missing-range optimization and pull the full
        window."
        """
        if not security:
            return None
        try:
            return exchange_calendars.get_calendar(security.primaryExchange)
        except Exception:
            try:
                return exchange_calendars.get_calendar(security.exchange)
            except Exception:
                return None

    async def _fetch_history_with_resume(
        self,
        security: SecurityDefinition,
        bar_size: BarSize,
        historical_days: int,
        strategy_name: str,
    ):
        """Fetch historical bars only for the date ranges not already in DuckDB.

        Mirrors the cache-aware pattern in data_service: ask TickStorage
        which calendar days inside the requested window are missing, then
        pull *just those* from IB and write the result back. This turns a
        90-day backfill on every strategy_service restart into a no-op
        once the local store is warm.

        Errors:
          * IBNoDataError    -> swallowed (logged at warning); some IB
                                contracts genuinely have no history.
          * IBConnectivityError -> propagated; caller decides whether to
                                reconnect and retry.
        """
        contract = SecurityDefinition.to_contract(security)
        what_to_show = _whattoshow_for_contract(contract)

        tick_data = self.storage.get_tickdata(bar_size=bar_size)
        tz = security.timeZoneId or 'US/Eastern'
        # dateify() with timezone= returns a tz-aware dt.datetime even
        # when given a naive datetime or a dt.date.
        window_start = dateify(
            dt.datetime.now() - dt.timedelta(days=historical_days),
            timezone=tz, make_sod=True,
        )
        window_end = dateify(dt.datetime.now(), timezone=tz, make_eod=True)

        cal = self._try_get_exchange_calendar(security)
        if cal is not None:
            try:
                date_ranges = tick_data.missing(
                    security, cal,
                    date_range=DateRange(start=window_start, end=window_end),
                )
            except Exception as ex:
                logging.warning(
                    'tick_data.missing() failed for %s strategy %s: %s — '
                    'falling back to full-window pull',
                    security.symbol, strategy_name, ex,
                )
                date_ranges = [DateRange(start=window_start, end=window_end)]
        else:
            # No calendar -> can't compute trading-day gaps. Pull the full
            # window. tick_data.write() upserts so we still won't double-store.
            date_ranges = [DateRange(start=window_start, end=window_end)]

        if not date_ranges:
            logging.debug(
                'history cache hit for %s (%s, %sd) — skipping IB fetch',
                security.symbol, strategy_name, historical_days,
            )
            return

        for dr in date_ranges:
            # tick_data.missing() returns DateRanges whose start/end are
            # bare dt.date objects (from exchange_calendars sessions.date)
            # — despite DateRange being type-annotated dt.datetime. The
            # IB worker expects tz-aware datetimes (it reads .tzinfo on
            # the input), so promote here before the call.
            dr_start = dateify(dr.start, timezone=tz, make_sod=True)
            dr_end = dateify(dr.end, timezone=tz, make_eod=True)
            try:
                df = await self.historical_data_client.get_contract_history(
                    security=contract,
                    what_to_show=what_to_show,
                    bar_size=bar_size,
                    start_date=dr_start,
                    end_date=dr_end,
                )
            except IBNoDataError as ex:
                logging.warning(
                    'no historical data for %s (%s) %s..%s strategy %s: %s',
                    security.symbol, security.conId,
                    dr.start, dr.end, strategy_name, ex,
                )
                continue

            if df is not None and len(df) > 0:
                try:
                    tick_data.write(security, df)
                    logging.debug(
                        'wrote %d bars for %s (%s) strategy %s',
                        len(df), security.symbol, security.conId, strategy_name,
                    )
                except Exception as ex:
                    logging.warning(
                        'tick_data.write() failed for %s strategy %s: %s',
                        security.symbol, strategy_name, ex,
                    )

    async def get_historical_data(self):
        for strategy in self.strategy_implementations:
            historical_days = strategy.historical_days_prior if strategy.historical_days_prior else 1

            if strategy.conids:
                for conId in strategy.conids:
                    security_definitions = self.trader_client.rpc().resolve_symbol(conId)
                    if security_definitions:
                        try:
                            await self._fetch_history_with_resume(
                                security=security_definitions[0],
                                bar_size=strategy.bar_size,
                                historical_days=historical_days,
                                strategy_name=strategy.name,
                            )
                        except IBConnectivityError:
                            raise
                    else:
                        logging.error('could not find security definition for conId {} for strategy {}'.format(conId, strategy))

            if strategy.universe:
                # Iterate SecurityDefinitions directly so we can pass them to
                # _fetch_history_with_resume (which needs primaryExchange,
                # timeZoneId, etc. for calendar lookup and missing-range
                # computation; a bare Contract(conId=...) wouldn't suffice).
                for sd in self.universe_accessor.get(strategy.universe).security_definitions:
                    try:
                        await self._fetch_history_with_resume(
                            security=sd,
                            bar_size=strategy.bar_size,
                            historical_days=historical_days,
                            strategy_name=strategy.name,
                        )
                    except IBConnectivityError:
                        raise
        logging.debug('finished get_historical_data()')

    async def run(self):
        logging.info('starting strategy_runtime')
        logging.debug('StrategyRuntime.run()')

        # Async setup that used to happen inside connect() via asyncio.run():
        # we now do it here so the tasks land on the real service loop and
        # actually get a chance to run.
        await self.zmq_messagebus_client.connect()
        await self.zmq_strategy_rpc_server.serve()
        await self.typed_command_server.serve()
        await self.typed_query_server.serve()
        self._trader_command_client.connect()
        self._trader_query_client.connect()

        await self.trader_client.connect()

        self.zmq_subscriber = TopicPubSub[Ticker](
            self.zmq_pubsub_server_address,
            self.zmq_pubsub_server_port,
        )

        logging.debug('subscribing to tick stream')
        observable = await self.zmq_subscriber.subscriber('ticker')
        self.observer = AutoDetachObserver(
            on_next=self.on_ticker_next,
            on_error=self.on_ticker_error,
            on_completed=self.on_ticker_completed
        )
        self.subscription = observable.subscribe(self.observer)

        # [M1-F3] Task 7: recover any staged config swap left PREPARED by a
        # crash between staging and commit -- MUST run before the first
        # config_loader() call below reads the (possibly still-staged) YAML.
        self.recover_startup_config()

        logging.debug('loading {} config file'.format(self.strategy_config_file))
        self.config_loader(self.strategy_config_file)

        logging.debug('subscribing to streams for all conids')

        # todo: i'm not sure the runtime should automagically subscribe here.
        # it's probably up to the strategy how they want to secure data
        for strategy in self.strategy_implementations:
            if strategy.conids:
                for conId in strategy.conids:
                    security_definitions = self.trader_client.rpc().resolve_symbol(conId)
                    if security_definitions:
                        self.subscribe(strategy, SecurityDefinition.to_contract(security_definitions[0]))
                    else:
                        logging.error('could not find security definition for conId {} for strategy {}. Disabling strategy.'
                                      .format(conId, strategy))
                        strategy.on_error(
                            Exception('could not find security definition for conId {} for strategy {}. Disabling strategy.'
                                      .format(conId, strategy))
                        )

            if strategy.universe:
                self.subscribe_universe(strategy, strategy.universe)

        logging.debug('starting connection to IB for historical data')

        self.historical_data_client = IBHistoryWorker(
            self.ib_server_address,
            self.ib_server_port,
            self.strategy_runtime_ib_client_id + 1,
        )
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                if not self.historical_data_client.connected:
                    await self.historical_data_client.connect_async()
                await self.get_historical_data()
                break
            except IBConnectivityError as ex:
                if attempt == max_retries:
                    logging.error('historical data failed after {} attempts, giving up: {}'.format(max_retries, ex))
                    break
                wait = min(2 ** attempt, 30)
                logging.warning('IB connectivity error (attempt {}/{}), retrying in {}s: {}'.format(
                    attempt, max_retries, wait, ex))
                try:
                    await self._reconnect_historical_client()
                except Exception as reconnect_ex:
                    logging.error('reconnect failed: {}'.format(reconnect_ex))
                await asyncio.sleep(wait)
            except ConnectionError:
                if attempt == max_retries:
                    logging.error('IB not connected after {} attempts, giving up'.format(max_retries))
                    break
                wait = min(2 ** attempt, 30)
                logging.warning('IB not connected (attempt {}/{}), retrying in {}s'.format(
                    attempt, max_retries, wait))
                await asyncio.sleep(wait)
            except Exception as ex:
                logging.error('unexpected error fetching historical data: {}'.format(ex))
                break

        # Track config mtime for change detection
        try:
            self._config_mtime = os.path.getmtime(self.strategy_config_file)
        except OSError:
            self._config_mtime = 0.0

        # Stay alive and periodically reconcile subscriptions. Reconcile
        # ONCE immediately (reconcile-then-sleep, not sleep-then-reconcile)
        # so the stale-proposal expiry sweep runs at startup rather than 30s
        # in — proposals that expired while the service was down get swept on
        # boot. This is idempotent: the inline subscribe block above already
        # ran and _config_mtime was just stamped, so _reconcile_sync's
        # subscribe/reload work is a no-op here; only the expiry sweep does
        # real work on this first pass.
        logging.info('entering reconciliation loop (30s interval)')
        while True:
            try:
                await self._reconcile()
            except Exception as ex:
                logging.error('reconciliation error: {}'.format(ex))
            await asyncio.sleep(30)

