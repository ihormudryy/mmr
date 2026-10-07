"""The experiment kill line: detection, the kill flatten and its notices (SP1 Plan 4 Tasks 4-5).

``KillLineMonitor.tick()`` runs every 5 s on the liquidation worker. It reads
every fenced broker capture (K1) and never forces a broker sync, so it sees
IB account updates: about three minutes for ``NetLiquidation``. That is
accepted for paper only.

A hit writes ``KILLED`` in its own transaction before any order (spec 5.5
step 1), then asks ``SessionController.flatten_account_now`` for Plan 1's
account flatten (K2, K3). A capture that cannot be trusted is unknown (K7):
it never kills and never changes the state, but entries are refused
``KILL_LINE_UNKNOWN`` while the kill line is set; a long streak pauses the
experiment (K23) and never flattens.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Callable, Optional

from trader.automation.experiments import ExperimentRecord, ExperimentRefused, ExperimentStore
from trader.automation.kill_line import KillEvaluation, KillLineInputError, effective_kill_line, evaluate_kill_line

logger = logging.getLogger(__name__)

KILL_MONITOR_PRINCIPAL = "kill_monitor"
DETECTION_NOTE = "IB account updates, about 3 minutes; paper only"


def kill_root_id(experiment_id: str, kill_seq: int, kill_round: int) -> str:
    """K4: colon-free, one root per flatten round of one kill."""
    return f"experiment-kill-{experiment_id}-{kill_seq}-{kill_round}"


def record_incident(journal: Any, *, kind: str, account_id: str, detail: str, event_id: str,
                    now: dt.datetime) -> None:
    """One durable journal event; a repeated ``event_id`` is an idempotent replay."""
    from trader.domain.events import DomainMutation
    journal.mutate(
        journal.connect(),
        DomainMutation(event_type=kind, entity_type="incident", entity_id=event_id, operation="upsert",
                       account_id=account_id, source="trader_service", source_timestamp=now,
                       correlation_id=None, payload={"kind": kind, "detail": detail}),
        lambda _conn, _revision: None,
        event_id=event_id,
    )


class KillLineMonitor:
    def __init__(self, *, store: ExperimentStore, broker: Any, session: Any, liquidation: Any, config: Any,
                 account_id: str, now: Callable[[], dt.datetime], journal: Any = None,
                 stale_after_seconds: float = 30.0, flatten_seconds: float = 300.0):
        self._store = store
        self._broker = broker
        self._session = session
        self._liquidation = liquidation
        self._config = config
        self._account_id = account_id
        self._now = now
        self._journal = journal
        self._stale_after = dt.timedelta(seconds=stale_after_seconds)
        self._flatten_seconds = flatten_seconds
        self._recovered = False
        self._last_generation: Optional[int] = None
        self._last_good_at: Optional[dt.datetime] = None
        self._last_evaluation: Optional[KillEvaluation] = None
        self._unknown_since: Optional[dt.datetime] = None
        self._unknown_reason: Optional[str] = None
        self._outage_handled = False
        self._alerts: Any = None
        self._session_end: Any = None

    def attach_notices(self, *, alerts: Any = None, session_end: Any = None) -> None:
        """Plan 5 calls this with its outbox and session ledger; until then both are None (K18)."""
        self._alerts = alerts
        self._session_end = session_end

    # -- public --------------------------------------------------------------

    @property
    def last_evaluation(self) -> Optional[KillEvaluation]:
        return self._last_evaluation

    @property
    def recovered(self) -> bool:
        return self._recovered

    def last_evaluation_view(self) -> Optional[dict]:
        ev = self._last_evaluation
        if ev is None:
            return None
        return {"hit": ev.hit, "reference": ev.reference, "net_liquidation": ev.net_liquidation,
                "drawdown_pct": ev.drawdown_pct,
                "at": None if self._last_good_at is None else self._last_good_at.isoformat(),
                "generation_id": self._last_generation}

    def recover(self) -> None:
        """Before readiness: resume a kill in progress, then allow entries again (K5)."""
        record = self._store.active()
        if record is not None and record.state == "KILLED":
            self._drive_kill(record)
        self._recovered = True

    def tick(self) -> None:
        record = self._store.active()
        if record is None:
            return
        if record.state == "KILLED":
            self._drive_kill(record)
            return
        snapshot = self._evaluable_capture()
        if snapshot is None:
            self._on_unknown(record)
            return
        self._end_unknown_streak()
        peak = self._store.raise_peak(record.experiment_id, snapshot.net_liquidation)
        line = effective_kill_line(record, self._config)
        self._last_generation = snapshot.generation_id
        self._last_good_at = self._now()
        if line is None:
            self._last_evaluation = None
            return
        try:
            evaluation = evaluate_kill_line(line, anchor=record.kill_anchor_net_liquidation, peak=peak,
                                            net_liquidation=snapshot.net_liquidation)
        except KillLineInputError:
            logger.exception("experiment %s: kill line inputs invalid; treated as unknown", record.experiment_id)
            self._last_good_at = None
            return
        self._last_evaluation = evaluation
        if evaluation.hit:
            self._kill(record, snapshot, evaluation)

    def entry_block(self, record: ExperimentRecord) -> Optional[str]:
        """K7/K19: why an ENTER is refused now, or None."""
        if record.state != "ARMED":
            return None                      # admission refuses by state
        if not self._recovered:
            return "EXPERIMENT_MONITOR_NOT_READY"
        if effective_kill_line(record, self._config) is None:
            return None
        if self._last_good_at is None or self._now() - self._last_good_at > self._stale_after:
            return "KILL_LINE_UNKNOWN"
        return None

    # -- detection -----------------------------------------------------------

    def _evaluable_capture(self) -> Any:
        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception as exc:
            self._unknown_reason = str(getattr(exc, "code", None) or type(exc).__name__)
            return None
        nlv = getattr(snapshot, "net_liquidation", None)
        generation = getattr(snapshot, "generation_id", None)
        if getattr(snapshot, "account_id", None) != self._account_id:
            self._unknown_reason = "ACCOUNT_MISMATCH"
        elif getattr(snapshot, "account_mode", None) != "paper":
            self._unknown_reason = "ACCOUNT_NOT_PAPER"
        elif type(nlv) not in (int, float) or not math.isfinite(nlv) or nlv <= 0:
            self._unknown_reason = "INVALID_NET_LIQUIDATION"
        elif type(generation) is not int:
            self._unknown_reason = "INVALID_GENERATION"
        elif self._last_generation is not None and generation < self._last_generation:
            self._unknown_reason = "GENERATION_REGRESSION"
        else:
            return snapshot
        return None

    def _on_unknown(self, record: ExperimentRecord) -> None:
        now = self._now()
        if self._unknown_since is None:
            self._unknown_since = now
            self._outage_handled = False
            logger.error("experiment %s: kill line unknown (%s)", record.experiment_id, self._unknown_reason)
            self._incident(record, now)
        if self._outage_handled:
            return
        if (now - self._unknown_since).total_seconds() >= self._config.broker_outage_pause_seconds:
            self._outage(record, now)

    def _end_unknown_streak(self) -> None:
        if self._unknown_since is not None:
            logger.warning("kill line known again after an unknown streak since %s", self._unknown_since)
        self._unknown_since = None
        self._unknown_reason = None
        self._outage_handled = False

    def _incident(self, record: ExperimentRecord, now: dt.datetime) -> None:
        if self._journal is None:
            return
        try:
            record_incident(
                self._journal, kind="experiment.kill_line_unknown", account_id=self._account_id,
                detail=f"broker capture unknown: {self._unknown_reason}",
                event_id=f"experiment.kill_line_unknown:{record.experiment_id}:{now.isoformat()}", now=now)
        except Exception:
            logger.exception("experiment %s: kill_line_unknown incident not written", record.experiment_id)

    def _outage(self, record: ExperimentRecord, now: dt.datetime) -> None:
        """K23: a long outage pauses an ARMED experiment durably. Never a flatten, never a kill.

        An operator-paused experiment is not touched; the alert still goes out once per streak.
        """
        seconds = int((now - self._unknown_since).total_seconds())
        target = record
        if record.state == "ARMED":
            try:
                target = self._store.transition(
                    record.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                    principal=KILL_MONITOR_PRINCIPAL, command_id=None,
                    reason=f"broker data unavailable for {seconds} s",
                    changes={"pause_cause": "BROKER_DATA_OUTAGE",
                             "pause_generation_id": (self._last_generation if self._last_generation is not None
                                                     else record.start_generation_id)})
            except ExperimentRefused as exc:
                logger.warning("experiment %s: outage pause not written (%s)", record.experiment_id, exc.code)
                return
            logger.error("experiment %s PAUSED: broker data unavailable for %d s", record.experiment_id, seconds)
        text = (f"PAPER experiment {target.experiment_id}: broker data unavailable for {seconds} s "
                f"({self._unknown_reason}). Entries are blocked"
                + ("; the experiment is PAUSED. " if target.state == "PAUSED" else ". ")
                + "Nothing was flattened. Resume needs fresh broker evidence and an operator.")
        self._outage_handled = self._send_alert(f"broker_outage:{target.experiment_id}:{target.revision}",
                                                "broker_outage", text) is not None

    def _send_alert(self, event_id: str, kind: str, text: str) -> Optional[str]:
        """Returns ENQUEUED, NO_OUTBOX, or None when the outbox failed (retried next tick)."""
        if self._alerts is None:
            logger.warning("no alert outbox: %s not sent (%s)", event_id, text)
            return "NO_OUTBOX"
        try:
            self._alerts.enqueue(event_id, kind, text)
        except Exception:
            logger.exception("alert %s could not be enqueued; retried next tick", event_id)
            return None
        return "ENQUEUED"

    # -- the kill ------------------------------------------------------------

    def _kill(self, record: ExperimentRecord, snapshot: Any, evaluation: KillEvaluation) -> None:
        seq = record.kill_seq + 1
        root = kill_root_id(record.experiment_id, seq, 0)
        try:
            killed = self._store.transition(        # 1. KILLED is durable before any order
                record.experiment_id, expected=frozenset({"ARMED", "PAUSED"}), to="KILLED",
                principal=KILL_MONITOR_PRINCIPAL, command_id=None, reason="kill line hit",
                changes={"killed_at": self._now(), "kill_seq": seq, "kill_round": 0, "kill_root_id": root,
                         "kill_flatten_root": None, "kill_net_liquidation": evaluation.net_liquidation,
                         "kill_observed_drawdown_pct": evaluation.drawdown_pct,
                         "kill_generation_id": snapshot.generation_id, "kill_flat_state": "PENDING",
                         "kill_alert_state": "PENDING", "kill_session_end_state": "PENDING"})
        except ExperimentRefused as exc:
            # A concurrent pause or stop won the compare-and-set; the next tick decides again.
            logger.warning("experiment %s: kill not written (%s); retried next tick", record.experiment_id, exc.code)
            return
        logger.critical("experiment %s KILLED: drawdown %.2f%% from %.2f (net liquidation %.2f)",
                        killed.experiment_id, evaluation.drawdown_pct, evaluation.reference,
                        evaluation.net_liquidation)
        self._drive_kill(killed)

    def _drive_kill(self, record: ExperimentRecord) -> None:
        if record.kill_flatten_root is None:
            deadline = self._now() + dt.timedelta(seconds=self._flatten_seconds)
            root = self._session.flatten_account_now(record.kill_root_id, deadline)
            self._store.update_kill_progress(record.experiment_id, expected_state="KILLED",
                                             changes={"kill_flatten_root": root})
