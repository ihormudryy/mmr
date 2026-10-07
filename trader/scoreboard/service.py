"""``ScoreboardService``: refresh the derived tables, build the report, verify (Plan 5 Tasks 5-6)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

from trader.scoreboard.ports import session_date_et
from trader.scoreboard.report import ReportInputs, build_report
from trader.scoreboard.round_trips import project_round_trips
from trader.scoreboard.session_ledger import experiment_fills
from trader.scoreboard.store import ScoreboardStore

logger = logging.getLogger(__name__)

EXPERIMENT_NOT_FOUND = "EXPERIMENT_NOT_FOUND"


class ScoreboardService:
    def __init__(self, *, store: ScoreboardStore, db: Any, experiments: Any, ledger: Any, book: Any, links: Any,
                 calendar: Any, now: Callable[[], dt.datetime], outbox: Any = None):
        self.store = store
        self.db = db
        self.experiments = experiments
        self.ledger = ledger
        self.book = book
        self.links = links
        self.calendar = calendar
        self.outbox = outbox
        self._now = now

    # -- derived tables --------------------------------------------------------

    def _projection(self, experiment: Any):
        facts = experiment_fills(self.db, experiment)
        return project_round_trips(facts, links_for=self.links.links_for_order_ref,
                                   account_id=experiment.account_id)

    def refresh(self) -> None:
        """Ruling 13: rebuild round_trips of the latest experiment and book late commissions."""
        experiment = self.experiments.latest()
        if experiment is None:
            return
        projection = self._projection(experiment)
        self.store.replace_round_trips(
            experiment.experiment_id, [t.as_row(experiment.experiment_id, experiment.account_id)
                                       for t in projection.trips])
        self.ledger.reconcile_commissions()

    def last_completed_session(self, now: dt.datetime) -> dt.date:
        """The newest session whose close has passed, with 15 minutes for the daily bar to land."""
        schedule = self.calendar.resolve(now)
        if schedule is not None and now >= schedule.close_utc + dt.timedelta(minutes=15):
            return schedule.session_date
        return self.calendar.previous_session(session_date_et(now))

    def refresh_benchmark(self) -> int:
        """SPY closes from the session before the start through the last completed session."""
        experiment = self.experiments.latest()
        if experiment is None:
            return 0
        start = self.calendar.previous_session(session_date_et(experiment.started_at))
        end = self.last_completed_session(self._now())
        stopped_at = getattr(experiment, "stopped_at", None)
        if stopped_at is not None:
            end = min(end, session_date_et(stopped_at))
        return self.book.refresh(start, end)

    # -- reads -----------------------------------------------------------------

    def resolve(self, experiment_id: Optional[str]) -> Any:
        return self.experiments.latest() if experiment_id is None else self.experiments.get(experiment_id)

    def report(self, experiment_id: Optional[str] = None) -> dict:
        experiment = self.resolve(experiment_id)
        if experiment is None and experiment_id is not None:
            return {"label": "PAPER", "error_code": EXPERIMENT_NOT_FOUND, "experiment_id": experiment_id}
        if experiment is None:
            return build_report(self._inputs(None, [], [], [], [], [], [], []))
        exp_id = experiment.experiment_id
        warnings = [{"code": "FILL_OUTSIDE_SESSION",
                     "detail": f"fill {piece.exec_id} on {piece.session_date} (not an XNYS session) is in the "
                               "round trips but in no session row"}
                    for piece in self._projection(experiment).pieces
                    if not self.calendar.is_session(piece.session_date)]
        incidents = [i for i in self.store.incidents()
                     if exp_id in i["key"] or i["kind"].startswith("BENCHMARK")]
        return build_report(self._inputs(
            experiment,
            self.store.fetch("equity_daily", {"experiment_id": exp_id}),
            self.store.fetch("equity_adjustments", {"experiment_id": exp_id}),
            self.store.fetch("round_trips", {"experiment_id": exp_id}),
            self.store.fetch("ai_costs", {"experiment_id": exp_id}),
            self.store.fetch("simulated_books", {"experiment_id": exp_id}),
            incidents, warnings))

    def _inputs(self, experiment, rows, adjustments, trips, ai_costs, simulated, incidents, warnings):
        return ReportInputs(
            experiment=experiment, rows=rows, adjustments=adjustments, trips=trips,
            spy_closes=self.book.closes(), spy_version=self.book.current_version(),
            spy_provider=self.book.provider(), ai_costs=ai_costs, simulated=simulated, incidents=incidents,
            warnings=warnings, outbox=None if self.outbox is None else self.outbox.counts(),
            calendar=self.calendar)
