"""The ai service: `python -m trader.ai_service` (SP2 Plan 5).

Holds model-provider credentials only (spec 4) and talks to the trader over
signed typed RPC as ai_supervisor and ai_research. It never publishes a risk
policy (spec 6.7) and never changes the budget cap from an AI path (spec 5.4):
the cap is the owner's trader.yaml value, read with get_ai_model_budget (Ruling 19).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from trader.ai.budget_cap import BudgetCapSync, CapGatedGateway
from trader.ai.clock import Clock, SystemClock
from trader.ai.config import DEFAULT_CONFIG_PATH, AiConfig, AiConfigError, check_credentials, load_ai_config
from trader.ai.controller import AiController, ExperimentWatch
from trader.ai.engine import DecisionEngine
from trader.ai.gateway import ModelCaller, build_gateway
from trader.ai.leadership import Leadership, new_holder_id
from trader.ai.outbox import ReportingOutbox
from trader.ai.replay import ReplayRecorder
from trader.ai.rpc_clients import AiRpcClients, ReadOnlySupervisor
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.signal_intake import SignalIntake
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter

logger = logging.getLogger("trader.ai_service")

DEFAULT_TRADER_ADDRESS = "tcp://127.0.0.1"
EXIT_REFUSED = 2


class EngineNotInstalled(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineDeps:
    config: AiConfig
    gateway: ModelCaller
    reads: ReadOnlySupervisor
    clock: Clock
    recorder: ReplayRecorder
    store: AiStore


def build_engine(deps: EngineDeps) -> DecisionEngine:
    """Plan 6 installs the real engine here (Ruling 16). Until then the service refuses to start."""
    raise EngineNotInstalled("no decision engine is installed (SP2 Plan 6); the ai service will not start")


@dataclass(frozen=True)
class ServiceSettings:
    config_path: str = DEFAULT_CONFIG_PATH
    keys_dir: Optional[str] = None
    trader_address: str = DEFAULT_TRADER_ADDRESS


async def serve(settings: ServiceSettings, *, engine_factory: Callable[[EngineDeps], Any], stop: asyncio.Event,
                clock: Optional[Clock] = None, environ: Optional[Mapping[str, str]] = None,
                wrap_clients: Optional[Callable[[AiRpcClients], AiRpcClients]] = None) -> None:
    clock = clock or SystemClock()
    environ = os.environ if environ is None else environ
    config = load_ai_config(settings.config_path)
    check_credentials(config, environ)                    # names missing variables only, never values
    cfg = config.controller
    Path(config.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    store = AiStore(config.database_path, clock=clock)
    await asyncio.to_thread(store.migrate, ALL_MIGRATIONS)
    clients = await asyncio.to_thread(lambda: AiRpcClients.connect(
        keys_dir=settings.keys_dir, address=settings.trader_address, query_port=cfg.trader_query_port,
        command_port=cfg.trader_command_port, timeout=cfg.rpc_timeout_seconds))
    if wrap_clients is not None:
        clients = wrap_clients(clients)
    try:
        raw_gateway = build_gateway(config, store=store, clock=clock, environ=environ)
        cap_sync = BudgetCapSync(supervisor=clients.supervisor, budget=raw_gateway.budget, clock=clock)
        gateway = CapGatedGateway(raw_gateway, cap_sync)  # no model call without a current owner cap
        engine = engine_factory(EngineDeps(config, gateway, ReadOnlySupervisor(clients.supervisor), clock,
                                           ReplayRecorder(store), store))
        leadership = Leadership(supervisor=clients.supervisor, store=store, clock=clock, holder_id=new_holder_id(),
                                lease_seconds=cfg.lease_seconds, renew_seconds=cfg.renew_seconds,
                                held_retry_seconds=cfg.held_retry_seconds)
        clients.supervisor.bind_epoch(leadership.current_epoch)
        logger.info("waiting for the controller epoch as %s (up to one lease after a restart)",
                    leadership.holder_id)
        if await leadership.acquire(stop) is None:
            return
        await raw_gateway.start()                         # only the leader turns half-finished calls into UNKNOWN
        slots = SessionSlots(entry_minutes=cfg.entry_slot_minutes, position_minutes=cfg.position_slot_minutes,
                             grace_seconds=cfg.slot_start_grace_seconds)
        watch = ExperimentWatch(clients.supervisor)
        submitter = Submitter(store=store, supervisor=clients.supervisor, leadership=leadership, clock=clock,
                              slots=slots, experiment_state=watch.state,
                              not_found_settle_seconds=cfg.not_found_settle_seconds)
        controller = AiController(
            config=cfg, store=store, clock=clock, supervisor=clients.supervisor, leadership=leadership, watch=watch,
            submitter=submitter,
            outbox=ReportingOutbox(store=store, journal=gateway.journal, supervisor=clients.supervisor, clock=clock),
            intake=SignalIntake(store=store, supervisor=clients.supervisor, clock=clock,
                                page_limit=cfg.signal_page_limit, max_age_seconds=cfg.signal_max_age_seconds),
            slots=slots, engine=engine, gateway=gateway, cap_sync=cap_sync)
        await controller.run(stop)
    finally:
        clients.close()


def run_service(settings: ServiceSettings, *, engine_factory: Callable[[EngineDeps], Any] = build_engine,
                clock: Optional[Clock] = None, environ: Optional[Mapping[str, str]] = None,
                wrap_clients: Optional[Callable[[AiRpcClients], AiRpcClients]] = None) -> int:
    async def main_task() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await serve(settings, engine_factory=engine_factory, stop=stop, clock=clock, environ=environ,
                    wrap_clients=wrap_clients)
    try:
        asyncio.run(main_task())
    except EngineNotInstalled as exc:
        logger.error("%s", exc)
        return EXIT_REFUSED
    except AiConfigError as exc:
        logger.error("ai service refused to start: %s", exc.code)
        return EXIT_REFUSED
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.ai_service")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--keys-dir", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ServiceSettings(config_path=args.config, keys_dir=args.keys_dir,
                               trader_address=os.environ.get("TRADER_TYPED_ADDRESS", DEFAULT_TRADER_ADDRESS))
    return run_service(settings)


if __name__ == "__main__":
    sys.exit(main())
