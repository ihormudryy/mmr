"""``ScoreboardService``: refresh the derived tables, build the report, verify (Plan 5 Tasks 5-6)."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Callable, Optional

from trader.scoreboard.ports import session_date_et
from trader.scoreboard.report import ReportInputs, build_report
from trader.scoreboard.round_trips import project_round_trips
from trader.scoreboard.session_ledger import SessionFacts, experiment_fills
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

    def refresh(self, experiment_id: Optional[str] = None) -> None:
        """Ruling 13: rebuild round_trips of the experiment (default: latest) and book late commissions."""
        experiment = self.resolve(experiment_id)
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

    def trips(self, experiment_id: str) -> dict:
        """Ruling 19: per-trip identity and quantities from the stored projection, ordered by opened_at."""
        experiment = self.experiments.get(experiment_id)
        if experiment is None:
            return {"experiment_id": experiment_id, "error_code": EXPERIMENT_NOT_FOUND, "generation": None,
                    "trips": None}
        rows = self.store.fetch("round_trips", {"experiment_id": experiment_id})
        return {"experiment_id": experiment_id, "generation": self._broker_generation(),
                "trips": [_trip_view(row) for row in rows]}

    def _broker_generation(self) -> Optional[int]:
        """The newest promoted broker generation the projection could have seen; None when unknown."""
        try:
            row = self.db.execute("SELECT MAX(generation_id) FROM broker_sync_generations WHERE status = 'promoted'",
                                  fetch="one")
        except Exception:
            logger.exception("broker generation unreadable for the trips read")
            return None
        return None if row is None or row[0] is None else int(row[0])

    def verify(self, experiment_id: Optional[str] = None) -> dict:
        """Rebuild every derived number from its stored inputs and compare; never writes (ruling 11)."""
        return _verify(self, experiment_id)

    def _inputs(self, experiment, rows, adjustments, trips, ai_costs, simulated, incidents, warnings):
        return ReportInputs(
            experiment=experiment, rows=rows, adjustments=adjustments, trips=trips,
            spy_closes=self.book.closes(), spy_version=self.book.current_version(),
            spy_provider=self.book.provider(), ai_costs=ai_costs, simulated=simulated, incidents=incidents,
            warnings=warnings, outbox=None if self.outbox is None else self.outbox.counts(),
            calendar=self.calendar)


def _trip_view(row: dict) -> dict:
    return {"round_trip_id": row["round_trip_id"], "conid": int(row["conid"]), "symbol": row["symbol"],
            "direction": row["direction"], "opened_at": row["opened_at"].isoformat(),
            "closed_at": None if row["closed_at"] is None else row["closed_at"].isoformat(),
            "opened_quantity": row["entry_qty"], "closed_quantity": row["exit_qty"],
            "exec_ids": json.loads(row["exec_ids"]), "net_pnl_usd": row["net_pnl_usd"],
            "decision_id": row["decision_id"], "strategy_ref": row["strategy_version"], "state": row["status"]}


ATTRIBUTION_COLUMNS = ("decision_id", "decider", "strategy_version", "policy_revision", "style", "links_digest")
_IGNORED = frozenset({"experiment_id", "account_id"})


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) <= 1e-6
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return a.astimezone(dt.timezone.utc) == b.astimezone(dt.timezone.utc)
    return a == b


def _differences(stored: dict, recomputed: dict, columns) -> dict:
    return {name: {"stored": stored.get(name), "recomputed": recomputed.get(name)}
            for name in columns if not _same(stored.get(name), recomputed.get(name))}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


class _Verifier:
    """Recomputes from broker_fills and the decision links; never reads a derived row as truth, never writes."""

    def __init__(self, service: "ScoreboardService", experiment: Any):
        self.service = service
        self.experiment = experiment
        self.mismatches: list[dict] = []

    def add(self, check: str, table: str, key: str, **detail: Any) -> None:
        self.mismatches.append({"check": check, "table": table, "key": key, **_jsonable(detail)})

    def round_trips(self, projection) -> None:
        store, exp = self.service.store, self.experiment
        recomputed = {t.round_trip_id: store.prepare("round_trips", t.as_row(exp.experiment_id, exp.account_id))
                      for t in projection.trips}
        stored = {r["round_trip_id"]: r for r in store.fetch("round_trips", {"experiment_id": exp.experiment_id})}
        for trip_id in sorted(set(recomputed) - set(stored)):
            self.add("ROUND_TRIP_MISSING", "round_trips", trip_id)
        for trip_id in sorted(set(stored) - set(recomputed)):
            self.add("ROUND_TRIP_EXTRA", "round_trips", trip_id)
        for trip_id in sorted(set(stored) & set(recomputed)):
            columns = [c for c in recomputed[trip_id] if c not in _IGNORED]
            money = _differences(stored[trip_id], recomputed[trip_id],
                                 [c for c in columns if c not in ATTRIBUTION_COLUMNS])
            if money:
                self.add("ROUND_TRIP_MISMATCH", "round_trips", trip_id, columns=money)
            links = _differences(stored[trip_id], recomputed[trip_id], ATTRIBUTION_COLUMNS)
            if links:
                self.add("ATTRIBUTION_CHANGED", "round_trips", trip_id, columns=links)

    def sessions(self, projection, facts, rows, adjustments) -> None:
        for row in rows:
            key = f"{row['experiment_id']}|{row['session_date']}"
            current = SessionFacts(projection, facts, row["session_date"])
            recomputed = {"fills_digest": current.fills_digest, "fill_count": current.fill_count,
                          "trade_count": current.trade_count}
            if row["realized_pnl_usd"] is not None:
                recomputed["realized_pnl_usd"] = float(current.realized)
            changed = _differences(row, recomputed, recomputed)
            if changed:
                self.add("SESSION_FILLS_CHANGED", "equity_daily", key, columns=changed)
            self.commissions(row, key, current.commissions, adjustments)

    def commissions(self, row, key, current, adjustments) -> None:
        stored = json.loads(row["commission_json"])
        for exec_id, fee in sorted(current.items()):
            if exec_id not in stored or fee is None:
                continue
            booked = (stored[exec_id] or 0.0) + sum(
                a["amount_usd"] for a in adjustments
                if a["session_date"] == row["session_date"] and a["exec_id"] == exec_id)
            if abs(float(fee) - booked) > 1e-9:
                self.add("COMMISSION_MISMATCH", "equity_daily", f"{key}|{exec_id}",
                         stored=booked, recomputed=float(fee))


def _verify(service: "ScoreboardService", experiment_id: Optional[str]) -> dict:
    experiment = service.resolve(experiment_id)
    if experiment is None and experiment_id is not None:
        return {"ok": False, "error_code": EXPERIMENT_NOT_FOUND, "experiment_id": experiment_id}
    mismatches = service.store.verify_seals()
    checked = {"seals": service.store.seal_count(), "round_trips": 0, "sessions": 0}
    incidents: list = []
    if experiment is not None:
        verifier = _Verifier(service, experiment)
        facts = experiment_fills(service.db, experiment)
        projection = project_round_trips(facts, links_for=service.links.links_for_order_ref,
                                         account_id=experiment.account_id)
        rows = service.store.fetch("equity_daily", {"experiment_id": experiment.experiment_id})
        verifier.round_trips(projection)
        verifier.sessions(projection, facts, rows,
                          service.store.fetch("equity_adjustments", {"experiment_id": experiment.experiment_id}))
        mismatches += verifier.mismatches
        checked.update(round_trips=len(projection.trips), sessions=len(rows))
        incidents = [_jsonable(i) for i in service.store.incidents() if experiment.experiment_id in i["key"]]
    scope = "-" if experiment is None else experiment.experiment_id
    for mismatch in mismatches:
        # Spec 5.2: a mismatch is an incident. Only the incident table is written, never the books.
        service.store.record_incident("VERIFY_MISMATCH", f"{scope}:{mismatch['check']}:{mismatch['table']}:"
                                      f"{mismatch['key']}", json.dumps(mismatch, sort_keys=True, default=str))
    return {"ok": not mismatches, "checked": checked, "mismatches": mismatches, "incidents": incidents}
