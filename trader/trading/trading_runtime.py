from ib_async.contract import Contract
from ib_async.objects import PnLSingle, PortfolioItem, Position
from ib_async.order import LimitOrder, MarketOrder, Order, StopLimitOrder, StopOrder, Trade
from ib_async.ticker import Ticker
from reactivex import pipe
from reactivex.abc import DisposableBase, ObserverBase
from reactivex.disposable import Disposable
from reactivex.observable import Observable
from reactivex.observer import AutoDetachObserver, Observer
from reactivex.scheduler.eventloop.asynciothreadsafescheduler import AsyncIOThreadSafeScheduler
from reactivex.subject import Subject
from trader.common.contract_sink import ContractSink
from trader.common.dataclass_cache import DataClassCache, DataClassEvent, UpdateEvent
from trader.common.exceptions import trader_exception, TraderConnectionException, TraderException
from trader.common.helpers import ListHelper
from trader.common.logging_helper import get_callstack, log_method, setup_logging
from trader.common.reactivex import AnonymousObserver, SuccessFail

from trader.data.data_access import PortfolioSummary, SecurityDefinition, TickStorage
from trader.data.broker_state import BrokerStateStore, broker_materialized_adapters
from trader.data.domain_journal import DomainJournal
from trader.data.event_store import EventStore, EventType, TradingEvent
from trader.data.market_data import SecurityDataStream
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import Universe, UniverseAccessor
from trader.domain.feed_service import DomainFeedService
from trader.domain.snapshot_service import DomainSnapshotService
from trader.data.duckdb_store import DuckDBConnection
from trader.trading.broker_ingest import BrokerIngest
from trader.trading.risk_gate import RiskGate, RiskLimits
from trader.listeners.ibreactive import IBAIORx, IBAIORxError
from trader.messaging.clientserver import MessageBusServer, MultithreadedTopicPubSub, RPCClient, RPCServer
from trader.objects import Action, ContractOrderPair, ExecutorCondition
from trader.trading.book import BookSubject
from trader.trading.executioner import TradeExecutioner
from trader.trading.portfolio import Portfolio
from trader.trading.strategy import Strategy, StrategyConfig, StrategyState
from typing import cast, Dict, List, NamedTuple, Optional, Tuple, Union

import asyncio
import backoff
import datetime as dt
import os
import reactivex as rx
import reactivex.operators as ops
import threading
import time
import trader.messaging.strategy_service_api as strategy_bus


logging = setup_logging(module_name='trading_runtime')

# notes
# https://groups.io/g/insync/topic/using_reqallopenorders/27261173?p=,,,20,0,0,0::recentpostdate%2Fsticky,,,20,2,0,27261173
# talks about trades/orders being tied to clientId, which means we'll need to always have a consistent clientid


class AccountNotPinnedError(Exception):
    """The trader is not pinned to a valid, mode-matched IB account.

    Raised at connect time to refuse trading when ``ib_account`` is blank,
    not among IB's ``managedAccounts()``, or mismatched with the trading
    mode — any of which could let orders route to the wrong account on a
    multi-account login. A hard, fatal refusal (not retried).
    """


class Trader():
    def __init__(self,
                 ib_server_address: str,
                 ib_server_port: int,
                 trading_runtime_ib_client_id: int,
                 ib_account: str,
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
                 history_duckdb_path: str = '',
                 journal_duckdb_path: str = '',
                 paper_trading: bool = False,
                 simulation: bool = False,
                 require_proposal_approval: bool = False,
                 typed_bind_address: str = 'tcp://127.0.0.1',
                 typed_query_port: int = 42101,
                 typed_command_port: int = 42102,
                 typed_feed_port: int = 42103,
                 service_hmac_key_file: str = '',
                 unsafe_legacy_rpc: bool = False,
                 command_authority: Optional[dict] = None,
                 strategy_typed_command_port: int = 42104,
                 strategy_typed_query_port: int = 42105,
                 strategy_typed_address: str = '',
                 automation_enabled: bool = False,
                 automation_live_enabled: bool = False,
                 automation_artifact_bundle_path: str = '',
                 automation_public_key_ring_path: str = '',
                 automation_expected_artifact_id: str = '',
                 automation_strategy_name: str = ''):
        self.ib_server_address = ib_server_address
        self.ib_server_port = ib_server_port
        self.trading_runtime_ib_client_id = trading_runtime_ib_client_id
        self.ib_account = ib_account
        self.duckdb_path = duckdb_path
        self.history_duckdb_path = history_duckdb_path or duckdb_path
        # Existing installations may predate ``journal_duckdb_path``. Keep
        # them functional while never falling back to the shared operational
        # database, which would reintroduce cross-process DuckDB lockouts.
        self.journal_duckdb_path = journal_duckdb_path or f"{duckdb_path}.journal"
        self.universe_library = universe_library
        self.simulation: bool = simulation
        self.paper_trading = paper_trading
        self.strategy_typed_command_port = int(strategy_typed_command_port)
        self.strategy_typed_query_port = int(strategy_typed_query_port)
        self.strategy_typed_address = strategy_typed_address or ''
        self.automation_enabled = bool(automation_enabled)
        self.automation_live_enabled = bool(automation_live_enabled)
        self.automation_artifact_bundle_path = automation_artifact_bundle_path or ''
        self.automation_public_key_ring_path = automation_public_key_ring_path or ''
        self.automation_expected_artifact_id = automation_expected_artifact_id or ''
        self.automation_strategy_name = automation_strategy_name or ''
        # When True, `place_order_simple` (the direct buy/sell RPC path) is
        # rejected unless the caller explicitly sets `skip_risk_gate=True`
        # (close-all / liquidation). All actionable new trades must come
        # in through `place_expressive_order`, which the approve() CLI /
        # helper use after a proposal is reviewed. Defensive gate against
        # LLM loops drifting off-plan and firing direct orders.
        self.require_proposal_approval: bool = require_proposal_approval
        # Typed authenticated query/command/feed transport (G0 Tasks 2-3) --
        # production's ONLY RPC boundary (see connect()). The legacy
        # dill/msgpack RPCServer is gated behind `unsafe_legacy_rpc` AND
        # `simulation` both being True (validate_rpc_mode enforces this).
        # The interface the typed ROUTER sockets bind to. Default loopback
        # (safe for local/non-Compose); the Compose `trader` service sets
        # TYPED_BIND_ADDRESS=tcp://0.0.0.0 so the published ports and
        # cross-container peers actually reach a listening socket. Without
        # threading this through, the servers inherited TypedRpcServer's own
        # tcp://127.0.0.1 default and the published 42101/42102 refused all
        # connections (G0 Task 5 fix).
        self.typed_bind_address = typed_bind_address
        self.typed_query_port = typed_query_port
        self.typed_command_port = typed_command_port
        self.typed_feed_port = typed_feed_port
        self.service_hmac_key_file = service_hmac_key_file
        self.unsafe_legacy_rpc: bool = unsafe_legacy_rpc
        self.command_authority = dict(command_authority or {})
        self.zmq_pubsub_server_address = zmq_pubsub_server_address
        self.zmq_pubsub_server_port = zmq_pubsub_server_port
        self.zmq_rpc_server_address = zmq_rpc_server_address
        self.zmq_rpc_server_port = zmq_rpc_server_port
        self.zmq_strategy_rpc_server_address = zmq_strategy_rpc_server_address
        self.zmq_strategy_rpc_server_port = zmq_strategy_rpc_server_port
        self.zmq_messagebus_server_address = zmq_messagebus_server_address
        self.zmq_messagebus_server_port = zmq_messagebus_server_port

        # todo you can have up to 24 connections to IB Gateway
        # so we need to take this from single client, to multiple client
        self.client: IBAIORx
        self.data: TickStorage
        self.universe_accessor: UniverseAccessor

        # the live ticker data streams we have
        self.contract_subscriptions: Dict[Contract, ContractSink] = {}
        # the minute-by-minute MarketData stream's we're subscribed to
        self.market_data_subscriptions: Dict[SecurityDefinition, SecurityDataStream] = {}

        # current order book (outstanding orders, trades etc)
        self.book: BookSubject = BookSubject()
        # portfolio (current and past positions)
        self.portfolio: Portfolio = Portfolio()
        # In-memory set of conIds already present in the 'portfolio' universe.
        # Populated lazily on the first update_portfolio_universe() call
        # per session. Lets a burst of N positionEvent emissions (49 on
        # initial connect) short-circuit without 49× DuckDB reads when
        # nothing's actually changing. Huge win: the old path did one
        # `universe_accessor.get + dill.dumps + DuckDB write` per event
        # on the main loop.
        self._known_portfolio_conids: set = set()
        # pnl for current portfolio
        self.pnl: DataClassCache = DataClassCache[PnLSingle](lambda pnl: str((pnl.account, pnl.conId)))
        self.pnl_subscriptions: Dict[Tuple[str, int], bool] = {}
        self._pnl_subscriptions_lock: threading.Lock = threading.Lock()
        # The main event loop, captured on connect(). Used when IB callbacks
        # fire on threads other than the loop thread.
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        # takes care of execution of orders
        self.executioner: TradeExecutioner
        # a list of all the universes of stocks we have registered
        self.market_data = 3
        # Legacy dill/object-returning RPC server -- ONLY constructed in
        # connect() when simulation=True AND unsafe_legacy_rpc=True (see
        # validate_rpc_mode). Absent (never set) in every other posture,
        # including all of production.
        self.zmq_rpc_server: Optional[RPCServer] = None
        # Typed authenticated query/command/feed servers -- ALWAYS started in
        # connect(); this is production's sole RPC boundary.
        self.typed_query_server: 'TypedRpcServer'
        self.typed_command_server: 'TypedRpcServer'
        self.typed_feed_server: 'TypedRpcServer'
        self.typed_authenticator: 'HmacServiceAuthenticator'
        self.zmq_pubsub_server: MultithreadedTopicPubSub
        self.zmq_pubsub_contracts: Dict[int, Observable[IBAIORxError]] = {}
        self.zmq_pubsub_contract_filters: Dict[int, bool] = {}
        self.zmq_pubsub_contract_subscription: DisposableBase = Disposable()
        # conId -> (Contract, delayed). Remembered so live ticker subscriptions
        # can be re-established after an IB reconnect (the old market-data lines
        # die with the previous session). Survives reconnects; reset on a fresh
        # connect(). Without this the strategy tick feed silently dies on reconnect.
        self.zmq_pubsub_published_contracts: Dict[int, Tuple[Contract, bool]] = {}
        # Coalesces the connected_event double-fire (eventkit emit + explicit call
        # on reconnect) so re-subscription doesn't run twice concurrently.
        self._in_connected_event: bool = False
        # Single sink for IB order-status truth (fill/cancel/reject events +
        # acceptance queries). Built here so it exists before the first
        # connected_event/setup_subscriptions runs; its event store is wired in
        # connect() and it's attached to orderStatusEvent in setup_subscriptions.
        from trader.trading.order_lifecycle import OrderLifecycleTracker
        self.order_tracker = OrderLifecycleTracker(None)

        self.zmq_strategy_client: RPCClient[strategy_bus.StrategyServiceApi]
        self.zmq_messagebus: MessageBusServer

        self.startup_time: dt.datetime = dt.datetime.now()
        self.last_connect_time: dt.datetime
        self.load_test: bool = False
        self.tws_client_ids: List[int] = [self.trading_runtime_ib_client_id, self.trading_runtime_ib_client_id + 1]
        self.scheduler: Optional[AsyncIOThreadSafeScheduler] = None

        # IB upstream connectivity tracking
        # These error codes indicate Gateway is connected locally but lost upstream IBKR connection
        self._ib_upstream_connected: bool = True
        self._ib_upstream_error: str = ''

        self.disposables: List[DisposableBase] = []

    def _assert_account_pinned(self, managed: list) -> str:
        """Verify the trader is pinned to exactly one configured account that
        IB actually manages, matching the trading mode. Returns the active
        account or raises ``AccountNotPinnedError``.

        This is the startup half of the account-safety story (the per-order
        half is the guard in ``TradeExecutioner.subscribe_place_order_direct``).
        The IB login can manage multiple accounts (e.g. a client sub-account
        plus a master/aggregate). If ``ib_account`` were blank, every order
        would be built with ``account=''`` and IB would route it to the
        *default* account — potentially the wrong one. So we refuse to start
        unless ``ib_account`` is non-empty AND present in ``managedAccounts()``.
        Paper accounts start with "D" (DU.../DF...); live accounts don't.
        """
        mode = 'paper' if self.paper_trading else 'live'
        if not self.ib_account:
            raise AccountNotPinnedError(
                f'SAFETY: no ib_account configured (trading_mode={mode}, managed={managed}). '
                'Set ib_paper_account / ib_live_account in trader.yaml or the IB_ACCOUNT env var. '
                'Refusing to continue.'
            )
        if not managed:
            raise AccountNotPinnedError(
                f'SAFETY: IB returned no managed accounts; cannot verify ib_account '
                f'"{self.ib_account}". Refusing to continue.'
            )
        if self.ib_account not in managed:
            raise AccountNotPinnedError(
                f'SAFETY: configured ib_account "{self.ib_account}" is not among IB managed '
                f'accounts {managed}. Refusing to continue.'
            )
        is_paper_account = self.ib_account.startswith('D')
        if self.paper_trading and not is_paper_account:
            raise AccountNotPinnedError(
                f'SAFETY: trading_mode is "paper" but ib_account "{self.ib_account}" looks live. '
                f'Managed accounts: {managed}. Refusing to continue.'
            )
        if not self.paper_trading and is_paper_account:
            raise AccountNotPinnedError(
                f'SAFETY: trading_mode is "live" but ib_account "{self.ib_account}" looks like a '
                f'paper account. Managed accounts: {managed}. Check your config.'
            )
        return self.ib_account

    def _fake_broker_enabled(self) -> bool:
        """Whether ``MMR_FAKE_BROKER=1`` activates the stub broker (G0 Task 6).

        FAIL-LOUD safety gate. The fake broker (``IBAIORx.connect_fake``)
        silently accepts and DROPS every order / stop / take-profit while
        reporting ``ib_connected: True`` -- so it must be impossible to
        activate against anything that could reach a real/live account. The
        catastrophic scenario this guards is a live ``docker compose up``
        with ``MMR_FAKE_BROKER=1`` left exported in the shell fabricating a
        healthy broker on a LIVE account and swallowing real trades.

        Returns ``False`` when the flag is unset (normal path -> real
        ``connect()``). When the flag IS set, permits the fake broker ONLY in
        the most restrictive non-live posture -- offline ``simulation`` AND
        ``paper_trading`` AND a paper (``D``-prefixed, e.g. ``DU...``)
        account -- and otherwise RAISES ``AccountNotPinnedError`` (a fatal
        safety refusal that ``connect()`` re-raises un-retried/un-wrapped).

        It deliberately RAISES rather than returning ``False`` in a
        live/misconfigured posture: silently falling back to the real broker
        would mask the misconfiguration, and proceeding with the stub would
        fabricate a healthy live broker that eats trades. A flag set on a
        live process is a bug that must hard-fail, loudly, either way.
        """
        if os.environ.get('MMR_FAKE_BROKER') != '1':
            return False
        if not (self.simulation and self.paper_trading
                and str(self.ib_account).startswith('D')):
            raise AccountNotPinnedError(
                'MMR_FAKE_BROKER refused: the fake broker is permitted ONLY in '
                'offline simulation + paper trading with a paper (D-prefixed) '
                'account. Got simulation={}, paper_trading={}, ib_account={!r}. '
                'Refusing to fabricate a broker in a potentially live posture '
                '(it would silently drop every order).'.format(
                    self.simulation, self.paper_trading, self.ib_account))
        return True

    @backoff.on_exception(backoff.expo, (ConnectionRefusedError, TimeoutError), max_tries=10, max_time=120)
    def connect(self):
        logging.debug('trading_runtime.connect() connecting to services: %s:%s' % (self.ib_server_address, self.ib_server_port))
        try:
            self.client = IBAIORx(
                ib_server_address=self.ib_server_address,
                ib_server_port=self.ib_server_port,
                ib_client_id=self.trading_runtime_ib_client_id,
                ib_account=self.ib_account,
            )
            self.data = TickStorage(self.history_duckdb_path)
            self.universe_accessor = UniverseAccessor(self.duckdb_path, self.universe_library)
            self.clear_portfolio_universe()
            self.contract_subscriptions = {}
            self.market_data_subscriptions = {}

            # [M1-F2] The journal file is owned exclusively by this process.
            # Build its durable broker view before IB can emit a connected
            # callback; callback registration and the initial completeness
            # barrier happen later in setup_subscriptions()/connected_event().
            journal_db = DuckDBConnection.get_instance(self.journal_duckdb_path)
            self.journal_db = journal_db
            journal_migrator = SchemaMigrator(journal_db)
            self.domain_journal = DomainJournal(journal_db)
            self.domain_journal.migrate(journal_migrator)
            self.broker_state_store = BrokerStateStore(journal_db)
            self.broker_state_store.migrate(journal_migrator)
            from trader.trading.risk_producer import ReconciliationProducer
            ReconciliationProducer(journal_db, self.domain_journal).migrate(journal_migrator)
            self.broker_ingest = BrokerIngest(
                db=journal_db,
                journal=self.domain_journal,
                store=self.broker_state_store,
                account_id=self.ib_account,
                account_mode='paper' if self.paper_trading else 'live',
            )
            self.snapshot_service = DomainSnapshotService(self.domain_journal)
            self.snapshot_service.register_broker_generation_reader(
                self.broker_state_store.latest_promoted_generation_in_tx
            )
            for adapter in broker_materialized_adapters(self.broker_state_store):
                self.snapshot_service.register_adapter(adapter)
            self.feed_service = DomainFeedService(self.domain_journal)
            self.client.ib.connectedEvent += self.connected_event
            self.client.ib.disconnectedEvent += self.disconnected_event
            # G0 Task 6: MMR_FAKE_BROKER=1 lets this process boot, report
            # healthy, and stay up WITHOUT a real IB Gateway connection --
            # the fullstack process-supervision gate's only use for this.
            # _fake_broker_enabled() is a FAIL-LOUD safety gate: it raises
            # (never silently falls back to either broker) if the flag is
            # set in any posture that could touch a real/live account, so
            # the else-branch's real connect() is reached only when the flag
            # is genuinely unset. See _fake_broker_enabled for the rules.
            if self._fake_broker_enabled():
                logging.warning(
                    'MMR_FAKE_BROKER=1: skipping real IB Gateway connection -- '
                    'trading_runtime is running against a stub broker (offline '
                    'simulation + paper account only). This must NEVER be set '
                    'outside the fullstack test-profile runner.'
                )
                self.client.connect_fake(self.ib_account)
                # connect_fake does not emit IB connectedEvent; broker sync and
                # the command-center snapshot barrier live in connected_event().
                self._fake_broker_schedule_connected = True
            else:
                self.client.connect()

            # Track IB upstream connectivity via error codes
            self.client.error_subject.subscribe(AnonymousObserver(
                on_next=self._on_ib_error,
                on_error=lambda e: None,
            ))

            # Hard safety gate: refuse to run unless we're pinned to exactly
            # one configured account that IB actually manages (and it matches
            # the trading mode). See _assert_account_pinned for the rationale.
            managed = self.client.ib.managedAccounts()
            try:
                active_account = self._assert_account_pinned(managed)
            except AccountNotPinnedError:
                self.client.ib.disconnect()
                raise
            logging.info('trading mode verified: %s, account: %s', 'paper' if self.paper_trading else 'live', active_account)

            self.last_connect_time = dt.datetime.now()

            # The production command composition root consumes the real risk
            # gate, so initialize it before building the typed registry. The
            # servers still start only after every adapter and executioner are
            # constructed below.
            self.event_store = EventStore(self.duckdb_path)
            self.risk_gate = RiskGate(RiskLimits(), self.event_store)
            self.order_tracker.set_event_store(self.event_store)
            from trader.trading.trading_filter import TradingFilter
            self.risk_gate.trading_filter = TradingFilter.load()

            # --- Production RPC boundary (G0 Task 4) -----------------------
            # Fail-closed guard: the dill-capable legacy RPC path (raw
            # objects, no schema validation) may run ONLY in offline
            # simulation with the explicit unsafe flag. This raises
            # ValueError before anything below binds a single socket if
            # some other code path ever tried to combine unsafe_legacy_rpc
            # with a live/production posture.
            from trader.messaging.production_api import (
                build_production_registry,
                register_strategy_state_ingest,
                validate_rpc_mode,
            )
            from trader.messaging.typed_rpc import (
                HmacServiceAuthenticator,
                TypedRpcServer,
                load_service_hmac_key,
            )
            from trader.trading.command_policy import load_and_validate_command_policy
            from trader.trading.command_stack import build_command_stack

            validate_rpc_mode(self.simulation, self.unsafe_legacy_rpc)

            # Typed query/command/feed servers ALWAYS start -- this is the
            # only RPC surface production exposes. The service HMAC key is
            # loaded (and hardness-checked -- absent/wrong-mode/empty/<32B
            # all fail loudly) unconditionally, offline simulation included:
            # there is no "test mode" bypass for the typed transport's
            # authentication.
            hmac_key = load_service_hmac_key(self.service_hmac_key_file)
            self.typed_authenticator = HmacServiceAuthenticator(hmac_key)

            self.command_authority_policy = load_and_validate_command_policy(
                self.command_authority,
                trader_account_id=self.ib_account,
                paper_trading=self.paper_trading,
            )
            command_stack = build_command_stack(
                self,
                self.command_authority_policy,
                now=lambda: dt.datetime.now(dt.timezone.utc),
            )
            production_registry = build_production_registry(
                self,
                self.typed_authenticator,
                snapshot_service=self.snapshot_service,
                feed_service=self.feed_service,
                command_stack=command_stack,
            )
            if command_stack is not None:
                hot_arm = getattr(command_stack, "paper_hot_arm", None)
                if hot_arm is not None and hasattr(hot_arm, "attach_registry"):
                    hot_arm.attach_registry(production_registry)
            if command_stack is None:
                register_strategy_state_ingest(production_registry, self.domain_journal)
            self.typed_query_server = TypedRpcServer(
                'query', production_registry, self.typed_authenticator,
                address=self.typed_bind_address,
                port=self.typed_query_port,
            )
            # All three servers share the ONE production_registry: it keys
            # handlers by (socket_role, method), and each server resolves only
            # its own role's methods (see TypedRpcRegistry.resolve), so the feed
            # server exposes exactly the feed-role methods (read_domain_events)
            # and nothing from the query/command roles -- no cross-role leak.
            # The feed server MUST use production_registry: build_production_
            # registry registers read_domain_events on role 'feed' there, and
            # the M1-R dashboard bridge long-polls it; an empty registry made
            # every read_domain_events call fail METHOD_NOT_ALLOWED, so the
            # bridge could never tail past the fenced baseline (dashboard stuck
            # resyncing). The command server uses this SAME registry. With
            # authority disabled it exposes only record_state_acknowledged;
            # with authority enabled the fail-closed command stack registers
            # the production-ready command surface as well.
            self.typed_command_server = TypedRpcServer(
                'command', production_registry, self.typed_authenticator,
                address=self.typed_bind_address,
                port=self.typed_command_port,
            )
            self.typed_feed_server = TypedRpcServer(
                'feed', production_registry, self.typed_authenticator,
                address=self.typed_bind_address,
                port=self.typed_feed_port,
            )

            # Legacy dill/object-returning RPC (clientserver.py) -- offline
            # simulation ONLY, and only with the explicit unsafe flag.
            # Production never constructs LegacyOfflineTraderServiceApi or
            # binds this socket at all.
            if self.simulation and self.unsafe_legacy_rpc:
                from trader.messaging.legacy_offline_api import LegacyOfflineTraderServiceApi
                self.zmq_rpc_server = RPCServer[LegacyOfflineTraderServiceApi](
                    instance=LegacyOfflineTraderServiceApi(self),
                    zmq_rpc_server_address=self.zmq_rpc_server_address,
                    zmq_rpc_server_port=self.zmq_rpc_server_port
                )
            # ----------------------------------------------------------------

            self.zmq_pubsub_server = MultithreadedTopicPubSub(
                zmq_pubsub_server_address=self.zmq_pubsub_server_address,
                zmq_pubsub_server_port=self.zmq_pubsub_server_port
            )
            self.zmq_pubsub_server.start()

            self.zmq_messagebus = MessageBusServer(self.zmq_messagebus_server_address, self.zmq_messagebus_server_port)
            self.run(self.zmq_messagebus.start())

            self.zmq_pubsub_contracts = {}
            self.zmq_pubsub_contract_filters = {}
            self.zmq_pubsub_contract_subscription = Disposable()

            # connect to the strategy server
            self.zmq_strategy_client = RPCClient[strategy_bus.StrategyServiceApi](
                self.zmq_strategy_rpc_server_address,
                self.zmq_strategy_rpc_server_port,
                timeout=6,
            )

            self.broker_ingest.start()

            # fire up the executioner
            self.executioner = TradeExecutioner()
            self.executioner.connect(self)

            self.run(self.zmq_strategy_client.connect())
            self.run(self.typed_query_server.serve())
            self.run(self.typed_command_server.serve())
            self.run(self.typed_feed_server.serve())
            if self.zmq_rpc_server is not None:
                self.run(self.zmq_rpc_server.serve())

        except KeyboardInterrupt:
            logging.info('connect() interrupted, shutting down')
            raise
        except (ConnectionRefusedError, TimeoutError):
            # Propagate un-wrapped so the @backoff.on_exception decorator on
            # connect() actually retries them. The old blanket `except Exception`
            # rewrapped these as TraderConnectionException, which backoff doesn't
            # match — so the retry decorator was dead code.
            raise
        except AccountNotPinnedError:
            # Fatal safety refusal — must never be retried or wrapped.
            raise
        except Exception as ex:
            # NOTE: ValueError from validate_rpc_mode() (unsafe_legacy_rpc
            # requested outside offline simulation) and
            # trader.messaging.typed_rpc.ServiceHmacKeyError from
            # load_service_hmac_key (HMAC key file absent, not mode 0600,
            # empty, or <32 bytes) both fall through to here rather than
            # getting a dedicated except clause: either way connect() still
            # fails loudly (raised as TraderConnectionException, never
            # silently swallowed, never started with a missing/weak key or
            # an illegal legacy-RPC mode combination) and, unlike
            # ConnectionRefusedError/TimeoutError, neither should be retried
            # by the @backoff decorator.
            raise trader_exception(self, TraderConnectionException, message='trading_runtime connect() exception', inner=ex)

    @log_method
    async def shutdown(self):
        if getattr(self, 'broker_ingest', None) is not None:
            self.broker_ingest.stop()
        self.client.ib.connectedEvent -= self.connected_event
        self.client.ib.disconnectedEvent -= self.disconnected_event
        self.client.ib.disconnect()

        for contract, sink in self.contract_subscriptions.items():
            sink.dispose()

        self.zmq_pubsub_contract_subscription.dispose()

        # for security_definition, security_datastream in self.market_data_subscriptions.items():
        #   security_datastream.dispose()

        for disposable in self.disposables:
            disposable.dispose()

        self.book.dispose()
        await self.client.shutdown()

    @log_method
    def reconnect(self):
        # this will force a reconnect through the disconnected event
        self.client.ib.disconnect()

    def __update_positions(self, positions: Union[List[Position], Position]):
        logging.debug('__update_positions')
        if type(positions) is Position:
            self.portfolio.add_position(positions)
        elif type(positions) is list:
            for position in positions:
                self.portfolio.add_position(position)

    def __update_portfolio(self, portfolio_item: PortfolioItem):
        logging.debug('__update_portfolio')
        self.portfolio.add_portfolio_item(portfolio_item=portfolio_item)
        # Schedule the async universe update onto the trader's main loop even
        # when this callback fires on an IB/eventkit thread. The old code fell
        # back to a *synchronous* disk-IO path in that case, which blocked
        # every other IB event on the callback thread.
        coro = self.update_portfolio_universe(portfolio_item)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
            return
        except RuntimeError:
            pass

        main_loop = self._main_loop
        if main_loop is not None and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, main_loop)
        else:
            # Truly no loop available (shutdown path, tests). Fall back to the
            # sync version but close the coroutine to avoid "never awaited".
            coro.close()
            self._update_portfolio_universe_sync(portfolio_item)

    def __dataclass_server_put(self, message: DataClassEvent):
        # logging.debug('__dataclass_server_put: {}'.format(message))
        self.zmq_pubsub_server.put(('dataclass', message))

    @log_method
    async def setup_subscriptions(self):
        if not self.is_ib_connected():
            raise ConnectionError('not connected to interactive brokers')

        def handle_subscription_exception(ex):
            exception = trader_exception(self, TraderException, message='setup_subscriptions()', inner=ex)
            raise exception

        def handle_completed():
            logging.debug('handle_completed()')

        # have the book subscribe to all relevant trade events
        await self.book.subscribe_to_eventkit_event(
            [
                self.client.ib.orderStatusEvent,
                self.client.ib.orderModifyEvent,
                self.client.ib.newOrderEvent,
                self.client.ib.cancelOrderEvent,
                self.client.ib.openOrderEvent,
            ]
        )

        # Feed the order-lifecycle tracker directly from the CURRENT ib's
        # orderStatusEvent. Use connect(keep_ref=True) — eventkit defaults to a
        # WEAK reference, which silently drops a freshly-bound method handler; a
        # strong ref guarantees delivery. disconnect-then-connect keeps exactly
        # one registration across reconnects (the ib instance is fresh each time).
        if getattr(self, 'order_tracker', None) is not None:
            _ev = self.client.ib.orderStatusEvent
            try:
                _ev.disconnect(self.order_tracker.on_trade)
            except Exception:
                pass
            _ev.connect(self.order_tracker.on_trade, keep_ref=True)
            logging.info('order-lifecycle tracker attached to orderStatusEvent')

        if getattr(self, 'broker_ingest', None) is not None:
            for event, handler in (
                (self.client.ib.accountValueEvent, self.broker_ingest.on_account_value),
                (self.client.ib.positionEvent, self.broker_ingest.on_position),
                (self.client.ib.updatePortfolioEvent, self.broker_ingest.on_portfolio_item),
                (self.client.ib.pnlSingleEvent, self.broker_ingest.on_pnl_single),
                (self.client.ib.openOrderEvent, self.broker_ingest.on_open_order),
                (self.client.ib.orderStatusEvent, self.broker_ingest.on_order_status),
                (self.client.ib.execDetailsEvent, self.broker_ingest.on_exec_details),
                (self.client.ib.commissionReportEvent, self.broker_ingest.on_commission_report),
            ):
                try:
                    event.disconnect(handler)
                except Exception:
                    pass
                event.connect(handler, keep_ref=True)
            logging.info('broker ingest producers attached to IB callbacks')

        positions_observer = Observer(
            on_next=self.__update_positions,
            on_error=handle_subscription_exception,
            on_completed=handle_completed
        )

        positions_disposable = (await self.client.subscribe_positions()).subscribe(positions_observer)
        self.disposables.append(positions_disposable)

        portfolio_disposable = (await self.client.subscribe_portfolio()).subscribe(AnonymousObserver(
            on_next=self.__update_portfolio,
            on_error=handle_subscription_exception,
        ))
        self.disposables.append(portfolio_disposable)

        # subscribe to all portfolio changes, then make sure we're subscribing to the pnl for each
        def __subscribe_pnl(portfolio_item: PortfolioItem):
            async def __async_subscribe_pnl(portfolio_item: PortfolioItem):
                if not portfolio_item.contract:
                    return
                key = (portfolio_item.account, portfolio_item.contract.conId)
                # Atomic "first claim wins" — prevents two concurrent portfolio
                # events from both crossing the earlier check-then-act gap and
                # leaking duplicate PnL subscriptions on reconnect.
                with self._pnl_subscriptions_lock:
                    if key in self.pnl_subscriptions:
                        return
                    self.pnl_subscriptions[key] = True

                try:
                    observable = await self.client.subscribe_single_pnl(
                        portfolio_item.contract,
                    )
                    disposable = observable.subscribe(
                        self.pnl.create_observer(error_func=handle_subscription_exception),
                    )
                    self.disposables.append(disposable)
                except Exception as ex:
                    # Back out the registry entry so a retry can re-attempt.
                    with self._pnl_subscriptions_lock:
                        self.pnl_subscriptions.pop(key, None)
                    logging.warning(f'Failed to subscribe PnL for {portfolio_item.contract}: {ex}')

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(__async_subscribe_pnl(portfolio_item))
                return
            except RuntimeError:
                pass

            # Off-loop-thread callback: hand off to the main loop if captured,
            # otherwise fall back to the legacy sync-run path.
            main_loop = self._main_loop
            if main_loop is not None and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(__async_subscribe_pnl(portfolio_item), main_loop)
            else:
                self.run(__async_subscribe_pnl(portfolio_item))

        disposable = (await self.client.subscribe_portfolio()).subscribe(
            AnonymousObserver(
                on_next=__subscribe_pnl,
                on_error=handle_subscription_exception,
            )
        )
        self.disposables.append(disposable)

        pnl_router_disposable = self.pnl.subscribe(on_next=self.__dataclass_server_put, on_error=handle_subscription_exception)
        self.disposables.append(pnl_router_disposable)

        # push book updates
        def __update_book(trade_order: Union[Trade, Order]):
            event = UpdateEvent(trade_order)
            self.__dataclass_server_put(event)

        book_update_disposable = self.book.subscribe(
            Observer(
                on_next=__update_book,
                on_error=handle_subscription_exception,
            )
        )
        self.disposables.append(book_update_disposable)

        # make sure we're getting either live, or delayed data
        self.client.ib.reqMarketDataType(self.market_data)

        orders = await self.client.ib.reqAllOpenOrdersAsync()
        for o in orders:
            self.book.on_next(o)

        # ensure that pnl is getting pumped out of zmq
        if self.scheduler is None:
            self.scheduler = AsyncIOThreadSafeScheduler(asyncio.get_running_loop())
        scheduled_disposable = self.scheduler.schedule_periodic(10, lambda x: self.pnl.post_all())
        self.disposables.append(scheduled_disposable)

    def _on_ib_error(self, error: IBAIORxError):
        """Track IB upstream connectivity from error codes.

        IB distinguishes two severities we care about:

        1. **1100 / 1102**: full gateway↔IBKR connectivity. 1100 means
           trading is actually disabled. This is the ONLY signal that
           should flip ``ib_upstream_connected`` to False.

        2. **2103 / 2105 / 2157**: per-data-farm status messages. IB
           Gateway has multiple farms (``usfarm``, ``euhmds``,
           ``cashfarm``, ``usfuture``, ...) and sends these warnings
           any time one farm briefly hiccups. Other farms stay up,
           trading keeps working, the Gateway UI stays green. Treating
           these as a hard disconnect produced false-positive "Gateway
           broken" warnings in the CLI while the user could see real-
           time P&L updating normally.

        We now track per-farm state as an informational dict so callers
        can surface "warning: usfarm hiccuped" without falsely reporting
        a full disconnect."""
        code = error.errorCode
        msg = error.errorString

        if code == 1100:
            # Real disconnect — trading disabled until 1102 arrives.
            self._ib_upstream_connected = False
            self._ib_upstream_error = msg
            logging.warning('IB upstream connection lost (code 1100): %s', msg)
        elif code == 1102:
            self._ib_upstream_connected = True
            self._ib_upstream_error = ''
            logging.info('IB upstream connection restored (code 1102): %s', msg)
        elif code in (2103, 2105, 2157):
            # Informational farm hiccup. Track so callers can query
            # ``_ib_farms_down`` if they really want farm-level detail,
            # but leave ``_ib_upstream_connected`` alone.
            if not hasattr(self, '_ib_farms_down'):
                self._ib_farms_down = {}
            self._ib_farms_down[code] = msg
            logging.info('IB farm warning (code %d, informational): %s', code, msg)
        elif code in (2104, 2106, 2158):
            if hasattr(self, '_ib_farms_down'):
                # 2104 ↔ 2103, 2106 ↔ 2105, 2158 ↔ 2157
                self._ib_farms_down.pop(code - 1, None)
            logging.info('IB farm restored (code %d): %s', code, msg)

    @log_method
    async def connected_event(self):
        # Coalesce the reconnect double-fire: connect_async() emits the IB
        # connectedEvent (→ this handler) AND the reconnect loop calls this
        # explicitly. Running both would dispose/rebuild subscriptions twice and
        # could double-subscribe the ticker feed. First one wins; skip the rest.
        if self._in_connected_event:
            logging.debug('connected_event already running — skipping duplicate invocation')
            return
        self._in_connected_event = True
        try:
            # Capture the main event loop now that we're running inside it. Used
            # to schedule async work from IB callback threads without spinning up
            # a throwaway loop.
            try:
                self._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            # Dispose old subscriptions before re-subscribing (happens on reconnect)
            for disposable in self.disposables:
                try:
                    disposable.dispose()
                except Exception:
                    pass
            self.disposables.clear()
            with self._pnl_subscriptions_lock:
                self.pnl_subscriptions.clear()

            if self._fake_broker_enabled():
                # connect_fake leaves a never-dialed IB() instance: live
                # reqPositionsAsync/reqAccountUpdates would hang forever, so
                # promote one empty broker generation from the stub client and
                # skip event subscriptions entirely.
                if getattr(self, 'broker_ingest', None) is not None:
                    await self.broker_ingest.run_broker_sync(
                        self._fake_broker_sync_client())
                return

            await self.setup_subscriptions()

            # Re-establish live ticker (pubsub) subscriptions. Their IB
            # market-data lines died with the previous session, so on a reconnect
            # we must resubscribe or the strategy tick feed goes silently dead.
            # No-op on the first connect (nothing published yet).
            self._republish_ticker_subscriptions()

            if getattr(self, 'broker_ingest', None) is not None:
                asyncio.get_running_loop().create_task(
                    self.broker_ingest.run_broker_sync(self.client)
                )

            # One-shot startup broker-truth reconciliation: after a restart,
            # cross-check proposals + positions against live IB and log any
            # divergence (report-only). Delayed so IB open-orders/positions have
            # populated; runs off the connected_event path so it can't block it.
            if not getattr(self, '_startup_reconciled', False):
                self._startup_reconciled = True

                async def _delayed_reconcile():
                    try:
                        await asyncio.sleep(8)
                        await self.reconcile_with_broker(trigger='startup')
                    except Exception as ex:
                        logging.warning('startup reconciliation failed: %s', ex)

                try:
                    asyncio.get_event_loop().create_task(_delayed_reconcile())
                except RuntimeError:
                    pass
        finally:
            self._in_connected_event = False

    def _republish_ticker_subscriptions(self):
        """Replay remembered publish_contract() calls after a reconnect."""
        remembered = dict(self.zmq_pubsub_published_contracts)
        if not remembered:
            return
        logging.info('re-establishing %d live ticker subscription(s) after reconnect',
                     len(remembered))
        # Tear down the stale shared subscription + per-contract state; the old
        # session's market-data lines are gone.
        try:
            self.zmq_pubsub_contract_subscription.dispose()
        except Exception:
            pass
        self.zmq_pubsub_contract_subscription = Disposable()
        self.zmq_pubsub_contracts = {}
        self.zmq_pubsub_contract_filters = {}
        for con_id, (contract, delayed) in remembered.items():
            try:
                self.publish_contract(contract, delayed=delayed)
            except Exception as ex:
                logging.error('failed to re-publish ticker subscription for conId %s: %s',
                              con_id, ex)

    @log_method
    async def disconnected_event(self):
        if getattr(self, 'broker_ingest', None) is not None:
            try:
                await asyncio.to_thread(
                    self.broker_ingest.abandon_generation, 'ib disconnected'
                )
            except Exception as ex:
                logging.warning('abandoning broker sync generation failed: %s', ex)

        # Guard against multiple concurrent reconnection attempts
        if hasattr(self, '_reconnecting') and self._reconnecting:
            logging.debug('reconnection already in progress, skipping')
            return

        self._reconnecting = True
        try:
            attempt = 0
            while True:
                attempt += 1
                delay = min(2 ** min(attempt, 7), 120)  # exponential backoff, cap at 2 minutes

                logging.warning(
                    'IB Gateway disconnected — reconnection attempt %d in %ds',
                    attempt, delay
                )

                t_before = asyncio.get_event_loop().time()
                await asyncio.sleep(delay)
                t_after = asyncio.get_event_loop().time()

                # Detect system sleep: if the actual elapsed time is much longer
                # than the requested delay, the system likely slept. Reset backoff
                # so we get a fresh set of fast retries after wake.
                elapsed = t_after - t_before
                if elapsed > delay * 3 and delay > 4:
                    logging.info(
                        'detected system sleep (requested %ds, elapsed %.0fs) — resetting backoff',
                        delay, elapsed,
                    )
                    attempt = 1

                try:
                    await self.client.connect_async()
                    # Re-attach event handlers to the fresh IB instance
                    if self.connected_event not in self.client.ib.connectedEvent:
                        self.client.ib.connectedEvent += self.connected_event
                    if self.disconnected_event not in self.client.ib.disconnectedEvent:
                        self.client.ib.disconnectedEvent += self.disconnected_event
                    logging.info('reconnected to IB Gateway on attempt %d', attempt)
                    await self.connected_event()
                    return
                except Exception as ex:
                    logging.error('reconnection attempt %d failed: %s', attempt, ex)
        finally:
            self._reconnecting = False

    # The strategy_service proxies below make a BLOCKING zmq RPC. It must run
    # in a thread (asyncio.to_thread), never on this event loop: several
    # strategy_service handlers (reload → reconcile → resolve_symbol,
    # update_strategy_params historically) call back INTO trader_service —
    # with our loop blocked awaiting their reply, those callbacks can't be
    # served and both sides burn their RPC timeout. Every `strategies reload`
    # silently timed out this way before the fix.

    @log_method
    async def enable_strategy(self, name: str) -> SuccessFail[StrategyState]:
        try:
            return await asyncio.to_thread(
                lambda: self.zmq_strategy_client.rpc().enable_strategy(name))
        except Exception as ex:
            logging.error('enable_strategy: {}'.format(ex))
            return SuccessFail.fail(exception=ex)

    @log_method
    async def update_strategy_params(self, name: str, params: dict) -> SuccessFail[dict]:
        try:
            return await asyncio.to_thread(
                lambda: self.zmq_strategy_client.rpc().update_strategy_params(name, params))
        except Exception as ex:
            logging.error('update_strategy_params: {}'.format(ex))
            return SuccessFail.fail(exception=ex)

    @log_method
    async def disable_strategy(self, name: str) -> SuccessFail[StrategyState]:
        try:
            return await asyncio.to_thread(
                lambda: self.zmq_strategy_client.rpc().disable_strategy(name))
        except Exception as ex:
            logging.error('disable_strategy: {}'.format(ex))
            return SuccessFail.fail(exception=ex)

    @log_method
    async def get_strategies(self) -> SuccessFail[List[StrategyConfig]]:
        try:
            rpc_call = await asyncio.to_thread(
                lambda: self.zmq_strategy_client.rpc().get_strategies())
            return SuccessFail.success(rpc_call)
        except Exception as ex:
            return SuccessFail.fail(exception=ex)

    @log_method
    async def reload_strategies(self) -> SuccessFail[List[StrategyConfig]]:
        try:
            return await asyncio.to_thread(
                lambda: self.zmq_strategy_client.rpc().reload_strategies())
        except Exception as ex:
            logging.error('reload_strategies: {}'.format(ex))
            return SuccessFail.fail(exception=ex)

    @log_method
    def clear_portfolio_universe(self):
        universe = self.universe_accessor.get('portfolio')
        universe.security_definitions.clear()
        self.universe_accessor.update(universe)

    @log_method
    async def resolve_contract(self, contract: Contract) -> List[SecurityDefinition]:
        """Resolve a partial Contract (e.g. with strike/expiry/right) to full SecurityDefinitions via IB."""
        contract_details = await self.client.ib.reqContractDetailsAsync(contract)
        if contract_details:
            return [SecurityDefinition.from_contract_details(cd) for cd in contract_details]
        return []

    @log_method
    async def resolve_symbol(
        self,
        symbol: Union[str, int],
        exchange: str = '',
        universe: str = '',
        sec_type: str = '',
    ) -> List[SecurityDefinition]:
        def __blocking_resolve_symbol_to_security_definitions(
            symbol: Union[str, int],
            exchange: str = '',
            universe: str = '',
            sec_type: str = '',
            first_only: bool = False,
        ) -> list[SecurityDefinition]:
            return self.universe_accessor.resolve_symbol(
                symbol=symbol,
                exchange=exchange,
                universe=universe,
                first_only=first_only
            )

        # if we're asking about conid's, we only want the first one
        first_only = False
        if type(symbol) is int:
            first_only = True

        # this could take a while
        result = await asyncio.to_thread(
            __blocking_resolve_symbol_to_security_definitions,
            symbol,
            exchange,
            universe,
            sec_type,
            first_only,
        )

        if len(result) > 0:
            return result

        # No IB fallback. resolve_symbol is a local DB lookup only.
        # Guessing with a partially-specified Contract can resolve to the
        # wrong instrument (e.g. SOXL → AEQLIT/CAD, 4391 → TSEJ).
        # Use resolve_contract() for explicit IB discovery with a
        # fully-specified Contract.
        if type(symbol) is int:
            logging.warning('conId %d not found in local universe DB', symbol)
        else:
            logging.warning("Symbol '%s' not found in local universe DB — "
                            "add it with `universe add` or use `resolve` CLI command", symbol)
        return []

    @log_method
    async def resolve_universe(
        self,
        symbol: Union[str, int],
        exchange: str = '',
        universe: str = '',
        sec_type: str = '',
    ) -> List[Tuple[str, SecurityDefinition]]:
        def __blocking_resolve_universe(
            symbol: Union[str, int],
            exchange: str = '',
            universe: str = '',
            sec_type: str = '',
        ) -> list[Tuple[str, SecurityDefinition]]:
            return self.universe_accessor.resolve_universe_name(
                symbol=symbol,
                exchange=exchange,
                universe=universe,
                sec_type=sec_type
            )

        # this could take a while
        return await asyncio.to_thread(__blocking_resolve_universe, symbol, exchange, universe, sec_type)

    @log_method
    def publish_contract(self, contract: Contract, delayed: bool) -> Observable[IBAIORxError]:
        # Remember the request so it can be replayed after a reconnect.
        self.zmq_pubsub_published_contracts[contract.conId] = (contract, delayed)
        if contract.conId in self.zmq_pubsub_contract_filters:
            return self.zmq_pubsub_contracts[contract.conId]

        def on_next(ticker: Ticker):
            self.zmq_pubsub_server.put(('ticker', ticker))

        def on_completed():
            del self.zmq_pubsub_contracts[contract.conId]
            del self.zmq_pubsub_contract_filters[contract.conId]
            logging.debug('publish_contract.aclose() for {}'.format(contract))

        def on_error(ex):
            del self.zmq_pubsub_contracts[contract.conId]
            del self.zmq_pubsub_contract_filters[contract.conId]
            raise trader_exception(self, TraderException, message='publish_contract() on_error', inner=ex)

        if len(self.zmq_pubsub_contract_filters) == 0:
            # setup the observable for the first time
            try:
                auto_detach = AutoDetachObserver(on_next=on_next, on_completed=on_completed, on_error=on_error)
                subscription = self.client.contracts_subject.subscribe(auto_detach)  # , scheduler=NewThreadScheduler())
                self.zmq_pubsub_contract_subscription = subscription
            except Exception as ex:
                # todo not sure how to deal with this error condition yet
                raise trader_exception(self, TraderException, message='publish_contract()', inner=ex)

        error_observable = self.client.subscribe_contract_direct(contract, delayed=delayed)
        self.zmq_pubsub_contract_filters[contract.conId] = True
        self.zmq_pubsub_contracts[contract.conId] = error_observable
        return error_observable

    async def update_portfolio_universe(self, portfolio_item: PortfolioItem):
        """Add new positions to the 'portfolio' universe so history downloads
        + strategy subscriptions auto-cover them.

        Called once per ``updatePortfolioEvent`` — which on a 49-position
        account produces a 49× burst at connect. We coalesce this via
        ``_known_portfolio_conids`` so repeated events for already-known
        conIds skip all DB work immediately. All ``universe_accessor``
        reads/writes run in a worker thread via ``asyncio.to_thread`` so
        the main event loop stays free for ticker dispatch / ZMQ traffic.
        """
        conid = portfolio_item.contract.conId

        # Fast path: conId already known from this session. Main-loop-
        # blocking reasons this is worth inlining:
        #   - The old path did 49 sync DuckDB reads + dill.loads + writes
        #     on the loop when 48 of them turned out to be no-ops.
        #   - `updatePortfolioEvent` fires on every position *update* too
        #     (price-mark refreshes), so this cache prevents a long-running
        #     session from paying the DB cost on every mark.
        if conid in self._known_portfolio_conids:
            return

        # First time we've seen this conId — reconcile with the persisted
        # universe. Thread the read so dill.loads on a multi-position
        # universe doesn't stall the loop.
        universe = await asyncio.to_thread(
            self.universe_accessor.get, 'portfolio',
        )

        # Seed / re-seed the in-memory set from the freshly read universe.
        # Any concurrent update_portfolio_universe calls that raced past
        # the fast-path check will all see the same set after this point.
        self._known_portfolio_conids = {
            d.conId for d in universe.security_definitions
        }
        if conid in self._known_portfolio_conids:
            # Another task added it while we were reading, or it's been
            # persisted across sessions. Either way, nothing to do.
            return

        # Genuinely new — go fetch contract details and persist.
        contract = portfolio_item.contract
        try:
            contract_details = await self.client.get_contract_details_async(contract)
        except Exception as ex:
            logging.warning(f'Failed to get contract details for {contract}: {ex}')
            return
        if not contract_details:
            return
        universe.security_definitions.append(
            SecurityDefinition.from_contract_details(contract_details[0])
        )
        self._known_portfolio_conids.add(conid)
        logging.debug('updating portfolio universe with %s', portfolio_item)

        # Thread the write — dill.dumps on the universe + DuckDB INSERT
        # can be tens of ms; no reason to run on the loop.
        await asyncio.to_thread(self.universe_accessor.update, universe)

    def _update_portfolio_universe_sync(self, portfolio_item: PortfolioItem):
        """Sync fallback when no event loop is running."""
        universe = self.universe_accessor.get('portfolio')
        if not ListHelper.isin(
            universe.security_definitions,
            lambda definition: definition.conId == portfolio_item.contract.conId
        ):
            contract = portfolio_item.contract
            contract_details = self.client.get_contract_details(contract)
            if contract_details and len(contract_details) >= 1:
                universe.security_definitions.append(
                    SecurityDefinition.from_contract_details(contract_details[0])
                )

            logging.debug('updating portfolio universe with {}'.format(portfolio_item))
            self.universe_accessor.update(universe)

    @log_method
    async def place_order(
        self,
        contract: Contract,
        order: Order,
        condition: ExecutorCondition,
    ) -> Observable[Trade]:
        return await self.executioner.place_order(contract_order=ContractOrderPair(contract, order), condition=condition)

    @log_method
    async def check_order_margin(self, contract: Contract, order: Order) -> dict:
        """Run whatIfOrder to get margin impact without placing."""
        order_state = await self.client.ib.whatIfOrderAsync(contract, order)
        numeric = order_state.numeric(2)
        return {
            'initMarginBefore': numeric.initMarginBefore,
            'maintMarginBefore': numeric.maintMarginBefore,
            'equityWithLoanBefore': numeric.equityWithLoanBefore,
            'initMarginChange': numeric.initMarginChange,
            'maintMarginChange': numeric.maintMarginChange,
            'equityWithLoanChange': numeric.equityWithLoanChange,
            'initMarginAfter': numeric.initMarginAfter,
            'maintMarginAfter': numeric.maintMarginAfter,
            'equityWithLoanAfter': numeric.equityWithLoanAfter,
            'commission': numeric.commission,
            'warningText': order_state.warningText,
        }

    @log_method
    async def place_expressive_order(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        execution_spec: dict,
        algo_name: str = 'proposal',
    ) -> SuccessFail:
        """Place an order with full execution specification (brackets, trailing stops, etc.)."""
        from trader.trading.proposal import ExecutionSpec
        spec = ExecutionSpec.from_dict(execution_spec)

        # Validate execution spec before placing any orders
        validation_errors = spec.validate()
        if validation_errors:
            return SuccessFail.fail(error=f'Invalid execution spec: {"; ".join(validation_errors)}')

        trades: List[Trade] = []

        reverse_action = 'SELL' if action == 'BUY' else 'BUY'

        common = dict(
            action=action,
            totalQuantity=quantity,
            account=self.ib_account,
            orderRef=algo_name,
            tif=spec.tif,
            outsideRth=spec.outside_rth,
        )
        if spec.tif == 'GTD' and spec.good_till_date:
            common['goodTillDate'] = spec.good_till_date

        def _build_entry(**common) -> Order:
            if spec.order_type == 'MARKET':
                return MarketOrder(**common)
            else:
                return LimitOrder(lmtPrice=spec.limit_price, **common)

        # --- Pre-trade risk checks ---

        # 0. Trading filter check (denylist/allowlist)
        if getattr(self, 'risk_gate', None) is not None:
            instrument_result = self.risk_gate.check_instrument(
                symbol=contract.symbol, exchange=contract.exchange or '', sec_type=contract.secType or '',
            )
            if not instrument_result.approved:
                return SuccessFail.fail(error=instrument_result.reason)

        # Build a temporary entry order for margin simulation
        probe_order = _build_entry(**common)

        # 1. whatIfOrder margin check
        margin_impact = None
        try:
            margin_impact = await self.check_order_margin(contract, probe_order)
        except Exception as ex:
            logging.warning(f'whatIfOrder failed, proceeding without margin check: {ex}')

        # 2. Leverage limit check
        if margin_impact and getattr(self, 'risk_gate', None) is not None:
            # Scope NetLiquidation to the configured account. With a
            # multi-account login, accountValues() returns rows for every
            # managed account; picking the first NetLiquidation row blind
            # could size the leverage check against the wrong (e.g. master
            # aggregate) account. Pin to self.ib_account.
            active_account = self.ib_account
            if not active_account:
                managed = self.client.ib.managedAccounts() or []
                active_account = managed[0] if managed else None
            net_liq = 0.0
            for v in self.client.ib.accountValues():
                if v.tag != 'NetLiquidation' or v.currency == 'BASE':
                    continue
                if active_account and v.account and v.account != active_account:
                    continue
                net_liq = float(v.value)
                break

            leverage_result = self.risk_gate.check_leverage(margin_impact, net_liq)
            if not leverage_result.approved:
                return SuccessFail.fail(error=leverage_result.reason)

        # 3. Risk gate checks (open orders, daily loss, concentration)
        if getattr(self, 'risk_gate', None) is not None:
            from trader.trading.strategy import Signal
            signal = Signal(
                source_name='proposal',
                action=Action.BUY if action == 'BUY' else Action.SELL,
                probability=1.0,
                risk=0.0,
            )

            # Count only *working* orders, not every order ever booked — else the
            # limit trips permanently mid-session and blocks all trading.
            open_order_count = (
                self.book.get_open_order_count() if hasattr(self, 'book') else 0
            )

            # Feed the daily-loss and concentration limits the state they need —
            # previously only open_order_count was passed, so those two limits were
            # dead code. Best-effort; on any read failure they degrade to 0 (skip)
            # rather than blocking the order.
            daily_pnl = 0.0
            try:
                for p in (self.get_pnl() or []):
                    daily_pnl += float(getattr(p, 'dailyPnL', 0.0) or 0.0)
            except Exception as ex:
                logging.warning('risk gate: could not read daily PnL: %s', ex)

            portfolio_value = 0.0
            try:
                active_account = self.ib_account or (
                    (self.client.ib.managedAccounts() or [None])[0])
                for v in self.client.ib.accountValues():
                    if v.tag != 'NetLiquidation' or v.currency == 'BASE':
                        continue
                    if active_account and v.account and v.account != active_account:
                        continue
                    portfolio_value = float(v.value)
                    break
            except Exception as ex:
                logging.warning('risk gate: could not read NetLiquidation: %s', ex)

            # Position notional is only reliable without a price fetch for LIMIT
            # orders; for market orders leave it 0 (concentration check skipped)
            # rather than guessing and false-blocking.
            position_value = 0.0
            if spec.order_type != 'MARKET' and spec.limit_price:
                try:
                    position_value = abs(float(quantity) * float(spec.limit_price))
                except (TypeError, ValueError):
                    position_value = 0.0

            gate_result = self.risk_gate.evaluate(
                signal=signal,
                open_order_count=open_order_count,
                daily_pnl=daily_pnl,
                portfolio_value=portfolio_value,
                position_value=position_value,
            )
            if not gate_result.approved:
                return SuccessFail.fail(error=f'Risk gate: {gate_result.reason}')

        async def _place_and_wait(c: Contract, o: Order) -> Optional[Trade]:
            """Place a single child order and await the IB ack. Returns the
            Trade object or None on failure (observer emitted on_error)."""
            event = asyncio.Event()
            result: Dict[str, Optional[Trade]] = {'trade': None}

            def _on_next(trade: Trade):
                result['trade'] = trade
                event.set()

            obs = await self.executioner.subscribe_place_order_direct(c, o)
            obs.subscribe(Observer(
                on_next=_on_next,
                on_error=lambda e: event.set(),
                on_completed=lambda: None,
            ))
            await event.wait()
            return result['trade']

        def _cancel_trade_safely(trade: Optional[Trade]) -> None:
            """Best-effort cancel of a staged (transmit=False) child order."""
            if trade is None or not getattr(trade, 'order', None):
                return
            try:
                self.client.ib.cancelOrder(trade.order)
            except Exception as ex:
                logging.warning(
                    'failed to cancel partial bracket leg %s: %s',
                    getattr(trade.order, 'orderId', '?'), ex,
                )

        try:
            if spec.exit_type == 'BRACKET':
                entry = _build_entry(**common)
                entry.transmit = False

                entry_trade = await _place_and_wait(contract, entry)
                if entry_trade is None:
                    return SuccessFail.fail(error='Failed to place entry order')

                trades.append(entry_trade)
                parent_id = entry_trade.order.orderId

                # Take-profit
                tp = LimitOrder(
                    action=reverse_action,
                    totalQuantity=quantity,
                    lmtPrice=spec.take_profit_price,
                    parentId=parent_id,
                    transmit=False,
                    account=self.ib_account,
                    orderRef=algo_name,
                    tif=spec.tif,
                    outsideRth=spec.outside_rth,
                )
                if spec.oca_group:
                    tp.ocaGroup = spec.oca_group
                    tp.ocaType = 1  # cancel remaining on fill
                tp_trade = await _place_and_wait(contract, tp)
                if tp_trade is None:
                    # Roll back the staged entry — it was transmit=False so no
                    # market-side exposure yet; cancelling keeps the book
                    # consistent with the caller's understanding that the
                    # bracket failed atomically.
                    _cancel_trade_safely(entry_trade)
                    return SuccessFail.fail(
                        error='Bracket aborted: take-profit order rejected; entry rolled back'
                    )
                trades.append(tp_trade)

                # Stop-loss (transmit=True triggers the whole bracket)
                sl = StopOrder(
                    action=reverse_action,
                    totalQuantity=quantity,
                    stopPrice=spec.stop_loss_price,
                    parentId=parent_id,
                    transmit=True,
                    account=self.ib_account,
                    orderRef=algo_name,
                    tif=spec.tif,
                    outsideRth=spec.outside_rth,
                )
                if spec.oca_group:
                    sl.ocaGroup = spec.oca_group
                    sl.ocaType = 1
                sl_trade = await _place_and_wait(contract, sl)
                if sl_trade is None:
                    # Same as above: cancel TP + entry before the bracket is
                    # ever transmitted to the market.
                    _cancel_trade_safely(tp_trade)
                    _cancel_trade_safely(entry_trade)
                    return SuccessFail.fail(
                        error='Bracket aborted: stop-loss order rejected; entry + TP rolled back'
                    )
                trades.append(sl_trade)

            elif spec.exit_type == 'TRAILING_STOP':
                entry = _build_entry(**common)
                entry.transmit = False

                task = asyncio.Event()
                entry_trade = None

                def on_entry_ts(trade: Trade):
                    nonlocal entry_trade
                    entry_trade = trade
                    task.set()

                observable = await self.executioner.subscribe_place_order_direct(contract, entry)
                observable.subscribe(Observer(on_next=on_entry_ts, on_error=lambda e: task.set(), on_completed=lambda: None))
                await task.wait()

                if entry_trade is None:
                    return SuccessFail.fail(error='Failed to place entry order')

                trades.append(entry_trade)
                parent_id = entry_trade.order.orderId

                trail = Order(
                    orderType='TRAIL',
                    action=reverse_action,
                    totalQuantity=quantity,
                    parentId=parent_id,
                    transmit=True,
                    account=self.ib_account,
                    orderRef=algo_name,
                    tif=spec.tif,
                    outsideRth=spec.outside_rth,
                )
                if spec.trailing_stop_percent:
                    trail.trailingPercent = spec.trailing_stop_percent
                elif spec.trailing_stop_amount:
                    trail.auxPrice = spec.trailing_stop_amount

                trail_task = asyncio.Event()
                trail_trade: Optional[Trade] = None

                def on_trail(trade: Trade):
                    nonlocal trail_trade
                    trail_trade = trade
                    trail_task.set()

                trail_obs = await self.executioner.subscribe_place_order_direct(contract, trail)
                trail_obs.subscribe(Observer(on_next=on_trail, on_error=lambda e: trail_task.set(), on_completed=lambda: None))
                await trail_task.wait()
                if trail_trade is None:
                    # All-or-nothing: the trailing stop is what transmits the
                    # staged (transmit=False) entry. If it failed, roll back the
                    # entry so we don't leave a zombie staged order and, crucially,
                    # don't report success for an unprotected/undelivered order.
                    _cancel_trade_safely(entry_trade)
                    return SuccessFail.fail(
                        error='Trailing-stop aborted: protective leg rejected; entry rolled back'
                    )
                trades.append(trail_trade)

            elif spec.exit_type == 'STOP_LOSS':
                entry = _build_entry(**common)
                entry.transmit = False

                task = asyncio.Event()
                entry_trade = None

                def on_entry_sl(trade: Trade):
                    nonlocal entry_trade
                    entry_trade = trade
                    task.set()

                observable = await self.executioner.subscribe_place_order_direct(contract, entry)
                observable.subscribe(Observer(on_next=on_entry_sl, on_error=lambda e: task.set(), on_completed=lambda: None))
                await task.wait()

                if entry_trade is None:
                    return SuccessFail.fail(error='Failed to place entry order')

                trades.append(entry_trade)
                parent_id = entry_trade.order.orderId

                sl = StopOrder(
                    action=reverse_action,
                    totalQuantity=quantity,
                    stopPrice=spec.stop_loss_price,
                    parentId=parent_id,
                    transmit=True,
                    account=self.ib_account,
                    orderRef=algo_name,
                    tif=spec.tif,
                    outsideRth=spec.outside_rth,
                )

                sl_task = asyncio.Event()
                sl_trade: Optional[Trade] = None

                def on_sl_only(trade: Trade):
                    nonlocal sl_trade
                    sl_trade = trade
                    sl_task.set()

                sl_obs = await self.executioner.subscribe_place_order_direct(contract, sl)
                sl_obs.subscribe(Observer(on_next=on_sl_only, on_error=lambda e: sl_task.set(), on_completed=lambda: None))
                await sl_task.wait()
                if sl_trade is None:
                    # All-or-nothing: the stop-loss transmits the staged
                    # (transmit=False) entry. If it failed, roll back the entry
                    # rather than returning success for an unprotected order.
                    _cancel_trade_safely(entry_trade)
                    return SuccessFail.fail(
                        error='Stop-loss aborted: protective leg rejected; entry rolled back'
                    )
                trades.append(sl_trade)

            else:
                # NONE — simple entry only
                entry = _build_entry(**common)
                entry.transmit = True

                task = asyncio.Event()
                entry_trade = None

                def on_entry_simple(trade: Trade):
                    nonlocal entry_trade
                    entry_trade = trade
                    task.set()

                observable = await self.executioner.subscribe_place_order_direct(contract, entry)
                observable.subscribe(Observer(on_next=on_entry_simple, on_error=lambda e: task.set(), on_completed=lambda: None))
                await task.wait()
                if entry_trade is None:
                    return SuccessFail.fail(error='Failed to place entry order')
                trades.append(entry_trade)

            # Confirm IB actually ACCEPTED the order — the placeOrder echo above
            # returns a Trade even for an order IB then rejects. Only an explicit
            # rejection downgrades to failure; a slow (timeout) status leaves the
            # result as success, because the order is placed and may be working
            # and reporting failure there would be the more dangerous lie.
            tracker = getattr(self, 'order_tracker', None)
            if tracker is not None and trades:
                entry_id = int(getattr(trades[0].order, 'orderId', 0) or 0)
                if entry_id:
                    verdict = await tracker.wait_decisive(entry_id, timeout=8.0)
                    if verdict == 'rejected':
                        for t in trades:
                            _cancel_trade_safely(t)
                        reason = tracker.latest_status(entry_id) or 'rejected'
                        return SuccessFail.fail(
                            error=f'Order rejected by IB (entry status={reason})')

            return SuccessFail.success(obj=trades)

        except Exception as ex:
            logging.error(f'place_expressive_order error: {ex}')
            return SuccessFail.fail(exception=ex)

    async def place_standalone_order(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        order_type: str,
        aux_price: float = 0,
        limit_price: float = 0,
        trailing_percent: float = 0,
        tif: str = 'GTC',
        outside_rth: bool = True,
    ) -> SuccessFail:
        """Place a standalone order (e.g. protective stop for an existing position).

        order_type: 'STP' (stop), 'TRAIL' (trailing stop), 'LMT' (take-profit limit)
        """
        try:
            if order_type == 'STP':
                order = StopOrder(
                    action=action,
                    totalQuantity=quantity,
                    stopPrice=aux_price,
                    account=self.ib_account,
                    tif=tif,
                    outsideRth=outside_rth,
                    transmit=True,
                )
            elif order_type == 'TRAIL':
                order = Order(
                    orderType='TRAIL',
                    action=action,
                    totalQuantity=quantity,
                    account=self.ib_account,
                    tif=tif,
                    outsideRth=outside_rth,
                    transmit=True,
                )
                if trailing_percent:
                    order.trailingPercent = trailing_percent
                elif aux_price:
                    order.auxPrice = aux_price
            elif order_type == 'LMT':
                order = LimitOrder(
                    action=action,
                    totalQuantity=quantity,
                    lmtPrice=limit_price,
                    account=self.ib_account,
                    tif=tif,
                    outsideRth=outside_rth,
                    transmit=True,
                )
            else:
                return SuccessFail.fail(error=f'Unsupported order_type: {order_type}')

            task = asyncio.Event()
            result_trade: Optional[Trade] = None

            def on_next(trade: Trade):
                nonlocal result_trade
                result_trade = trade
                task.set()

            observable = await self.executioner.subscribe_place_order_direct(contract, order)
            observable.subscribe(Observer(on_next=on_next, on_error=lambda e: task.set(), on_completed=lambda: None))
            await task.wait()

            if result_trade:
                return SuccessFail.success(obj=result_trade)
            else:
                return SuccessFail.fail(error='Failed to place standalone order')

        except Exception as ex:
            logging.error(f'place_standalone_order error: {ex}')
            return SuccessFail.fail(exception=ex)

    @log_method
    async def place_order_simple(
        self,
        contract: Contract,
        action: Action,
        equity_amount: Optional[float],
        quantity: Optional[float],
        limit_price: Optional[float],
        market_order: bool,
        stop_loss_percentage: float,
        algo_name: str = 'global',
        debug: bool = False,
        skip_risk_gate: bool = False,
    ) -> Observable[Trade]:
        latest_tick: Ticker = await self.client.get_snapshot(contract)

        contract_order = self.executioner.helper_create_order(
            contract,
            action,
            latest_tick,
            equity_amount,
            quantity,
            limit_price,
            market_order,
            stop_loss_percentage,
            algo_name=algo_name,
            debug=debug
        )
        return await self.executioner.place_order(
            contract_order=contract_order,
            condition=ExecutorCondition.SANITY_CHECK,
            skip_risk_gate=skip_risk_gate,
        )

    @log_method
    def cancel_order(self, order_id: int) -> Optional[Trade]:
        return self.executioner.cancel_order_id(order_id)

    @log_method
    def cancel_all(self) -> SuccessFail[List[int]]:
        cancelled = []
        failed_cancels = []
        for order_id, _ in self.book.get_orders().items():
            trade: Optional[Trade] = self.cancel_order(order_id)
            if trade:
                cancelled.append(order_id)
            else:
                failed_cancels.append(order_id)

        if failed_cancels:
            return SuccessFail.fail(error=f'Failed to cancel: {failed_cancels}')
        else:
            return SuccessFail.success(obj=cancelled)

    async def scanner_data(self, **kwargs) -> list[dict]:
        return await self.client.scanner_data(**kwargs)

    async def scanner_locations(self) -> list[dict]:
        """List every scanner location this account is authorised for.
        Diagnostic for error 162 (scanner not configured) — if your
        chosen location isn't in this list, either the string is wrong
        or your account/paper mode lacks the subscription."""
        return await self.client.scanner_locations()

    async def get_snapshots_batch(self, contracts, delayed: bool = False) -> list[dict]:
        return await self.client.get_snapshots_batch(contracts, delayed)

    async def get_history_bars(self, contract, duration: str = '60 D', bar_size: str = '1 day') -> list[dict]:
        return await self.client.get_history_bars(contract, duration, bar_size)

    async def get_fundamental_data(self, contract, report_type: str = 'ReportSnapshot') -> str:
        return await self.client.get_fundamental_data(contract, report_type)

    async def get_market_depth(self, contract, num_rows: int = 5, is_smart_depth: bool = False) -> dict:
        return await self.client.get_market_depth(contract, num_rows=num_rows, is_smart_depth=is_smart_depth)

    async def get_news_headlines(self, conId: int, provider_codes: str = '',
                                  total_results: int = 5) -> list[dict]:
        return await self.client.get_news_headlines(conId, provider_codes, total_results)

    def is_ib_connected(self) -> bool:
        return self.client.ib.isConnected()

    @log_method
    def red_button(self):
        self.client.ib.reqGlobalCancel()

    # status() is polled heavily by strategy_service, the CLI, and the
    # risk-gate. A 1-second TTL cache is invisible to every caller (these
    # are idempotent-diagnostic reads, not trade decisions) and avoids
    # re-walking IB state on every RPC. No @log_method — the decorator's
    # inspect.signature + repr for every call adds measurable overhead on a
    # hot path, and the RPC server already DEBUG-logs each dispatch.
    def status(self) -> dict:
        now = time.monotonic()
        cached_ts = getattr(self, '_status_cache_ts', 0.0)
        if now - cached_ts < 1.0:
            cached = getattr(self, '_status_cache', None)
            if cached is not None:
                return cached
        status = {
            'ib_connected': self.client.ib.isConnected(),
            'ib_upstream_connected': self._ib_upstream_connected,
            'storage_connected': self.data is not None,
        }
        if not self._ib_upstream_connected:
            status['ib_upstream_error'] = self._ib_upstream_error
        self._status_cache = status
        self._status_cache_ts = now
        return status

    def get_unique_client_id(self) -> int:
        new_client_id = max(self.tws_client_ids) + 1
        self.tws_client_ids.append(new_client_id)
        self.tws_client_ids.append(new_client_id + 1)
        return new_client_id

    def get_pnl(self) -> List[PnLSingle]:
        return self.pnl.get_all()

    # Async + thread-offloaded. This is the method strategy_service calls
    # every reconcile (30s) plus what the CLI's `portfolio` command hits,
    # so it's on a hot path. Iterating the portfolio dict is cheap, but
    # building PortfolioSummary dataclasses and doing the PnL-cache lookup
    # per item was one of the callsites starving the trader_service event
    # loop (RPC handler slow-callback warnings at ~1s). Running the body
    # in a worker thread keeps the loop responsive for ticker dispatch
    # and other RPC requests.
    async def get_portfolio_summary(self) -> List[PortfolioSummary]:
        return await asyncio.to_thread(self._get_portfolio_summary_sync)

    def _get_portfolio_summary_sync(self) -> List[PortfolioSummary]:
        def find_pnl_or_nan(account: str, contract: Contract) -> float:
            if str((account, contract.conId)) in self.pnl.cache:
                return self.pnl.cache[str((account, contract.conId))].dailyPnL
            else:
                return float('nan')

        # Source of truth: always ask ib_async's ib.portfolio() directly.
        # The old path read from self.portfolio (our Portfolio cache), which
        # is populated by the updatePortfolioEvent observer — if that
        # observer chain ever breaks (e.g. event handlers dropped across an
        # IB() replacement), the cache stays empty even though ib.portfolio()
        # returns the live data. Reading directly is O(N) in position count
        # and already fast; no reason to route through the cache.
        portfolio_items = []
        try:
            portfolio_items = self.client.ib.portfolio(
                account=self.ib_account
            ) if self.ib_account else self.client.ib.portfolio()
        except Exception as ex:
            logging.warning(
                'ib.portfolio() failed, falling back to local cache: %s', ex,
            )
            portfolio_items = self.portfolio.get_portfolio_items()

        # If ib_async returned empty (e.g. subscription not ready) but our
        # local cache has items from a prior event, prefer the cache —
        # belt-and-braces for the reverse failure.
        if not portfolio_items and self.portfolio.portfolio_items:
            portfolio_items = self.portfolio.get_portfolio_items()

        summary: List[PortfolioSummary] = []
        for portfolio_item in portfolio_items:
            summary.append(PortfolioSummary(
                contract=portfolio_item.contract,
                position=portfolio_item.position,
                marketValue=portfolio_item.marketValue,
                averageCost=portfolio_item.averageCost,
                unrealizedPNL=portfolio_item.unrealizedPNL,
                realizedPNL=portfolio_item.realizedPNL,
                account=portfolio_item.account,
                marketPrice=portfolio_item.marketPrice,
                dailyPNL=find_pnl_or_nan(portfolio_item.account, portfolio_item.contract)
            ))
        return summary

    def get_positions(self) -> List[Position]:
        # See _get_portfolio_summary_sync for rationale — hit ib_async
        # directly rather than relying on the event-driven local cache.
        try:
            positions = self.client.ib.positions(
                account=self.ib_account
            ) if self.ib_account else self.client.ib.positions()
            if positions:
                return list(positions)
        except Exception as ex:
            logging.warning('ib.positions() failed, using cache: %s', ex)
        return self.portfolio.get_positions()

    async def reconcile_with_broker(self, trigger: str = 'operator') -> dict:
        """Cross-check recent proposals + positions against live IB truth.

        REPORT-ONLY: fetches IB open orders, executions and positions, compares
        them to the proposal store and current positions, and returns a
        divergence report. Places/cancels nothing and mutates no proposal status.
        """
        import uuid

        from trader.trading.reconciliation import reconcile
        from trader.data.proposal_store import ProposalStore

        started_at = dt.datetime.now(dt.timezone.utc)

        def _action(o):
            return str(getattr(o, 'action', '') or '')

        # Open orders — reqAllOpenOrders returns Trade objects (order+contract+status).
        open_orders = []
        try:
            for t in (await self.client.get_open_orders()) or []:
                order = getattr(t, 'order', t)
                contract = getattr(t, 'contract', None)
                st = getattr(t, 'orderStatus', None)
                open_orders.append({
                    'order_id': int(getattr(order, 'orderId', 0) or 0),
                    'conId': int(getattr(contract, 'conId', 0) or 0) if contract else 0,
                    'symbol': getattr(contract, 'symbol', '') if contract else '',
                    'action': _action(order),
                    'orderType': str(getattr(order, 'orderType', '') or ''),
                    'status': str(getattr(st, 'status', '') or ''),
                })
        except Exception as ex:
            logging.warning('reconcile: get_open_orders failed: %s', ex)

        executions = []
        try:
            for fill in (await self.client.get_executions()) or []:
                ex_obj = getattr(fill, 'execution', None)
                contract = getattr(fill, 'contract', None)
                executions.append({
                    'order_id': int(getattr(ex_obj, 'orderId', 0) or 0) if ex_obj else 0,
                    'conId': int(getattr(contract, 'conId', 0) or 0) if contract else 0,
                    'symbol': getattr(contract, 'symbol', '') if contract else '',
                    'side': str(getattr(ex_obj, 'side', '') or '') if ex_obj else '',
                    'shares': float(getattr(ex_obj, 'shares', 0.0) or 0.0) if ex_obj else 0.0,
                    'price': float(getattr(ex_obj, 'price', 0.0) or 0.0) if ex_obj else 0.0,
                })
        except Exception as ex:
            logging.warning('reconcile: get_executions failed: %s', ex)

        positions = []
        try:
            for p in self.get_positions() or []:
                contract = getattr(p, 'contract', None)
                positions.append({
                    'conId': int(getattr(contract, 'conId', 0) or 0) if contract else 0,
                    'symbol': getattr(contract, 'symbol', '') if contract else '',
                    'position': float(getattr(p, 'position', 0.0) or 0.0),
                })
        except Exception as ex:
            logging.warning('reconcile: get_positions failed: %s', ex)

        proposals = []
        try:
            store = ProposalStore(self.duckdb_path)
            proposals = (store.query(status='EXECUTED', limit=100)
                         + store.query(status='APPROVED', limit=100))
        except Exception as ex:
            logging.warning('reconcile: proposal query failed: %s', ex)

        report = reconcile(proposals, open_orders, executions, positions)
        for f in report.findings:
            level = logging.error if f.severity == 'critical' else logging.warning
            level('reconcile [%s] %s (proposal=%s): %s',
                  f.severity, f.symbol, f.proposal_id, f.detail)
        if not report.findings:
            logging.info('reconcile: no divergence (%d proposals, %d positions, '
                         '%d open orders, %d executions checked)',
                         report.checked_proposals, report.checked_positions,
                         report.ib_open_orders, report.ib_executions)
        result = report.to_dict()
        # Reconciliation remains report-only: failure to record its audit
        # event must never alter the existing report or broker interaction.
        if getattr(self, 'domain_journal', None) is not None:
            try:
                from trader.trading.risk_producer import ReconciliationProducer

                journal_db = getattr(self, 'journal_db', None)
                if journal_db is None:
                    journal_db = self.domain_journal.db
                ReconciliationProducer(db=journal_db, journal=self.domain_journal).publish_run(
                    run_id=f"recon-{uuid.uuid4().hex}",
                    trigger=trigger,
                    source_cursor=None,
                    discrepancies=result.get('discrepancies', []),
                    resolutions=result.get('resolutions', []),
                    started_at=started_at,
                    completed_at=dt.datetime.now(dt.timezone.utc),
                )
            except Exception as ex:
                logging.warning('journaling reconciliation run failed: %s', ex)
        return result

    def diagnose_portfolio_feed(self) -> dict:
        """Dump raw IB portfolio/positions from every managed account.

        Bypasses MMR's ``Portfolio`` cache (which is populated by event
        callbacks) and hits ``ib.portfolio(account)`` / ``ib.positions(account)``
        directly. Used to diagnose the "status shows $1M in margin,
        positions=0" class of bug — usually means MMR is filtering by
        the wrong account string (FA paper accounts have sub-accounts),
        or the init subscriptions timed out and the event cache never
        populated."""
        result = {
            'configured_ib_account': self.ib_account,
            'managed_accounts': [],
            'accounts_from_client': [],
            'cache_portfolio_count': len(self.portfolio.portfolio_items),
            'cache_position_count': len(self.portfolio.positions),
            'per_account': {},
        }
        try:
            result['managed_accounts'] = list(self.client.ib.managedAccounts() or [])
        except Exception as ex:
            result['managed_accounts_error'] = str(ex)
        try:
            result['accounts_from_client'] = list(self.client.ib.client.getAccounts() or [])
        except Exception as ex:
            result['accounts_from_client_error'] = str(ex)

        # Try every account we know about, plus the empty-string "default"
        # query and the configured ib_account. Dedup.
        targets = set(result['managed_accounts'])
        targets.update(result['accounts_from_client'])
        if self.ib_account:
            targets.add(self.ib_account)
        targets.add('')  # empty = IB's default (= single managed account)

        for acct in sorted(targets):
            info: dict = {}
            try:
                items = self.client.ib.portfolio(account=acct) if acct else self.client.ib.portfolio()
                info['portfolio_count'] = len(items)
                info['portfolio_sample'] = [
                    {
                        'symbol': it.contract.symbol,
                        'secType': it.contract.secType,
                        'position': it.position,
                        'marketValue': it.marketValue,
                        'account': it.account,
                    }
                    for it in items[:5]
                ]
            except Exception as ex:
                info['portfolio_error'] = str(ex)
            try:
                positions = self.client.ib.positions(account=acct) if acct else self.client.ib.positions()
                info['positions_count'] = len(positions)
            except Exception as ex:
                info['positions_error'] = str(ex)
            result['per_account'][acct or '(default)'] = info
        return result

    @log_method
    async def get_shortable_shares(self, contract: Contract) -> float:
        return await self.client.get_shortable_shares(contract)

    @log_method
    def release_client_id(self, client_id: int):
        if client_id in self.tws_client_ids:
            self.tws_client_ids.remove(client_id)

    def start_load_test(self):
        async def _load_test_helper():
            amd = Contract(symbol='AMD', conId=4391, exchange='SMART', primaryExchange='NASDAQ', currency='USD')
            ticker = Ticker(
                contract=amd,
                time=dt.datetime.now(),
                bid=87.05,
                ask=87.06,
                prevBid=87.05,
                prevAsk=87.06,
                askSize=100.0,
                bidSize=100.0,
                prevAskSize=100.0,
                prevBidSize=100.0,
                lastSize=0,
                halted=0,
                close=85.00,
                low=84.00,
                high=86.00,
                open=85.50,
                last=87.05,
            )
            counter = 0
            timer = dt.datetime.now()
            while self.load_test:
                self.client._contracts_source.on_next(set([ticker]))

                # asyncio.sleep(0)
                # any asyncio.sleep here seems to give us a 100x slowdown.
                # await asyncio.sleep(0.000001)
                # sleep 0.000001 give us about 9000 /sec.
                # asyncio.sleep(0) gives us about 29k tickers/sec
                # no sleep gives us 400k/sec but no active control over the process
                counter = counter + 1
                delta = dt.datetime.now() - timer
                if delta.seconds >= 10:
                    task_num = len(asyncio.all_tasks())
                    threading_num = threading.active_count()
                    logging.critical(
                        '{} tickers per second, {} tasks, {} threads'.format(
                            float(counter) / 10.0,
                            task_num,
                            threading_num
                        )
                    )
                    counter = 0
                    timer = dt.datetime.now()
            logging.debug('load test stopped')

        self.load_test = True
        logging.critical('starting start_load_test()')
        task = asyncio.create_task(_load_test_helper())

    def run(self, *args):
        if getattr(self, '_fake_broker_schedule_connected', False):
            self._fake_broker_schedule_connected = False
            loop = asyncio.get_event_loop()
            loop.create_task(self.connected_event())
        self.client.run(*args)

    def _fake_broker_sync_client(self):
        """Minimal IB stand-in so run_broker_sync can promote a generation
        under MMR_FAKE_BROKER without dialing a real Gateway socket."""
        from types import SimpleNamespace

        account = self.ib_account

        class _FakeIB:
            def accountValues(self, _account_id):
                return [SimpleNamespace(
                    account=account, tag='NetLiquidation',
                    currency='USD', value='100000',
                )]

            async def reqPositionsAsync(self):
                return []

            async def reqAllOpenOrdersAsync(self):
                return []

            async def reqCompletedOrdersAsync(self, *, apiOnly):
                return []

            async def reqExecutionsAsync(self):
                return []

        return SimpleNamespace(ib=_FakeIB())


class TradingRuntimeOrderDispatch:
    """[M1-F3] Task 5: ``OrderDispatchPort`` over the trader's async
    ``place_expressive_order``.

    The approval saga runs on the coordinator/RPC thread; this adapter bridges
    that synchronous call onto the trader's own event loop
    (``run_coroutine_threadsafe`` against ``_main_loop``, the same loop the PnL
    off-loop routing uses) and maps the resulting ``SuccessFail`` onto the
    port contract:

    - ``is_success()`` → ``SubmittedOrders`` (this deliberately INCLUDES the
      "placed but ack slow" case: ``place_expressive_order`` returns SUCCESS on
      its 8s decisive-wait timeout because the order IS working, which is a
      genuine ``SUBMITTED``, not an ambiguous dispatch);
    - a clean ``sf.error`` (invalid spec, denylist, leverage/risk reject,
      "Failed to place entry order", bracket-aborted-and-rolled-back, "Order
      rejected by IB") → ``BrokerRejectedError`` (no live order left behind →
      proposal ``FAILED``);
    - ``sf.exception`` or a cross-thread ``TimeoutError``/disconnect → propagate
      a generic ``Exception`` (the saga treats it as ``OUTCOME_UNKNOWN`` and
      NEVER auto-retries a possibly-live real-money dispatch).

    The correlatable ``order_ref`` (``encode_order_ref(order_group_id)`` →
    ``"mmr:og-<cmd>"``) is stamped on EVERY bracket leg by
    ``place_expressive_order`` so the Task-9 reconciler can bind all
    ``broker_orders`` rows back to the group.
    """

    def __init__(self, trader: 'Trader', *, dispatch_timeout: float = 30.0, policy=None):
        self._trader = trader
        self._dispatch_timeout = dispatch_timeout
        self._policy = policy

    def submit(self, proposal, order_ref: str, order_group_id: str):
        from trader.trading.command_coordinator import BrokerRejectedError, SubmittedOrders

        if self._policy is not None:
            import math
            account = getattr(proposal, 'account_id', None)
            mode = getattr(proposal, 'account_mode', None)
            if account != getattr(self._trader, 'ib_account', None):
                raise BrokerRejectedError('proposal account does not match trader account')
            expected_mode = 'paper' if getattr(self._trader, 'paper_trading', False) else 'live'
            if mode != expected_mode:
                raise BrokerRejectedError('proposal account mode does not match trader mode')
            try:
                notional = abs(float(proposal.quantity) * float(proposal.reference_price))
            except (TypeError, ValueError) as exc:
                raise BrokerRejectedError('order notional is invalid') from exc
            ceiling = self._policy.max_order_notional
            if not math.isfinite(notional) or (
                ceiling is not None and notional > float(ceiling)
            ):
                raise BrokerRejectedError('order notional exceeds trader policy')

        loop = getattr(self._trader, '_main_loop', None)
        if loop is None:
            raise RuntimeError('trader event loop unavailable for order dispatch')
        contract = Contract(
            conId=int(proposal.conid),
            symbol=proposal.symbol,
            secType=proposal.sec_type or 'STK',
            exchange='SMART',
            currency='USD',
        )
        execution_spec = dict(proposal.execution or {})
        quantity = float(proposal.quantity or 0.0)

        future = asyncio.run_coroutine_threadsafe(
            self._trader.place_expressive_order(
                contract, proposal.action, quantity, execution_spec, algo_name=order_ref,
            ),
            loop,
        )
        # .result() may raise TimeoutError (cross-thread wait) or a disconnect
        # exception — both propagate as generic Exception → OUTCOME_UNKNOWN.
        sf = future.result(timeout=self._dispatch_timeout)

        if sf.is_success():
            trades = sf.obj or []
            order_ids = [
                int(t.order.orderId) for t in trades
                if getattr(t, 'order', None) is not None
            ]
            return SubmittedOrders(
                order_group_id=order_group_id, order_ref=order_ref, order_ids=order_ids,
            )
        if sf.error is not None:
            raise BrokerRejectedError(str(sf.error))
        if sf.exception is not None:
            raise sf.exception
        raise BrokerRejectedError('order dispatch failed with no error detail')

    def cancel(self, order_entity_id: str, order_ref: str):
        # Resolve the STABLE perm_id for this order entity from the persisted
        # broker_order_aliases table (the entity id is order_group_id:leg /
        # ext:<uuid> -- perm_id is an alias, never the key), then match the
        # CURRENT session's open trades by perm_id (never a session-scoped
        # orderId, which can go stale / be reused across a reconnect).
        #
        # A cancel we cannot resolve to a LIVE order RAISES rather than
        # returning: the coordinator treats a non-raising return as SUBMITTED,
        # so a silent "cancelled=False" would falsely report success. Raising
        # sends the command to OUTCOME_UNKNOWN, and the Task-9 reconciler
        # resolves the true state from broker_orders (already terminal -> no-op
        # success; still working -> retry). NEVER report SUBMITTED for an order
        # we did not cancel. Pure matcher: command_ports.resolve_cancel_target.
        from trader.trading.command_coordinator import CancelAck
        from trader.trading.command_ports import CancelUnresolved, resolve_cancel_target
        perm_id = self._perm_id_for_order(order_entity_id)
        order = resolve_cancel_target(perm_id, self._open_trades())
        if order is None:
            raise CancelUnresolved(
                f"no live order to cancel for {order_entity_id!r} "
                f"(perm_id={perm_id}); deferring to reconciliation")
        self._trader.client.ib.cancelOrder(order)
        return CancelAck(order_entity_id=order_entity_id, cancelled=True)

    def reduce_position(self, position, side: str, quantity: float, order_ref: str):
        """Submit an emergency reduce-only market order.

        This intentionally bypasses proposal semantics, but not the trader's
        one real-order boundary.  The caller supplies a broker position and
        this method re-derives side/quantity, rejecting any request which
        could increase or flip exposure.
        """
        broker_quantity = float(position.quantity)
        expected_side = 'SELL' if broker_quantity > 0 else 'BUY'
        if broker_quantity == 0 or side != expected_side or float(quantity) != abs(broker_quantity):
            raise ValueError('liquidation order must exactly reduce the broker position')
        loop = getattr(self._trader, '_main_loop', None)
        if loop is None:
            raise RuntimeError('trader event loop unavailable for liquidation')
        contract = Contract(
            conId=int(position.conid), symbol=position.symbol,
            secType=position.sec_type or 'STK', exchange=position.exchange or 'SMART',
            currency=position.currency or 'USD',
        )
        future = asyncio.run_coroutine_threadsafe(
            self._trader.place_expressive_order(
                contract, side, abs(broker_quantity),
                {'order_type': 'MARKET', 'exit_type': 'NONE', 'tif': 'DAY', 'outside_rth': False},
                algo_name=order_ref,
            ), loop,
        )
        result = future.result(timeout=self._dispatch_timeout)
        if result.is_success():
            return result.obj or []
        if result.exception is not None:
            raise result.exception
        raise RuntimeError(str(result.error or 'liquidation dispatch failed'))

    def _perm_id_for_order(self, order_entity_id: str):
        # Conn-free reverse alias lookup (order_entity_id -> perm_id). Fail-safe:
        # no store -> None -> cancel raises CancelUnresolved (reconciler resolves).
        store = getattr(self._trader, 'broker_state_store', None)
        journal = getattr(self._trader, 'domain_journal', None)
        if store is None or journal is None:
            return None
        return store.find_perm_id_for_order_in_tx(journal.connect(), order_entity_id)

    def find_by_order_ref(self, account_id: str, order_ref: str) -> list:
        # Read-only correlation: broker_orders rows for this command's order
        # group (og-{command_id}, stable across reconnects). A non-empty result
        # proves the order was submitted -> the reconciler marks it EXECUTED.
        from trader.trading.command_ports import orders_matching_group
        from trader.trading.order_correlation import decode_order_ref
        group = decode_order_ref(order_ref)
        if group is None:
            return []
        return orders_matching_group(self._active_order_rows(), account_id, group)

    def _open_trades(self) -> list:
        ib = getattr(getattr(self._trader, 'client', None), 'ib', None)
        return list(ib.openTrades()) if ib is not None else []

    def _active_order_rows(self) -> list:
        # Conn-free read over [M1-F2]'s materialized broker_orders store -- mirror
        # of trader_service._BrokerStoreOrderView. Fail-safe: no store -> [] (the
        # reconciler then treats absence as unknown, never a false EXECUTED).
        store = getattr(self._trader, 'broker_state_store', None)
        journal = getattr(self._trader, 'domain_journal', None)
        if store is None or journal is None:
            return []
        return store.select_active_orders_in_tx(journal.connect())

    def enumeration_complete(self) -> bool:
        ingest = getattr(self._trader, 'broker_ingest', None)
        return bool(ingest is not None and ingest.is_ready())
