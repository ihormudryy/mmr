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
