"""The research service: `python -m trader.research_service` (SP2c spec 5.1).

A typed RPC server (42106 query, 42107 command) that claims evaluation slots at the trader, runs cohort
evaluations one at a time, signs evaluation cases, turns a durable DEPLOY judgment into the paper llm review
and a bundle, and replays every judged strategy nightly into the trader's shadow books. It talks to no broker
and calls no model.

Start order: recover from the trader first, then start the worker, then open the sockets. A caller can
never submit before recovery finished. If a background job dies the process exits non-zero, so compose restarts
it; a running evaluation may finish first (bounded), because killing it would spend its holdout window.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from trader.automation.backtest_judge_config import load_backtest_judge_config
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.messaging.typed_rpc import ServiceIdentity, TypedRpcClient, TypedRpcServer
from trader.research.cohort import build_cohort_spec
from trader.research.cohort_evaluation import evaluate_cohort
from trader.research.evaluation import EvaluationPaths
from trader.research.evaluation_service import EvaluationService
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.judgment_attest import JudgmentAttest, is_paper_posture
from trader.research.key_purpose import default_rpc_keys_dir
from trader.research.research_surface import build_research_registry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import load_research_service_config
from trader.research.service_store import ResearchStore
from trader.research.shadow_replay import ShadowReplay
from trader.research.signing import AttestationSigner
from trader.research.strategy_paths import repo_root
from trader.research.trader_port import TraderPort, TraderUnavailable
from trader.simulation.execution_costs import load_execution_costs_config

logger = logging.getLogger("trader.research_service")
DEFAULT_CONFIG_PATH = "~/.config/mmr/trader.yaml"
ARTIFACTS_ROOT = Path("~/.local/share/mmr/artifacts").expanduser()
DEFAULT_RESEARCH_DB = "~/.local/share/mmr/data/mmr_research.duckdb"
DEFAULT_QUERY_PORT = 42106
DEFAULT_COMMAND_PORT = 42107
RECOVER_FIRST_RETRY_SECONDS = 2.0
RECOVER_MAX_RETRY_SECONDS = 30.0
WATCH_INTERVAL_SECONDS = 0.2
EXIT_BACKGROUND_DIED = 1
EXIT_FAILED = 2
WORKER_JOIN_SECONDS = 5.0
HOLDOUT_FINISH_SECONDS = 30 * 60.0


@dataclass
class ResearchRuntime:
    evaluations: EvaluationService
    servers: list
    background: list            # callables(stop), each run on its own thread once recovery is done
    holdout_jobs: tuple = ()    # the background jobs that may hold an open holdout (see run_service)


def _path(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not value:
        raise ValueError(f"trader.yaml: {key} is required by the research service")
    return str(Path(value).expanduser())


def build_runtime(*, config_path: str, environ: Mapping[str, str], now: Callable[[], dt.datetime]) -> ResearchRuntime:
    """One EvaluationService, one JudgmentAttest and one ShadowReplay for the whole process; nothing is started."""
    config_file = Path(config_path).expanduser()
    raw = yaml.safe_load(config_file.read_text()) or {}
    judge = load_backtest_judge_config((raw.get("ai_paper") or {}).get("backtest_judge"))
    config = load_research_service_config(raw)
    identity = ServiceIdentity.load("research")
    signing_key = default_rpc_keys_dir().parent / "private" / "signing.pem"
    signer = AttestationSigner.from_key_file(str(signing_key))
    db = DuckDBConnection.get_instance(str(Path(environ.get("MMR_RESEARCH_DUCKDB") or DEFAULT_RESEARCH_DB).expanduser()))
    apply_research_migrations(SchemaMigrator(db))
    trader_address = environ.get("TRADER_TYPED_ADDRESS", "tcp://127.0.0.1")
    query = TypedRpcClient("query", identity, server="trader", address=trader_address,
                           port=int(raw.get("typed_query_port", 42101)))
    command = TypedRpcClient("command", identity, server="trader", address=trader_address,
                             port=int(raw.get("typed_command_port", 42102)))
    query.connect()
    command.connect()
    trader = TraderPort(query, command)
    duckdb_path, history_path = _path(raw, "duckdb_path"), _path(raw, "history_duckdb_path")
    universe_library = raw.get("universe_library", "Universes")
    universe = UniverseAccessor(duckdb_path, universe_library)
    costs_path = config_file.parent / "execution_costs.yaml"
    costs = load_execution_costs_config(str(costs_path))
    root = repo_root()
    paths = EvaluationPaths(history_db=history_path, universe_db=duckdb_path, universe_library=universe_library,
                            execution_costs=str(costs_path), repo_root=root,
                            reports_dir=ARTIFACTS_ROOT / "reports", summaries_dir=ARTIFACTS_ROOT / "evaluations")
    registry, store = ExperimentRegistry(db), ResearchStore(db)

    def build_spec(body):
        return build_cohort_spec(body, config=config, judge=judge, universe_accessor=universe, costs_config=costs,
                                 repo_root=root, registry=registry)

    evaluations = EvaluationService(
        store=store, trader=trader, build_spec=build_spec,
        evaluate=lambda spec: evaluate_cohort(spec, research_db=db, paths=paths, now=now), signer=signer,
        artifacts_root=ARTIFACTS_ROOT, warmup_sessions=judge.shadow_warmup_sessions,
        order_notional=config.order_notional, queue_max=config.queue_max, now=now)
    attest = JudgmentAttest(research_db=db, store=store, trader=trader, signer=signer, artifacts_root=ARTIFACTS_ROOT,
                            repo_root=root, is_paper=lambda: is_paper_posture(environ, raw), now=now)
    shadow = ShadowReplay(store=store, trader=trader, signer=signer, artifacts_root=ARTIFACTS_ROOT, paths=paths,
                          registry=registry, config=config, judge=judge, now=now)
    rpc = build_research_registry(evaluations=evaluations, attest=attest)
    bind = environ.get("RESEARCH_TYPED_BIND_ADDRESS", "tcp://127.0.0.1")
    servers = [TypedRpcServer("query", rpc, identity, address=bind,
                              port=int(raw.get("research_typed_query_port", DEFAULT_QUERY_PORT))),
               TypedRpcServer("command", rpc, identity, address=bind,
                              port=int(raw.get("research_typed_command_port", DEFAULT_COMMAND_PORT)))]
    return ResearchRuntime(evaluations=evaluations, servers=servers,
                           background=[evaluations.serve_forever, shadow.serve_forever],
                           holdout_jobs=(evaluations.serve_forever,))


def recover_until_reachable(evaluations: Any, stop: threading.Event) -> bool:
    """Recover our open work from the trader; true when done, false when stopped first.

    An unreachable trader is retried with a doubling, capped wait. Any other failure (the research DB and the
    trader disagree, the trader refuses a call) is a bug or an operator matter and ends the process.
    """
    wait = RECOVER_FIRST_RETRY_SECONDS
    while not stop.is_set():
        try:
            evaluations.recover()
            return True
        except TraderUnavailable as exc:
            logger.warning("trader unreachable during recovery (%s); retrying in %.0f s; no submit is accepted yet",
                           exc, wait)
            stop.wait(wait)
            wait = min(wait * 2, RECOVER_MAX_RETRY_SECONDS)
    return False


def _run_background_job(job: Callable[[threading.Event], None], stop: threading.Event,
                        died: threading.Event) -> None:
    """A job that raises, or returns while the service still runs, ends the whole service."""
    name = getattr(job, "__qualname__", repr(job))
    try:
        job(stop)
    except Exception:
        logger.exception("research background job %s died; the service exits so it is restarted", name)
        died.set()
        return
    if not stop.is_set():
        logger.error("research background job %s returned early; the service exits so it is restarted", name)
        died.set()


async def _serve_until_done(servers: list, stop: threading.Event, died: threading.Event) -> None:
    try:
        await asyncio.gather(*(server.serve() for server in servers))
        logger.info("research service serving")
        while not (stop.is_set() or died.is_set()):
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
    finally:
        await asyncio.gather(*(server.aclose() for server in servers))


def _let_holdout_work_finish(workers: list, holdout_jobs: tuple) -> None:
    """After another job died: an evaluation with an open holdout spends its window if the process exits now."""
    for job, worker in workers:
        if job not in holdout_jobs or not worker.is_alive():
            continue
        logger.warning("a research background job died; waiting up to %.0f min for the running evaluation to "
                       "finish before the exit", HOLDOUT_FINISH_SECONDS / 60)
        worker.join(HOLDOUT_FINISH_SECONDS)
        if worker.is_alive():
            logger.error("the evaluation is still running after %.0f min; the service exits anyway and an open "
                         "holdout window is spent", HOLDOUT_FINISH_SECONDS / 60)


def run_service(runtime: ResearchRuntime, stop: threading.Event) -> int:
    """Recover, start the background jobs, then serve until ``stop`` is set or a job dies. Returns the exit code."""
    if not recover_until_reachable(runtime.evaluations, stop):
        return 0
    died = threading.Event()
    workers = [(job, threading.Thread(target=_run_background_job, args=(job, stop, died), daemon=True))
               for job in runtime.background]
    for _, worker in workers:
        worker.start()
    asyncio.run(_serve_until_done(runtime.servers, stop, died))
    if died.is_set():
        stop.set()                                           # every live job ends after its current item
        _let_holdout_work_finish(workers, runtime.holdout_jobs)
    for _, worker in workers:                                # let a running tick finish; never wait longer
        worker.join(WORKER_JOIN_SECONDS)
    return EXIT_BACKGROUND_DIED if died.is_set() else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        runtime = build_runtime(config_path=os.environ.get("TRADER_CONFIG", DEFAULT_CONFIG_PATH),
                                environ=os.environ, now=lambda: dt.datetime.now(dt.timezone.utc))
        return run_service(runtime, stop)
    except Exception:
        logger.exception("research service stopped by an error")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
