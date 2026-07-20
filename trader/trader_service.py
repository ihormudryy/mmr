from asyncio import AbstractEventLoop
from trader.common.helpers import get_network_ip
from trader.common.logging_helper import LogLevels, set_all_log_level, setup_logging
from trader.container import Container, default_config_path
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.trading_control import TradingControlStore, apply_trading_control_migration
from trader.trading.trading_runtime import Trader

import asyncio
import click
import datetime as dt
import logging as log
import os
import signal


logging = setup_logging(module_name='trader_service')


def _resolve_seed_accounts(
    live_account: str | None,
    paper_account: str | None,
    active_account: str | None,
    paper_trading: bool,
) -> list[tuple[str, str]]:
    """Resolve the (account_id, mode) set to seed into the pause gate.

    Seeds the configured per-mode accounts (``ib_live_account`` /
    ``ib_paper_account``) when present AND unconditionally unions the
    resolved account this process actually trades under
    (``trader.ib_account`` with its mode from ``trader.paper_trading``).
    An ``IB_ACCOUNT`` env override can leave ``config.ib.account`` diverging
    from the per-mode YAML fields; without this union the ACTIVE account
    could be left unseeded, and once the gate is wired every
    exposure-increasing proposal for it would be refused with a misleading
    ``TRADING_PAUSED`` (``PauseStateUnavailable``) even though nobody paused
    it. The active account's runtime mode wins on an id collision (dict
    overwrite keeps first-insertion order, so [live, paper] ordering holds).
    """
    seed: dict[str, str] = {}
    if live_account:
        seed[live_account] = 'live'
    if paper_account:
        seed[paper_account] = 'paper'
    if active_account:
        seed[active_account] = 'paper' if paper_trading else 'live'
    return list(seed.items())


class _LoggingCriticalAlerts:
    """[M1-F3] Task 9 ``CriticalAlertPort``: an ``OUTCOME_UNKNOWN`` command
    still unresolved after 15 minutes is surfaced to the log for the operator.

    The durable, authoritative signal is the ``OUTCOME_UNKNOWN`` ledger row
    itself (which ``[M1-R]`` health rendering reads directly); this handler is
    the operational breadcrumb so it also shows up in the service log.
    """

    def raise_alert(self, command_id: str, detail: str) -> None:
        logging.error(
            'command %s requires operator reconciliation: %s', command_id, detail
        )


class _BrokerStoreOrderView:
    """[M1-F3] Task 9 MEDIUM-2 production ``OrderStateView``.

    Wraps [M1-F2]'s ``BrokerStateStore.get_order_in_tx`` behind the conn-free
    ``get_order`` seam the ``OutcomeReconciler``'s cancel reconciliation reads,
    so an ambiguous ``cancel_order`` resolves against the TARGET order's
    authoritative materialized status instead of the always-empty ``og-*``
    order-ref lookup (a ``cancel_order`` creates no order group). Mirrors the
    same conn-free seam the cancel saga itself consumes.
    """

    def __init__(self, store, connect):
        self._store = store
        self._connect = connect

    def get_order(self, order_entity_id):
        return self._store.get_order_in_tx(self._connect(), order_entity_id)


def build_order_state_view(trader: Trader):
    """Build the production ``OrderStateView`` for the command reconciler, or
    ``None`` when [M1-F2]'s materialized broker-order store is not attached
    (dormant). The integration step that constructs the ``OutcomeReconciler``
    passes this as its ``orders_view`` so a ``cancel_order`` wedge reconciles
    against the ``broker_orders`` store's authoritative status. Returning
    ``None`` keeps the cancel branch fail-safe (it stays OUTCOME_UNKNOWN rather
    than rubber-stamping RESOLVED) when the store is unavailable."""
    store = getattr(trader, 'broker_state_store', None)
    journal = getattr(trader, 'domain_journal', None)
    if store is None or journal is None:
        return None
    return _BrokerStoreOrderView(store, journal.connect)


async def _command_reconciliation_loop(
    reconciler,
    ledger,
    *,
    now,
    reconcile_interval: float = 5.0,
    retention_interval: float = 86_400.0,
):
    """[M1-F3] Task 9: drive ``OUTCOME_UNKNOWN`` reconciliation on a fixed
    5-second cadence and purge terminal (``RESOLVED``/``REJECTED``) ledger +
    audit rows once a day.

    The loop NEVER dies on a transient failure: each tick's error is logged
    and the loop continues, because an ambiguous real-money command must keep
    being reconciled until it actually resolves. ``purge_expired`` never
    touches ``OUTCOME_UNKNOWN`` (see its own docstring), so retention can never
    drop an unreconciled command.
    """
    last_retention = now()
    while True:
        try:
            reconciler.run_due(now())
        except Exception as ex:
            logging.error('command reconciliation tick failed: {}'.format(ex))
        try:
            if (now() - last_retention).total_seconds() >= retention_interval:
                purged = ledger.purge_expired(now())
                last_retention = now()
                if purged:
                    logging.info('purged {} terminal command-ledger rows'.format(purged))
        except Exception as ex:
            logging.error('command-ledger retention failed: {}'.format(ex))
        await asyncio.sleep(reconcile_interval)


def _maybe_start_command_reconciliation(trader: Trader, loop: AbstractEventLoop) -> None:
    """[M1-F3] Task 9: run ``rescan_on_startup()`` BEFORE readiness (coordinator
    crash recovery, spec §9.5) and start the reconciliation + daily-retention
    loop.

    Additive + DORMANT by design (addendum §3): T9-core deliberately leaves the
    live command DISPATCH unwired (no ``register_command_authority`` on the live
    socket, no live command registry bound in ``Trader.connect()``), so no
    ``command_reconciler`` is attached to the trader yet and this is a no-op --
    startup behaviour is byte-for-byte unchanged. The coordinated integration
    step that binds the live command authority also sets
    ``trader.command_reconciler`` / ``trader.command_ledger`` and thereby
    activates this loop. Any failure here is logged and swallowed so it can
    never take down service startup.
    """
    reconciler = getattr(trader, 'command_reconciler', None)
    ledger = getattr(trader, 'command_ledger', None)
    if reconciler is None or ledger is None:
        return
    try:
        # [M1-F3] MEDIUM-2: ensure the reconciler can read the TARGET order's
        # authoritative status for a cancel_order wedge. If the integration
        # step that constructed the reconciler left it without an OrderStateView,
        # attach the production one now (still fail-safe: None leaves the cancel
        # branch unresolved rather than rubber-stamping).
        if getattr(reconciler, '_orders_view', None) is None:
            view = build_order_state_view(trader)
            if view is not None:
                reconciler._orders_view = view
        requeued = reconciler.rescan_on_startup()
        if requeued:
            logging.info(
                'requeued {} in-flight command(s) for reconciliation'.format(len(requeued))
            )
        loop.create_task(_command_reconciliation_loop(
            reconciler, ledger, now=lambda: dt.datetime.now(dt.timezone.utc),
        ))
    except Exception as ex:
        logging.error('failed to start command reconciliation: {}'.format(ex))


async def _liquidation_recovery_loop(service, *, interval: float = 5.0) -> None:
    """Keep unresolved verified-liquidation roots moving after restart.

    Every transition still requires a newly promoted broker snapshot; a loop
    tick can never manufacture a flat result.  Errors are contained so an IB
    outage preserves the durable run for the next tick rather than killing the
    trader process.
    """
    while True:
        try:
            receipt = service.rescan()
            if receipt is not None and receipt.state != 'FLAT':
                logging.warning('liquidation %s remains %s: %s', receipt.cause_command_id,
                                receipt.state, receipt.detail)
        except Exception as ex:
            logging.error('liquidation recovery tick failed: {}'.format(ex))
        await asyncio.sleep(interval)


def _maybe_start_liquidation_recovery(trader: Trader, loop: AbstractEventLoop) -> None:
    """Rescan durable liquidation roots before normal service operation."""
    service = getattr(trader, 'liquidation_service', None)
    if service is None:
        return
    try:
        first = service.rescan()
        if first is not None and first.state != 'FLAT':
            logging.warning('resumed liquidation %s in state %s', first.cause_command_id, first.state)
        loop.create_task(_liquidation_recovery_loop(service))
    except Exception as ex:
        logging.error('failed to start liquidation recovery: {}'.format(ex))


async def _session_controller_loop(controller, *, interval: float = 5.0) -> None:
    """Tick absolute session deadlines until flat or incident."""
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            state = controller.run_due(now)
            if state.state not in ('FLAT', 'INCIDENT', 'CLOSED'):
                logging.debug(
                    'session controller %s state=%s cutoff=%s',
                    state.session_date, state.state, state.entry_cutoff_reached,
                )
        except Exception as ex:
            logging.error('session controller tick failed: {}'.format(ex))
        await asyncio.sleep(interval)


def _maybe_start_session_recovery(trader: Trader, loop: AbstractEventLoop) -> None:
    """P3 Task 6: resume session deadlines BEFORE semantic readiness / run().

    Session recovery must precede readiness so a restart mid-flatten cannot
    open a window where automation is 'ready' but deadlines are unenforced.
    """
    controller = getattr(trader, 'session_controller', None)
    if controller is None:
        return
    try:
        now = dt.datetime.now(dt.timezone.utc)
        state = controller.recover(now)
        if state.state not in ('FLAT', 'CLOSED'):
            logging.warning(
                'resumed session %s in state %s (incident=%s)',
                state.session_date, state.state, state.incident,
            )
        loop.create_task(_session_controller_loop(controller))
    except Exception as ex:
        logging.error('failed to start session recovery: {}'.format(ex))


def _seed_trading_control(trader: Trader, container: Container) -> TradingControlStore:
    """[M1-F3] Task 4: seed the durable per-account pause gate BEFORE this
    service is considered ready (i.e. before ``trader.run()`` starts the
    main loop). ``trader.connect()`` has already migrated/opened the
    journal file (``trader.journal_db`` / ``trader.domain_journal``) by the
    time this runs, so this only adds migration 22 and seeds whichever
    accounts are configured.

    Seeds BOTH the configured live and paper accounts (``ib_live_account``/
    ``ib_paper_account``) when present -- a single journal file governs
    pause state for both, even though this process only ever ACTS as one
    of them -- and ALWAYS unions the currently-active resolved account
    (``trader.ib_account`` / ``trader.paper_trading``). An ``IB_ACCOUNT``
    env-var override can leave ``config.ib.account`` diverging from the
    per-mode config fields, so unconditionally including the active account
    (see ``_resolve_seed_accounts``) is what guarantees the account this
    process actually trades under is never left ungoverned.

    Runs inside ONE transaction (``seed_in_tx`` never opens its own --
    see ``trading_control.py``'s module docstring) via
    ``trader.journal_db.transaction(...)``.
    """
    migrator = SchemaMigrator(trader.journal_db)
    apply_trading_control_migration(migrator)
    store = TradingControlStore(trader.domain_journal)

    ib_config = container.typed_config().ib
    accounts = _resolve_seed_accounts(
        ib_config.live_account, ib_config.paper_account,
        trader.ib_account, trader.paper_trading,
    )

    if accounts:
        now = dt.datetime.now(dt.timezone.utc)
        trader.journal_db.transaction(lambda conn: store.seed_in_tx(conn, accounts, now))

    trader.trading_control_store = store
    return store


@click.command()
@click.option('--simulation', required=False, default=False, help='load with historical data')
@click.option('--debug', is_flag=True, default=False, help='enable verbose ib_async debug logging')
@click.option('--config', required=False, default='',
              help='trader.yaml config file location')
def main(simulation: bool,
         debug: bool,
         config: str):

    if not config:
        config = default_config_path()

    is_stopping = False
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    container = Container.create(config)
    trader = container.resolve(Trader, simulation=simulation)

    async def graceful_shutdown():
        nonlocal is_stopping
        if is_stopping:
            return
        is_stopping = True
        logging.info('shutting down...')
        try:
            await trader.shutdown()
        except Exception as ex:
            logging.error('error during shutdown: {}'.format(ex))

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if pending:
            logging.debug('waiting five seconds for {} pending tasks'.format(len(pending)))
            await asyncio.wait(pending, timeout=5)
            # Cancel anything still running
            still_pending = [t for t in pending if not t.done()]
            for t in still_pending:
                t.cancel()
            if still_pending:
                # Give cancelled tasks a moment to handle CancelledError
                await asyncio.wait(still_pending, timeout=2)
        loop.stop()

    def handle_sigint():
        asyncio.ensure_future(graceful_shutdown())

    if simulation:
        logging.info('simulation mode: use the backtest CLI command instead')

    try:
        if os.environ.get('TRADER_NODEBUG'):
            set_all_log_level(LogLevels.CRITICAL)
        else:
            loop.set_debug(enabled=True)

        if debug:
            from trader.common.logging_helper import set_external_log_level
            set_external_log_level(LogLevels.DEBUG)
            logging.info('verbose ib_async debug logging enabled')

        loop.add_signal_handler(signal.SIGINT, handle_sigint)
        loop.add_signal_handler(signal.SIGTERM, handle_sigint)

        trader.connect()

        # [M1-F3] Task 4: the durable per-account pause gate must be seeded
        # before this service is considered ready -- an exposure-increasing
        # command that races startup must find a governed (not missing)
        # row, never a silent bypass. Uses the SAME journal file connect()
        # just migrated/opened.
        _seed_trading_control(trader, container)

        # [M1-F3] Task 9: run coordinator crash-recovery rescan before readiness
        # and start the OUTCOME_UNKNOWN reconciliation + daily retention loop.
        # Dormant until the live command authority is wired (see the function's
        # docstring) -- a no-op here today, never a startup regression.
        _maybe_start_command_reconciliation(trader, loop)
        _maybe_start_liquidation_recovery(trader, loop)
        # P3 Task 6: session deadline recovery must start before readiness/run.
        _maybe_start_session_recovery(trader, loop)

        ip_address = get_network_ip()
        logging.debug('starting trading_runtime at network address: {}'.format(ip_address))

        logging.debug('starting trader run() loop')
        trader.run()

    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass


if __name__ == '__main__':
    main()
