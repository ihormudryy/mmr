"""Build the scoreboard inside trader_service's command stack (Plan 5 Task 10).

Everything writes only to the trader-owned journal DuckDB file. The loop body
(``ScoreboardServices.tick``) runs each step on its own: one failing step is
logged and never stops the others.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from trader.scoreboard.bar_sources import default_bar_sources
from trader.scoreboard.benchmark import BenchmarkBook, BenchmarkSourceError, read_spy_closes
from trader.scoreboard.close_fills import JournalCloseFills
from trader.scoreboard.fx import IbFxEvidence
from trader.scoreboard.ingest import AiIngest
from trader.scoreboard.ports import (DecisionStoreAttribution, DecisionStoreCloseLinks, DecisionStoreFacts,
                                     NullAttributionLookup, NullCloseFills, NullDecisionFacts, NullExperimentReader)
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.service import ScoreboardService
from trader.scoreboard.session_ledger import SessionLedger
from trader.scoreboard.session_simulator import SessionSimulator
from trader.scoreboard.store import ScoreboardStore
from trader.scoreboard.summary_text import DailySummaryProducer
from trader.scoreboard.telegram_sender import build_telegram

logger = logging.getLogger(__name__)


@dataclass
class ScoreboardServices:
    store: ScoreboardStore
    ledger: SessionLedger
    book: BenchmarkBook
    service: ScoreboardService
    broker: Any
    account_id: str
    now: Callable[[], dt.datetime]
    outbox: Any = None          # TelegramOutbox when ai_paper.telegram.enabled
    sender: Any = None          # TelegramSender when enabled
    producer: Any = None        # DailySummaryProducer when enabled
    ingest: Any = None          # AiIngest: record_ai_cost / record_simulated_decision (SP2 Plan 2)
    simulator: Any = None       # SessionSimulator: baseline outcomes from 1-minute bars (SP2 Plan 2)
    close_fills: Any = None     # CloseFills the simulator proves matched-entry shares with (Ruling 21)

    def recover(self, now: Optional[dt.datetime] = None) -> list[str]:
        """Startup: rows for past sessions without one (ruling 6), then a lost summary (if Telegram is on)."""
        written = self.ledger.recover(now or self.now())
        if written:
            logger.warning("scoreboard recovered equity_daily rows for %s", ", ".join(written))
        self._step("summary catch-up", self._catch_up)
        return written

    def tick(self) -> None:
        """The 30 s loop: peak exposure, projection + late commissions, SPY closes, summaries, Telegram."""
        self._step("peak observation", self._observe)
        self._step("refresh", self.service.refresh)
        self._step("benchmark refresh", self._benchmark)
        self._step("summary catch-up", self._catch_up)
        if self.simulator is not None:
            self._step("simulation", self.simulator.run_due)
        if self.sender is not None:
            self._step("telegram drain", self.sender.drain)

    @staticmethod
    def _step(name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            logger.exception("scoreboard %s failed; the next tick retries", name)

    def _observe(self) -> None:
        if self.ledger.experiments.latest() is None:
            return
        self.ledger.observe_snapshot(self.broker.capture(self.account_id))

    def _benchmark(self) -> None:
        try:
            self.service.refresh_benchmark()
        except BenchmarkSourceError as exc:
            logger.warning("scoreboard SPY benchmark not refreshed: %s", exc)

    def _catch_up(self) -> None:
        if self.producer is not None:
            self.producer.catch_up()


def _history_source(history_db_path: str) -> Callable[[dt.date, dt.date], dict]:
    def read(start: dt.date, end: dt.date) -> dict:
        if not history_db_path:
            raise BenchmarkSourceError("no history_duckdb_path is configured; the SPY benchmark is unknown")
        return read_spy_closes(history_db_path, start, end)
    return read


def _telegram_section(trader: Any) -> Optional[dict]:
    config = getattr(trader, "ai_paper_config", None)
    raw = getattr(config, "raw_section", None) or {}
    return raw.get("telegram")


def build_scoreboard(trader: Any, *, migrator: Any, broker: Any, experiments: Any, decision_store: Any,
                     calendar: Any, cash: Callable[[], dict], now: Callable[[], dt.datetime],
                     sizer: Any = None, command_ledger: Any = None,
                     bar_sources: Optional[Sequence[Any]] = None) -> ScoreboardServices:
    """Raises TelegramConfigError when ai_paper.telegram is enabled but invalid (startup stops, ruling 17).

    ``command_ledger`` is the SP1 ``CommandLedger``: a placed ENTER's size is read from its receipt.
    """
    apply_scoreboard_migrations(migrator)
    db = trader.journal_db
    store = ScoreboardStore(db, now=now)
    reader = experiments if experiments is not None else NullExperimentReader()
    links = DecisionStoreAttribution(decision_store) if decision_store is not None else NullAttributionLookup()
    ledger = SessionLedger(store=store, db=db, experiments=reader, broker=broker, fx=IbFxEvidence(cash, now),
                           calendar=calendar, links=links, now=now)
    book = BenchmarkBook(store, _history_source(getattr(trader, "history_duckdb_path", "") or ""), calendar, now)
    outbox, sender = build_telegram(_telegram_section(trader), db, now)
    service = ScoreboardService(store=store, db=db, experiments=reader, ledger=ledger, book=book, links=links,
                                calendar=calendar, now=now, outbox=outbox)
    decisions = NullDecisionFacts() if decision_store is None else DecisionStoreFacts(decision_store, command_ledger)
    ingest = AiIngest(store=store, experiments=reader, decisions=decisions, calendar=calendar, now=now, sizer=sizer)
    close_fills = NullCloseFills() if decision_store is None else JournalCloseFills(
        store, DecisionStoreCloseLinks(decision_store), decisions, ledger.trip_executions)
    simulator = SessionSimulator(store=store, calendar=calendar,
                                 sources=default_bar_sources(trader) if bar_sources is None else bar_sources,
                                 now=now, close_fills=close_fills)
    producer = None
    if outbox is not None:
        producer = DailySummaryProducer(service, outbox, now=now)
        ledger.on_row_written = producer.on_session_row
    logger.warning("scoreboard ready; telegram summaries %s", "ON" if sender is not None else "off")
    return ScoreboardServices(store=store, ledger=ledger, book=book, service=service, broker=broker,
                              account_id=trader.ib_account, now=now, outbox=outbox, sender=sender, producer=producer,
                              ingest=ingest, simulator=simulator, close_fills=close_fills)
