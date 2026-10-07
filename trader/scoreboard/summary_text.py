"""The daily Telegram summary (Plan 5 rulings 5, 17, 18): plain text, PAPER and the end state first."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

from trader.scoreboard.ports import session_date_et
from trader.scoreboard.telegram_outbox import fit_text

logger = logging.getLogger(__name__)

KIND = "daily_summary"
SUMMARY_STATES = ("FLAT", "KILLED", "FAILED_SAFE")
MAX_LISTED_INCIDENTS = 50


def summary_event_id(experiment_id: str, session_date: dt.date) -> str:
    return f"{KIND}:{experiment_id}:{session_date.isoformat()}"


def _known(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _money(value: Any) -> str:
    if not _known(value):
        return "-"
    return ("-$" if value < 0 else "$") + f"{abs(value):,.2f}"


def _pct(value: Any) -> str:
    return f"{value:.2f}%" if _known(value) else "-"


def _plain(value: Any) -> str:
    return "-" if value is None else str(value)


def format_daily_summary(report: dict, session_date: dt.date) -> str:
    """Refuses a date without a row and an UNKNOWN row: those send no summary."""
    session = next((s for s in report.get("sessions", []) if s["date"] == session_date.isoformat()), None)
    if session is None:
        raise ValueError(f"no session row for {session_date}")
    if session["end_state"] not in SUMMARY_STATES:
        raise ValueError(f"no summary for a session that ended {session['end_state']}")
    experiment = report["experiment"]
    account, bench = report["account"], report["benchmarks"]
    spy = bench["spy"]
    ai_cost = "unavailable" if bench["ai_costs_status"] == "UNAVAILABLE" else _money(bench["ai_cost_usd"])
    lines = [
        f"PAPER — {session['end_state']}",
        f"Session {session['date']}, experiment {experiment['id']} ({experiment['state']})",
        f"Session: return {_pct(session['return_pct'])}, end value {_money(session['end_nlv_usd'])}, "
        f"realized {_money(session['realized_pnl_usd'])}, fees {_money(session['commissions_usd'])}, "
        f"trades {_plain(session['trade_count'])}, open positions {_plain(session['open_positions'])}",
        f"Experiment: return {_pct(account['return_pct'])}, P&L {_money(account['pnl_usd'])}, "
        f"{account['sessions']} sessions",
        f"SPY: {_pct(spy['return_pct'])} ({spy['label']}); vs SPY "
        + (f"{bench['vs_spy_pp']:+.2f} pp" if _known(bench["vs_spy_pp"]) else "-"),
        f"End-of-day drawdown: {_pct(account['eod_drawdown_pct'])} ({account['eod_drawdown_label']})",
        f"AI cost: {ai_cost}; P&L minus AI cost: {_money(bench['pnl_minus_ai_cost_usd'])}",
        f"Incidents: {len(report['incidents'])}",
    ]
    lines += [f"- {i['kind']} {i['key']}: {i['detail']}" for i in report["incidents"][:MAX_LISTED_INCIDENTS]]
    lines.append(report["disclaimer"])
    return fit_text("\n".join(lines), summary_event_id(experiment["id"], session_date))


class DailySummaryProducer:
    """Enqueues one summary per FLAT, KILLED or FAILED_SAFE row (ledger ``on_row_written``)."""

    def __init__(self, service: Any, outbox: Any, now: Optional[Callable[[], dt.datetime]] = None):
        self._service = service
        self._outbox = outbox
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def on_session_row(self, experiment_id: str, session_date: dt.date) -> bool:
        rows = self._service.store.fetch("equity_daily", {"experiment_id": experiment_id,
                                                           "session_date": session_date})
        if not rows or rows[0]["session_end_state"] not in SUMMARY_STATES:
            return False
        event_id = summary_event_id(experiment_id, session_date)
        if self._outbox.row(event_id) is not None:
            return False
        self._service.refresh(experiment_id)
        text = format_daily_summary(self._service.report(experiment_id), session_date)
        return self._outbox.enqueue(event_id, KIND, text)

    def catch_up(self) -> bool:
        """After a crash between the row and the enqueue: the newest row of the latest experiment, if it is
        from the last completed session or today. Older rows are never sent (no flood when Telegram is
        enabled later)."""
        experiment = self._service.experiments.latest()
        if experiment is None:
            return False
        rows = self._service.store.fetch("equity_daily", {"experiment_id": experiment.experiment_id})
        if not rows:
            return False
        newest = rows[-1]
        today = session_date_et(self._now())
        if newest["session_date"] < self._service.calendar.previous_session(today):
            return False
        return self.on_session_row(experiment.experiment_id, newest["session_date"])
