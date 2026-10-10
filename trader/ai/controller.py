"""The ai controller: one async service with a durable journal (SP2 spec 3, 4, 5.2, 5.5, 9; Plan 5 Rulings 7-9, 15).

Deterministic code only: schedule, intake, submission, reconciliation and
reporting. Trading judgments come from the DecisionEngine. Each loop is its own
task and model work runs in spawned tasks, so a slow model call never blocks
receipts, reconciliation or recovery.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from trader.ai.engine import (
    ALLOWED_ACTIONS, MATCHED_ENTRY_BASELINE, EngineResult, EntryCycleContext, ExperimentView, ModelWork,
    PositionCycleContext, SignalContext, SignalOpportunity, owned_positions_from_trips,
)
from trader.ai.ids import derive_decision_id
from trader.ai.outbox import register_context_in_tx
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.schedule import ENTRY, POSITION, Slot
from trader.ai.store import to_utc

logger = logging.getLogger(__name__)

WAIT = "WAIT"
EXIT_WAIT_OVERRUN = dt.timedelta(minutes=10)      # backstop past an exit's own wait_until (PR #86 4211394337)
EXIT_WAIT_STUCK = "EXIT_WAIT_STUCK"
EXIT_WAIT_ALERT_EVERY = dt.timedelta(hours=1)
NOTHING_TO_CLOSE_YET = frozenset({"POSITION_NOT_OWNED", "NOT_A_REDUCTION"})
EXIT_REOPEN_WAIT = dt.timedelta(hours=1)          # the engine sets the real backstop on its next judgment
SLOT_POLL_SECONDS = 1.0
CYCLE_SOURCE = {ENTRY: "entry_cycle", POSITION: "position_cycle"}
TRADER_AWAY = (RpcNotSent, RpcOutcomeUnknown, RpcRefused)


class SlotDeadlinePassed(Exception):
    """A cycle's result arrived or was about to persist after its slot deadline: nothing is persisted."""


class ExperimentWatch:
    """The trader's experiment as last read. Unknown (trader away, bad reply) is never 'no experiment'."""

    def __init__(self, supervisor: Any):
        self._supervisor = supervisor
        self.view: Optional[ExperimentView] = None
        self.known = False

    async def refresh(self) -> None:
        try:
            reply = await self._supervisor.call("get_experiment", {})
            self.view = ExperimentView.from_reply(reply)
        except TRADER_AWAY as exc:
            self.known = False
            logger.warning("experiment view unavailable: %s", exc.code)
            return
        except ValueError as exc:
            self.known = False
            logger.error("experiment view malformed: %s", exc)
            return
        self.known = True

    def state(self) -> Optional[str]:
        return self.view.state if self.known and self.view is not None else None


def validate_result(source_kind: str, result: Any) -> Optional[str]:
    """Ruling 15: the whole result is refused if one part does not fit its source."""
    if not isinstance(result, EngineResult):
        return "RESULT_TYPE"
    keys = [decision.action_key for decision in result.decisions]
    if len(keys) != len(set(keys)):
        return "DUPLICATE_ACTION_KEY"
    if any(decision.action not in ALLOWED_ACTIONS[source_kind] for decision in result.decisions):
        return "ACTION_NOT_ALLOWED_HERE"
    if any(b.linked_action_key is not None and b.linked_action_key not in keys for b in result.baselines):
        return "BASELINE_LINK_UNKNOWN"
    if result.wait_until is not None and (source_kind != "exit_signal" or result.decisions or result.baselines):
        return "WAIT_NOT_ALLOWED_HERE"
    return None


def _close_attempt(action_key: str) -> int:
    suffix = action_key.split(":")[2] if action_key.count(":") == 2 else "r1"
    return int(suffix[1:]) if suffix.startswith("r") and suffix[1:].isdigit() else 1


def _stop_when_renewals_die(task: asyncio.Task, stop: asyncio.Event) -> None:
    """Without renewals the epoch lapses and nothing is sent, while the heartbeat stays fresh.
    Stop the service loudly instead, so it restarts as a new holder."""
    if task.cancelled() or stop.is_set():
        return
    error = task.exception()
    logger.error("controller epoch renewal stopped (%s); the ai service stops so it can restart",
                 getattr(error, "code", None) or repr(error))
    stop.set()


def _write_atomically(path: str, text: str) -> None:
    temporary = Path(path).with_suffix(".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


class AiController:
    def __init__(self, *, config: Any, store: Any, clock: Any, supervisor: Any, leadership: Any,
                 watch: ExperimentWatch, submitter: Any, outbox: Any, intake: Any, slots: Any, engine: Any,
                 gateway: Any, cap_sync: Any = None, research: Any = None):
        self._config, self._store, self._clock = config, store, clock
        self._supervisor, self._leadership, self._watch = supervisor, leadership, watch
        self._submitter, self._outbox, self._intake = submitter, outbox, intake
        self._slots, self._engine, self._gateway = slots, engine, gateway
        self._cap_sync = cap_sync                      # BudgetCapSync (Ruling 19); None in unit tests
        self._research = research                      # ResearchCycle (SP2c Plan 4); None when research is off
        self._ttl = dt.timedelta(seconds=config.decision_ttl_seconds)
        self._tasks: set[asyncio.Task] = set()
        self._opportunity_tasks: dict[str, asyncio.Task] = {}
        self._cycle_tasks: dict[str, asyncio.Task] = {}

    # -- lifecycle ---------------------------------------------------------------------------------
    def _spawn(self, coroutine: Awaitable[None]) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def start(self) -> None:
        """After a restart: possibly-sent work is reconciled, unfinished cycles are never replayed (spec 9)."""
        await self._submitter.recover()
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_cycles SET state = 'FAILED', reason = 'PROCESS_RESTARTED', finished_at = ? "
            "WHERE state = 'RUNNING'", [now]))
        await self._watch.refresh()
        if self._cap_sync is not None:
            await self._cap_sync.sync()                # a failure is logged; the gateway stays closed (Ruling 19)
        if self._research is not None:
            await self._research.recover()             # the gateway already turned STARTED attempts into UNKNOWN

    async def refresh_experiment(self) -> None:
        await self._watch.refresh()

    # -- model work identity -------------------------------------------------------------------------
    async def _open_work(self, source_id: str, served_kind: str, experiment: ExperimentView) -> ModelWork:
        async def register(context_key: str, kind: str, served_id: str) -> None:
            now = self._clock.now()
            await self._store.atransaction(lambda conn: register_context_in_tx(
                conn, context_key=context_key, experiment_id=experiment.experiment_id, served_kind=kind,
                served_id=served_id, now=now))
        async def record_baselines(baselines: tuple) -> None:
            if any(b.linked_action_key is not None or b.baseline_id == MATCHED_ENTRY_BASELINE for b in baselines):
                raise ValueError("only unlinked baselines are recorded ahead of their result")
            now = self._clock.now()

            def work(conn: Any) -> None:
                for baseline in baselines:
                    self._outbox.enqueue_simulated_in_tx(conn, experiment_id=experiment.experiment_id,
                                                         baseline=baseline, wait_for_decision_id=None, now=now)
            await self._store.atransaction(work)
        await register(source_id, served_kind, source_id)
        return ModelWork(context_key=source_id, served_kind=served_kind, served_id=source_id, source_id=source_id,
                         experiment_id=experiment.experiment_id, gateway=self._gateway,
                         deadline=self._gateway.new_deadline(source_id), register=register,
                         record_baselines=record_baselines)

    async def _commit(self, source_kind: str, source_id: str, experiment: ExperimentView, result: Any,
                      finish: Callable[[Any, bool, Optional[str]], None],
                      deadline: Optional[dt.datetime] = None) -> None:
        """Decisions, baselines and the opportunity or cycle state in one transaction. With a slot
        ``deadline``, the transaction persists nothing once it has passed (Ruling 8)."""
        problem = validate_result(source_kind, result)
        if problem is not None:
            logger.error("engine result for %s refused: %s", source_id, problem)
            await self._store.atransaction(lambda conn: finish(conn, False, problem))
            return
        epoch = self._leadership.last_epoch
        if epoch is None:
            raise RuntimeError("model work ran without ever holding a controller epoch")
        now = self._clock.now()
        expires_at = now + self._ttl

        def work(conn: Any) -> None:
            if deadline is not None and self._clock.now() >= deadline:
                raise SlotDeadlinePassed(source_id)
            for decision in result.decisions:
                self._submitter.insert_in_tx(conn, source_kind=source_kind, source_id=source_id, decision=decision,
                                             expires_at=expires_at, epoch=epoch, now=now)
            for baseline in result.baselines:
                if baseline.baseline_id == MATCHED_ENTRY_BASELINE:
                    wait_for = baseline.opportunity_id          # its own close (Ruling 13)
                else:
                    wait_for = (None if baseline.linked_action_key is None
                                else derive_decision_id(source_id, baseline.linked_action_key))
                self._outbox.enqueue_simulated_in_tx(conn, experiment_id=experiment.experiment_id,
                                                     baseline=baseline, wait_for_decision_id=wait_for, now=now)
            finish(conn, True, result.note or None)
        await self._store.atransaction(work)

    # -- signals -------------------------------------------------------------------------------------
    async def tick_signals(self) -> None:
        if self._leadership.current_epoch() is None:
            return
        await self._intake.poll()
        await self._intake.expire_stale()
        await self._reopen_refused_exits()
        await self.dispatch_opportunities()

    async def _reopen_refused_exits(self) -> None:
        """A strategy CLOSE refused because nothing of ours was held (yet) puts its exit back on the wait path:
        the engine closes only once shares are proven held, else it ends unheld (PR #86 4212667433).

        Only the exit's latest attempt can reopen it, and only once (PR #86 4212958297): a refusal that a
        later attempt superseded, pending, accepted or final, never reopens anything. The attempts are the
        durable ai_submissions rows; ``reopened_for`` marks the latest refusal already handled."""
        codes = sorted(NOTHING_TO_CLOSE_YET)
        rows = await self._store.aquery(
            "SELECT s.source_id, s.decision_id, s.error_code FROM ai_submissions s "
            "JOIN ai_opportunities o ON o.opportunity_id = s.source_id "
            "WHERE s.source_kind = 'exit_signal' AND s.action = 'CLOSE' AND s.receipt_state = 'REJECTED' "
            f"AND s.error_code IN ({', '.join('?' for _ in codes)}) AND o.state = 'DECIDED' "
            "AND NOT EXISTS (SELECT 1 FROM ai_exit_waits w WHERE w.opportunity_id = s.source_id "
            "AND w.reopened_for = s.decision_id)", codes)
        for opportunity_id, decision_id, code in rows:
            if decision_id != await self._latest_close_attempt(opportunity_id):
                continue                                   # superseded by a later attempt of the same exit
            logger.warning("exit %s: close %s refused %s; it waits for held shares again", opportunity_id,
                           decision_id, code)
            await self._intake.reopen(opportunity_id, decision_id, self._clock.now() + EXIT_REOPEN_WAIT,
                                      f"EXIT_CLOSE_REFUSED_{code}")

    async def _latest_close_attempt(self, opportunity_id: str) -> str:
        """The decision id of the exit's highest attempt: ``close:<conid>`` is 1, ``close:<conid>:r<n>`` is n."""
        rows = await self._store.aquery("SELECT decision_id, action_key FROM ai_submissions "
                                        "WHERE source_id = ? AND action = 'CLOSE'", [opportunity_id])
        return max(rows, key=lambda row: _close_attempt(row[1]))[0]

    async def dispatch_opportunities(self) -> None:
        now = self._clock.now()
        waits = await self._intake.waits()
        for opportunity, _state in await self._intake.open_opportunities():
            if opportunity.opportunity_id in self._opportunity_tasks:
                continue
            wait = waits.get(opportunity.opportunity_id)
            if wait is not None:
                await self._escalate_if_stuck(opportunity.opportunity_id, *wait, now)
            verdict = self._signal_verdict(opportunity, now, None if wait is None else wait[0])
            if verdict == WAIT:
                continue
            if verdict is not None:
                await self._intake.mark(opportunity.opportunity_id, "MISSED", verdict)
                continue
            await self._intake.mark(opportunity.opportunity_id, "IN_PROGRESS", None)
            task = self._spawn(self._judge(opportunity))
            self._opportunity_tasks[opportunity.opportunity_id] = task
            task.add_done_callback(lambda _t, key=opportunity.opportunity_id: self._opportunity_tasks.pop(key, None))

    async def _escalate_if_stuck(self, opportunity_id: str, wait_until: dt.datetime,
                                 last_alert: Optional[dt.datetime], now: dt.datetime) -> None:
        """Past its backstop a waiting exit is an incident: one ERROR per hour, counted in the heartbeat. It
        stays pending until the broker proves its entry ended or filled (PR #86 4212341131)."""
        if now <= wait_until + EXIT_WAIT_OVERRUN:
            return
        if last_alert is not None and now - last_alert < EXIT_WAIT_ALERT_EVERY:
            return
        logger.error("%s: exit signal %s still waits for its entry (backstop %s passed); it stays pending until "
                     "the broker proves the entry ended or filled. Check the broker and the trader.",
                     EXIT_WAIT_STUCK, opportunity_id, wait_until.isoformat())
        await self._intake.record_wait_alert(opportunity_id)

    def _signal_verdict(self, opportunity: SignalOpportunity, now: dt.datetime,
                        waiting_until: Optional[dt.datetime] = None) -> Optional[str]:
        stale = None if waiting_until is not None else self._intake.stale_reason(opportunity, now)
        if stale is not None:
            return stale
        if self._leadership.current_epoch() is None or not self._watch.known:
            return WAIT
        experiment = self._watch.view
        if experiment is None:
            return "NO_EXPERIMENT"
        if opportunity.action == "BUY":
            if experiment.state != "ARMED":
                return f"EXPERIMENT_{experiment.state}"
            if experiment.entry_block:
                return "ENTRY_BLOCKED"
            if not self._slots.entry_window_open(now):
                return "OUTSIDE_ENTRY_WINDOW"
            return None
        return "EXPERIMENT_STOPPED" if experiment.state == "STOPPED" else None

    async def _judge(self, opportunity: SignalOpportunity) -> None:
        experiment = self._watch.view
        buy = opportunity.action == "BUY"
        hook = self._engine.on_entry_signal if buy else self._engine.on_exit_signal

        def finish(conn: Any, ok: bool, reason: Optional[str]) -> None:
            self._intake.finish_in_tx(conn, opportunity.opportunity_id, ok, reason)
        try:
            work = await self._open_work(opportunity.opportunity_id, "signal", experiment)
            result = await hook(SignalContext(self._clock.now(), experiment, opportunity, work))
            if not buy and isinstance(result, EngineResult) and result.wait_until is not None \
                    and validate_result("exit_signal", result) is None:
                await self._intake.wait(opportunity.opportunity_id, result.wait_until, result.note or "EXIT_WAITING")
                return                                     # judged again on the next tick, never lost
            await self._commit("entry_signal" if buy else "exit_signal", opportunity.opportunity_id, experiment,
                               result, finish)
        except asyncio.CancelledError:
            raise                                          # IN_PROGRESS stays: judged again after a restart if fresh
        except Exception:
            logger.exception("judging opportunity %s failed", opportunity.opportunity_id)
            await self._intake.mark(opportunity.opportunity_id, "FAILED", "ENGINE_ERROR")
            return
        await self._submitter.send_due()

    # -- slots ---------------------------------------------------------------------------------------
    async def run_due_slots(self) -> None:
        now = self._clock.now()
        for kind in (POSITION, ENTRY):
            slot = self._slots.latest(kind, now)
            await self._journal_elapsed_slots(kind, slot, now)
            if slot is None or await self._cycle_recorded(slot.cycle_id):
                continue
            if not self._slots.is_due(slot, now):
                await self.record_cycle(slot, "MISSED", "LATE_START")
                continue
            running = self._cycle_tasks.get(kind)
            if running is not None and not running.done():
                await self.record_cycle(slot, "MISSED", "PREVIOUS_CYCLE_RUNNING")
                continue
            if self._leadership.current_epoch() is None or not self._watch.known:
                continue                                   # decide later, while the slot is still due
            skip, positions = await self._cycle_gate(kind)
            if skip == WAIT:
                continue
            if skip is not None:
                await self.record_cycle(slot, "SKIPPED", skip)
                continue
            await self.record_cycle(slot, "RUNNING", None)
            self._cycle_tasks[kind] = self._spawn(self.run_cycle(slot, positions))

    async def _journal_elapsed_slots(self, kind: str, latest: Optional[Slot], now: dt.datetime) -> None:
        """Slots that started after the newest journaled one and before ``latest`` were never seen
        (a time jump or a restart): journal each as MISSED, never run them (Ruling 8, PR #84 thread 4210304802).
        A first start has nothing journaled and invents no slots from before the service ran."""
        row = await self._store.aquery("SELECT max(slot_start) FROM ai_cycles WHERE kind = ?", [kind], fetch="one")
        if row is None or row[0] is None:
            return
        newest = to_utc(row[0])
        if latest is not None and newest >= latest.start:
            return
        for slot in self._slots.elapsed(kind, newest, now):
            if latest is None or slot.start < latest.start:
                await self.record_cycle(slot, "MISSED", "LATE_START")

    async def _cycle_gate(self, kind: str) -> tuple[Optional[str], tuple]:
        experiment = self._watch.view
        if experiment is None:
            return "NO_EXPERIMENT", ()
        if kind == ENTRY:
            if experiment.state != "ARMED":
                return f"EXPERIMENT_{experiment.state}", ()
            return ("ENTRY_BLOCKED", ()) if experiment.entry_block else (None, ())
        if experiment.state not in ("ARMED", "PAUSED"):
            return f"EXPERIMENT_{experiment.state}", ()
        try:
            positions = owned_positions_from_trips(await self._supervisor.call(
                "get_experiment_trips", {"experiment_id": experiment.experiment_id}))
        except (*TRADER_AWAY, ValueError) as exc:
            logger.warning("owned positions unavailable (%s); the position slot waits", exc)
            return WAIT, ()
        return (None, positions) if positions else ("NO_OWNED_POSITIONS", ())

    async def _cycle_recorded(self, cycle_id: str) -> bool:
        return await self._store.aquery("SELECT 1 FROM ai_cycles WHERE cycle_id = ?", [cycle_id],
                                        fetch="one") is not None

    async def record_cycle(self, slot: Slot, state: str, reason: Optional[str]) -> None:
        now = self._clock.now()
        finished = None if state == "RUNNING" else now
        await self._store.atransaction(lambda conn: conn.execute(
            "INSERT INTO ai_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (cycle_id) DO NOTHING",
            [slot.cycle_id, slot.kind, f"{slot.session_date:%Y-%m-%d}", slot.start, state, reason, now, finished]))

    def _finish_cycle_in_tx(self, conn: Any, cycle_id: str, state: str, reason: Optional[str]) -> None:
        conn.execute("UPDATE ai_cycles SET state = ?, reason = ?, finished_at = ? WHERE cycle_id = ?",
                     [state, reason, self._clock.now(), cycle_id])

    async def run_cycle(self, slot: Slot, positions: tuple = ()) -> None:
        experiment = self._watch.view

        def finish(conn: Any, ok: bool, reason: Optional[str]) -> None:
            self._finish_cycle_in_tx(conn, slot.cycle_id, "DONE" if ok else "FAILED", reason)

        async def finish_now(state: str, reason: str) -> None:
            await self._store.atransaction(lambda conn: self._finish_cycle_in_tx(conn, slot.cycle_id, state, reason))
        try:
            work = await self._open_work(slot.cycle_id, "cycle", experiment)
            now = self._clock.now()
            if slot.kind == ENTRY:
                pending = self._engine.on_entry_cycle(EntryCycleContext(now, experiment, slot, work))
            else:
                pending = self._engine.on_position_cycle(PositionCycleContext(now, experiment, slot, positions, work))
            budget = max((slot.deadline - now).total_seconds(), 0.0)
            result = await asyncio.wait_for(pending, timeout=budget)       # bounded work per slot (spec 5.2)
            if self._clock.now() >= slot.deadline:                            # the answer came too late
                raise SlotDeadlinePassed(slot.cycle_id)
            await self._commit(CYCLE_SOURCE[slot.kind], slot.cycle_id, experiment, result, finish,
                               deadline=slot.deadline)
        except (TimeoutError, SlotDeadlinePassed):
            await finish_now("TIMED_OUT", "SLOT_DEADLINE")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cycle %s failed", slot.cycle_id)
            await finish_now("FAILED", "ENGINE_ERROR")
            return
        await self._submitter.send_due()

    # -- receipts, reporting, status ----------------------------------------------------------------
    async def reconcile_once(self) -> None:
        """Independent of cycles and of the budget (spec 5.2)."""
        await self._submitter.reconcile_once()
        await self._submitter.send_due()

    async def report_once(self) -> None:
        await self._outbox.pump_costs()
        await self._outbox.deliver_due()

    async def heartbeat(self) -> dict:
        status = {"at": self._clock.now().isoformat(), "holder_id": self._leadership.holder_id,
                  "epoch": self._leadership.current_epoch(),
                  "unsettled_submissions": await self._submitter.unsettled_count(),
                  "outbox": await self._outbox.counts(),
                  "running_cycles": sorted(kind for kind, task in self._cycle_tasks.items() if not task.done()),
                  "budget_cap_ready": None if self._cap_sync is None else self._cap_sync.ready(),
                  "exit_waits_stuck": sum(1 for until, _alert in (await self._intake.waits()).values()
                                          if self._clock.now() > until + EXIT_WAIT_OVERRUN),
                  "research": None if self._research is None else await self._research.counts()}
        if self._config.heartbeat_path:
            await asyncio.to_thread(_write_atomically, self._config.heartbeat_path, json.dumps(status))
        return status

    async def _every(self, stop: asyncio.Event, seconds: float, step: Callable[[], Awaitable[Any]],
                     name: str) -> None:
        while not stop.is_set():
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ai controller step %s failed", name)
            await self._clock.sleep(seconds)

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        cfg = self._config
        renewals = asyncio.create_task(self._leadership.run_renewals(stop))
        renewals.add_done_callback(lambda task: _stop_when_renewals_die(task, stop))
        loops = [renewals]
        for seconds, step, name in ((cfg.experiment_poll_seconds, self.refresh_experiment, "experiment"),
                                    (cfg.signal_poll_seconds, self.tick_signals, "signals"),
                                    (SLOT_POLL_SECONDS, self.run_due_slots, "slots"),
                                    (cfg.reconcile_seconds, self.reconcile_once, "reconcile"),
                                    (cfg.outbox_seconds, self.report_once, "outbox"),
                                    (cfg.heartbeat_seconds, self.heartbeat, "heartbeat")):
            loops.append(asyncio.create_task(self._every(stop, seconds, step, name)))
        if self._cap_sync is not None:
            loops.append(asyncio.create_task(self._every(stop, cfg.budget_cap_poll_seconds, self._cap_sync.sync,
                                                         "budget_cap")))
        if self._research is not None:
            for seconds, step, name in ((SLOT_POLL_SECONDS, self._research.run_due_slot, "research_slot"),
                                        (self._research.poll_seconds, self._research.pump, "research_pump")):
                loops.append(asyncio.create_task(self._every(stop, seconds, step, name)))
        try:
            await stop.wait()
        finally:
            for task in [*loops, *self._tasks]:
                task.cancel()
            await asyncio.gather(*loops, *list(self._tasks), return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.heartbeat()
