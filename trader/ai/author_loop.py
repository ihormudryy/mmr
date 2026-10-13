"""The nightly author (SP3 spec 7.4, 8; Plan 5 Rulings 5, 9-12, 15-17).

run_due_slot(): once per New York trading day after the close: the gates, the facts, at most two author calls (the
second only to fix a refused answer), then submit_authored_strategy as ai_author. One durable author_attempts row per
night: the submit body is stored before the call and resent unchanged after a lost reply.
recover(): at controller start, a night cut after a call may have left is FAILED and never asked again; a night cut
after its submit is checked against the trader's records by digest before anything is sent again.
The ai service never reaches the sandbox or the store: it sends text; the trader checks it, stores it and keeps records.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from trader.ai.author_facts import (AuthorFactsError, authored_history, build_author_facts, catalogue_evaluations,
                                    scoreboard_facts)
from trader.ai.author_roles import (CheckedAnswer, author_messages, check_answer, parse_author_answer,
                                    refusal_problems, retry_messages)
from trader.ai.author_wire import AuthoredStrategiesReply, parse_authored_strategies, parse_submit_reply
from trader.ai.budget import BudgetNotInitialized
from trader.ai.config import AUTHOR_ROLE, authored_universe
from trader.ai.decision_replay import manifest_mismatch
from trader.ai.gateway import CallFailed, CallRefused
from trader.ai.ids import canonical_json
from trader.ai.model_client import NOT_SENT, ModelRequest
from trader.ai.outbox import register_context_in_tx
from trader.ai.replay import INCOMPLETE, ExternalAdapterCounter, ReplayEvidence, ReplayResult, ReplaySession
from trader.ai.research_wire import WireError
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.schedule import AuthorSlot, author_slot_end
from trader.ai.tools import LiveTools, ReplayTools
from trader.ai.untrusted import OutputRefusal
from trader.authored.static_check import StaticViolation
from trader.automation.strategy_binding import bar_size_key

logger = logging.getLogger(__name__)
AWAY = (RpcNotSent, RpcOutcomeUnknown)
STORE_DOWN = "AUTHORED_STORE_UNAVAILABLE"   # an RpcRefused: the trader's store failed; wait inside the slot
STORE_UNAVAILABLE = "STORE_UNAVAILABLE"     # the row code when the slot ended during such an outage
FACTS_UNREADABLE = "FACTS_UNREADABLE"
ENGINE_ERROR = "ENGINE_ERROR"
REPORT_OBJECTS = ("account", "splits", "authored_vs_catalogue")    # what scoreboard_facts reads, each an object
CALLS_PER_NIGHT = 2                       # one answer and one retry (spec 7.4, 9)
HOLD_SECONDS = 60                         # a waiting gate is not asked again every second
SUBMITTING, FAILED = "SUBMITTING", "FAILED"


class _Skip(Exception):
    """The night is not authored: one SKIPPED row with this code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Wait(_Skip):
    """A gate cannot be read now (trader away, cap unread, store down): no row; tried again while the slot is due."""


class _Fail(Exception):
    """The night's facts could not be built: one FAILED row with this code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AuthorSettings:
    max_output_tokens: int
    call_timeout_seconds: float

    @classmethod
    def from_config(cls, config: Any) -> "AuthorSettings":
        return cls(config.role(AUTHOR_ROLE).max_output_tokens, config.authoring.call_timeout_seconds)


@dataclass(frozen=True)
class AuthorOutcome:
    state: str                                     # SUBMITTING (checked, body ready) or FAILED
    reason: str
    calls: int
    checked: Optional[CheckedAnswer] = None
    problems: tuple[StaticViolation, ...] = ()

    def summary(self) -> dict:
        return {"state": self.state, "reason": self.reason, "calls": self.calls,
                "digest": None if self.checked is None else self.checked.digest,
                "problems": [problem.rule for problem in self.problems]}


def common_bar_sizes(ours: Sequence[str], theirs: Sequence[str]) -> tuple[str, ...]:
    """Ruling 24: ai.yaml's sizes the trader also allows, in ai.yaml order and spelling. Sizes are compared as the
    trader compares them (bar_size_key), so "15 Mins" there matches "15 mins" here."""
    allowed = {bar_size_key(size) for size in theirs}
    menu: dict[str, str] = {}
    for size in ours:
        menu.setdefault(bar_size_key(size), size)
    return tuple(size for key, size in menu.items() if key in allowed)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _problems_json(problems: tuple[StaticViolation, ...]) -> Optional[str]:
    if not problems:
        return None
    return canonical_json([{"rule": p.rule, "line": p.line, "detail": p.detail} for p in problems])


def _as_report(reply: Any) -> dict:
    """The shape scoreboard_facts reads: a PAPER report whose account, splits, splits.strategy_version and
    authored_vs_catalogue are each an object or absent (Ruling 21: the view may be missing)."""
    if not isinstance(reply, dict) or reply.get("label") != "PAPER":
        raise WireError("get_scoreboard: the reply is not a PAPER report")
    for name in REPORT_OBJECTS:
        _require_object(reply.get(name), name)
    _require_object((reply.get("splits") or {}).get("strategy_version"), "splits.strategy_version")
    return reply


def _require_object(value: Any, name: str) -> None:
    if value is not None and not isinstance(value, dict):
        raise WireError(f"get_scoreboard: {name} is not an object")


async def _call_author(tools: Any, request: ModelRequest, call_no: int, seconds: float) -> Any:
    """One call. A refusal leaves no journal attempt, so each call records how it ended and replay takes the same
    branch without a gateway call, as Jev's backtest judgment does."""
    refusal = await tools.recorded_refusal(call_no)
    if refusal is not None:
        raise CallRefused(refusal)
    try:
        result = await tools.gateway.call(AUTHOR_ROLE, request, tools.gateway.new_deadline(tools.unit_key, seconds))
    except CallRefused as exc:
        await tools.record_try(call_no, exc.code)
        raise
    except CallFailed:
        await tools.record_try(call_no, None)
        raise
    await tools.record_try(call_no, None)
    return result


async def write_strategy(tools: Any, facts_value: Optional[dict], settings: AuthorSettings) -> AuthorOutcome:
    """One night's model work, replayable: one answer and one retry with code's problems, never more (Ruling 10).
    The bar-size menu is read from the recorded facts, so replay checks against the same menu."""
    facts = await tools.given("facts", facts_value)
    bar_sizes = tuple(facts["bar_sizes"])
    messages = author_messages(facts)
    problems: list[StaticViolation] = []
    for call_no in range(1, CALLS_PER_NIGHT + 1):
        request = ModelRequest(request_key=tools.request_key(AUTHOR_ROLE, call_no), messages=messages,
                               max_output_tokens=settings.max_output_tokens)
        try:
            result = await _call_author(tools, request, call_no, settings.call_timeout_seconds)
        except CallRefused as exc:
            return AuthorOutcome(FAILED, f"MODEL_REFUSED_{exc.code}", call_no - 1, problems=tuple(problems))
        except CallFailed as exc:
            return AuthorOutcome(FAILED, f"MODEL_FAILED_{exc.outcome}", call_no, problems=tuple(problems))
        text = result.response.text
        answer = parse_author_answer(text)
        checked = (refusal_problems(answer) if isinstance(answer, OutputRefusal)
                   else check_answer(answer, bar_sizes=bar_sizes))
        if isinstance(checked, CheckedAnswer):
            return AuthorOutcome(SUBMITTING, "CHECKED", call_no, checked)
        problems = list(checked)
        messages = retry_messages(messages, text, problems)
    return AuthorOutcome(FAILED, "CHECK_FAILED_TWICE", CALLS_PER_NIGHT, problems=tuple(problems))


class AuthorLoop:
    def __init__(self, *, config: Any, store: Any, clock: Any, slots: Any, leadership: Any, watch: Any, author: Any,
                 supervisor: Any, gateway: Any, recorder: Any):
        self._config, self._store, self._clock, self._slots = config, store, clock, slots
        self._leadership, self._watch = leadership, watch
        self._author, self._supervisor, self._gateway, self._recorder = author, supervisor, gateway, recorder
        self._settings = AuthorSettings.from_config(config)
        self._universe = authored_universe(config.research)
        self._hold: Optional[tuple[str, dt.datetime]] = None     # (attempt_id, not asked again before)
        self._last_wait: Optional[tuple[str, str]] = None        # (attempt_id, code) of the slot's latest wait
        self._logged: set[tuple[str, str]] = set()               # (attempt_id, code) already logged
        self._resend_after: Optional[dt.datetime] = None          # after a lost submit reply, not every second
        self._unreconciled: set[str] = set()                     # SUBMITTING rows a restart cut (Ruling 11)

    # -- the slot ------------------------------------------------------------------------------------
    async def run_due_slot(self) -> None:
        if self._leadership.current_epoch() is None:
            return                                   # a non-leader reads, asks, writes and sends nothing
        await self._resend_submissions()
        now = self._clock.now()
        slot = self._slots.author_slot(now)
        if slot is None or await self._row_exists(slot.attempt_id):
            return
        if not self._slots.author_due(slot, now):
            await self._close_unstarted(slot)
            return
        if self._held(slot, now) or not self._watch.known:
            return
        try:
            experiment_id, reply, bar_sizes = await self._gate()
            facts = await self._guarded_facts(slot, reply, bar_sizes)
        except _Wait as wait:
            self._hold_slot(slot, wait.code, now)
            return
        except _Skip as skip:
            await self._open(slot, "SKIPPED", skip.code)
            logger.info("author slot %s skipped: %s", slot.attempt_id, skip.code)
            return
        except _Fail as failed:
            await self._open(slot, FAILED, failed.code)
            return
        if await self._open(slot, "RUNNING", None):
            await self._run(slot, experiment_id, facts)

    async def _close_unstarted(self, slot: AuthorSlot) -> None:
        """A slot that ended without a row: a store outage that lasted to the end says so; anything else is late."""
        if self._last_wait == (slot.attempt_id, STORE_DOWN):
            logger.error("author slot %s ended while the trader's authored store was unavailable; no strategy "
                         "tonight", slot.attempt_id)
            await self._open(slot, "SKIPPED", STORE_UNAVAILABLE)
            return
        await self._open(slot, "MISSED", "LATE_START")

    async def _gate(self) -> tuple[str, AuthoredStrategiesReply, tuple[str, ...]]:
        """Ruling 9, in order. Returns the experiment, the trader's view and the bar-size menu (Ruling 24)."""
        view = self._watch.view
        if view is None:
            raise _Skip("NO_EXPERIMENT")
        if view.state == "KILLED":
            raise _Skip("EXPERIMENT_KILLED")
        if not self._gateway.ready():
            raise _Wait("BUDGET_CAP_UNKNOWN")
        reply = await self._read(self._author, "get_authored_strategies", parse_authored_strategies)
        if not reply.enabled:
            raise _Skip("AUTHORED_STRATEGIES_DISABLED")
        if not reply.cap_has_room:
            raise _Skip("AUTHORED_CAP_FULL")
        bar_sizes = common_bar_sizes(self._config.research.bar_sizes, reply.bar_sizes)
        if not bar_sizes:
            raise _Skip("NO_COMMON_BAR_SIZE")
        if not await self._budget_has_room():
            raise _Skip("BUDGET_NO_ROOM")
        return view.experiment_id, reply, bar_sizes

    async def _read(self, client: Any, method: str, parse: Callable[[Any], Any]) -> Any:
        try:
            return parse(await client.call(method, {}))
        except AWAY as exc:
            raise _Wait(exc.code) from None
        except RpcRefused as exc:
            if exc.code == STORE_DOWN:
                raise _Wait(exc.code) from None
            raise _Skip(f"READ_REFUSED_{exc.code}") from None
        except WireError as exc:
            logger.error("author: the %s reply does not parse; the night is skipped", method)
            logger.debug("author: %s", exc)           # may quote stored notes, which a model wrote
            raise _Skip("WIRE_ERROR") from None

    async def _budget_has_room(self) -> bool:
        """Room for one worst-case author call in today's New York budget (spec 7.4); the gateway still decides."""
        role = self._config.role(AUTHOR_ROLE)
        worst_case = self._config.prices.price_for(role.backend, role.model).cost_micros(
            role.max_input_tokens, role.max_output_tokens)
        try:
            snapshot = await self._gateway.budget.snapshot()
        except BudgetNotInitialized:
            raise _Wait("BUDGET_CAP_UNKNOWN") from None
        return snapshot.effective_cap_micros - snapshot.committed_micros >= worst_case

    async def _guarded_facts(self, slot: AuthorSlot, reply: AuthoredStrategiesReply,
                             bar_sizes: tuple[str, ...]) -> dict:
        """_facts, with every failure but a wait or a skip turned into a FAILED night: nothing escapes the loop."""
        try:
            return await self._facts(slot, reply, bar_sizes)
        except (_Skip, asyncio.CancelledError):
            raise
        except AuthorFactsError as exc:
            logger.error("author slot %s: no strategy tonight, a stored row cannot be read: %s", slot.attempt_id, exc)
            raise _Fail(FACTS_UNREADABLE) from None
        except Exception:
            logger.error("author slot %s: building the facts failed; no strategy tonight", slot.attempt_id,
                         exc_info=True)
            raise _Fail(ENGINE_ERROR) from None

    async def _facts(self, slot: AuthorSlot, reply: AuthoredStrategiesReply, bar_sizes: tuple[str, ...]) -> dict:
        report = await self._read(self._supervisor, "get_scoreboard", _as_report)
        return build_author_facts(
            session_date=slot.session_date, universe=self._universe, bar_sizes=bar_sizes,
            catalogue=await catalogue_evaluations(self._store), scoreboard=scoreboard_facts(report),
            authored=await authored_history(self._store, reply.records), trial_count=reply.judged_count,
            max_active=reply.max_active, counted=reply.counted)

    async def _run(self, slot: AuthorSlot, experiment_id: str, facts: dict) -> None:
        try:
            outcome = await self._write(slot, experiment_id, facts)
            if outcome.state != FAILED:
                await self._store_body(slot.attempt_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("author slot %s failed", slot.attempt_id)
            await self._fail_running(slot.attempt_id, ENGINE_ERROR)
            return
        if outcome.state == FAILED:
            logger.warning("author slot %s: no strategy tonight (%s)", slot.attempt_id, outcome.reason)
            await self._finish(slot.attempt_id, FAILED, outcome.reason, calls=outcome.calls,
                               problems=outcome.problems)
            return
        await self._submit(slot.attempt_id)

    async def _fail_running(self, attempt_id: str, reason: str) -> None:
        """Only a night still RUNNING: a body already stored as SUBMITTING keeps being sent."""
        await self._update("UPDATE author_attempts SET state = ?, reason = ?, finished_at = ? "
                           "WHERE attempt_id = ? AND state = 'RUNNING'", [FAILED, reason, self._clock.now(), attempt_id])

    async def _write(self, slot: AuthorSlot, experiment_id: str, facts: dict) -> AuthorOutcome:
        now = self._clock.now()
        await self._store.atransaction(lambda conn: register_context_in_tx(
            conn, context_key=slot.attempt_id, experiment_id=experiment_id, served_kind="research",
            served_id=slot.attempt_id, now=now))
        tools = LiveTools(unit_key=slot.attempt_id, reads=None, recorder=self._recorder, clock=self._clock,
                          gateway=self._gateway, deadline=None)
        try:
            return await write_strategy(tools, facts, self._settings)
        finally:
            await tools.finish(self._config.digest())

    # -- submit: the stored body, resent unchanged (Ruling 12) ---------------------------------------
    async def _resend_submissions(self) -> None:
        if self._is_resend_held():
            return
        rows = await self._store.aquery("SELECT attempt_id FROM author_attempts WHERE state = 'SUBMITTING' "
                                        "ORDER BY session_date")
        pending = [attempt_id for (attempt_id,) in rows]
        if self._unreconciled & set(pending) and not await self._reconcile():
            return
        for attempt_id in pending:
            await self._submit(attempt_id)

    def _is_resend_held(self) -> bool:
        return self._resend_after is not None and self._clock.now() < self._resend_after

    def _hold_resends(self) -> None:
        self._resend_after = self._clock.now() + dt.timedelta(seconds=HOLD_SECONDS)

    async def _reconcile(self) -> bool:
        """A submission a restart cut may already be stored: the trader's records decide by digest before anything
        is sent again. False while they cannot be read (nothing is sent then)."""
        try:
            reply = parse_authored_strategies(await self._author.call("get_authored_strategies", {}))
        except (*AWAY, RpcRefused) as exc:
            if isinstance(exc, RpcRefused) and exc.code != STORE_DOWN:
                logger.error("author: the trader refused the records read (%s); cut submissions are sent again "
                             "unchecked (the digest makes a repeat EXISTING)", exc.code)
                self._unreconciled.clear()
                return True
            self._hold_resends()
            self._log_once("recover", exc.code, logging.WARNING, "the trader's records cannot be read yet; no cut "
                                                                 "submission is sent again until they are")
            return False
        except WireError as exc:
            logger.error("author: the get_authored_strategies reply does not parse; cut submissions are sent again "
                         "unchecked (the digest makes a repeat EXISTING)")
            logger.debug("author: %s", exc)
            self._unreconciled.clear()
            return True
        held = {record.digest: record for record in reply.records}
        for attempt_id in sorted(self._unreconciled):
            self._unreconciled.discard(attempt_id)
            await self._settle_cut_submission(attempt_id, held)
        return True

    async def _settle_cut_submission(self, attempt_id: str, held: dict) -> None:
        row = await self._store.aquery("SELECT digest FROM author_attempts WHERE attempt_id = ? "
                                       "AND state = 'SUBMITTING'", [attempt_id], fetch="one")
        if row is None:
            return
        record = held.get(row[0])
        if record is None:
            logger.warning("author %s: the trader holds no file %s; the stored body is sent again", attempt_id, row[0])
            return
        logger.info("author %s: the trader already holds %s; it is not sent again", attempt_id, record.key)
        await self._finish(attempt_id, "STORED", "EXISTING",
                           receipt={"source": "get_authored_strategies", "record": record.model_dump()})

    async def _store_body(self, attempt_id: str, outcome: AuthorOutcome) -> None:
        checked = outcome.checked
        body = canonical_json(checked.submit_body(self._config.role(AUTHOR_ROLE).model))
        await self._update(
            "UPDATE author_attempts SET state = 'SUBMITTING', reason = 'CHECKED', calls = ?, body_json = ?, "
            "body_sha256 = ?, digest = ?, authored_key = ?, tunables_json = ? WHERE attempt_id = ? AND state = 'RUNNING'",
            [outcome.calls, body, _sha(body), checked.digest, checked.authored_key, canonical_json(checked.tunables),
             attempt_id])

    async def _submit(self, attempt_id: str) -> None:
        row = await self._store.aquery("SELECT body_json, digest, session_date FROM author_attempts "
                                       "WHERE attempt_id = ? AND state = 'SUBMITTING'", [attempt_id], fetch="one")
        if row is None:
            return
        body_json, digest, session_date = row
        try:
            reply = parse_submit_reply(await self._author.call("submit_authored_strategy", json.loads(body_json)))
        except AWAY as exc:
            self._hold_resends()
            self._log_once(attempt_id, exc.code, logging.WARNING, "submit not answered; the same body is sent again")
            return
        except RpcRefused as exc:
            if exc.code == STORE_DOWN:
                await self._store_was_down(attempt_id, session_date)
                return
            await self._finish(attempt_id, "REFUSED", f"RPC_{exc.code}")
            return
        except WireError as exc:
            self._hold_resends()
            self._log_once(attempt_id, "WIRE_ERROR", logging.ERROR, "the submit reply does not parse; the same body "
                                                                    "is sent again")
            logger.debug("author %s: %s", attempt_id, exc)
            return
        if reply.status == "REFUSED" and reply.retryable:
            self._hold_resends()
            self._log_once(attempt_id, reply.code or "REFUSED", logging.WARNING, "refused for now; sent again later")
        elif reply.status == "REFUSED":
            logger.warning("author %s: the trader refused the file: %s", attempt_id, reply.code)
            await self._finish(attempt_id, "REFUSED", reply.code or "REFUSED_WITHOUT_CODE",
                               receipt=reply.model_dump())
        elif reply.record.digest != digest:
            logger.error("author %s: the trader answered digest %s for the file %s", attempt_id, reply.record.digest,
                         digest)
            await self._finish(attempt_id, FAILED, "DIGEST_MISMATCH", receipt=reply.model_dump())
        else:
            logger.info("author %s: %s %s", attempt_id, reply.status, reply.record.key)
            await self._finish(attempt_id, "STORED", reply.status, receipt=reply.model_dump())

    async def _store_was_down(self, attempt_id: str, session_date: str) -> None:
        """The same body is sent again while the night's slot lasts; after it the night failed with a body sent."""
        if self._clock.now() >= author_slot_end(dt.date.fromisoformat(session_date)):
            logger.error("author %s: the trader's authored store stayed unavailable to the end of the slot; no "
                         "strategy tonight", attempt_id)
            await self._finish(attempt_id, FAILED, STORE_UNAVAILABLE)
            return
        self._hold_resends()
        self._log_once(attempt_id, STORE_DOWN, logging.WARNING, "the trader's store is unavailable; the same body is "
                                                                "sent again")

    # -- rows ----------------------------------------------------------------------------------------
    async def _row_exists(self, attempt_id: str) -> bool:
        return await self._store.aquery("SELECT 1 FROM author_attempts WHERE attempt_id = ?", [attempt_id],
                                        fetch="one") is not None

    async def _open(self, slot: AuthorSlot, state: str, reason: Optional[str]) -> bool:
        """True when this call wrote the night's row; an existing row means the night is taken."""
        now = self._clock.now()
        row = await self._store.atransaction(lambda conn: conn.execute(
            "INSERT INTO author_attempts (attempt_id, session_date, slot_start, state, reason, author_model, "
            "started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (attempt_id) DO NOTHING "
            "RETURNING attempt_id",
            [slot.attempt_id, f"{slot.session_date:%Y-%m-%d}", slot.start, state, reason,
             self._config.role(AUTHOR_ROLE).model, now, None if state == "RUNNING" else now]).fetchone())
        return row is not None

    async def _finish(self, attempt_id: str, state: str, reason: str, *, calls: Optional[int] = None,
                      problems: tuple[StaticViolation, ...] = (), receipt: Optional[dict] = None) -> None:
        await self._update(
            "UPDATE author_attempts SET state = ?, reason = ?, calls = COALESCE(?, calls), "
            "violations_json = COALESCE(?, violations_json), receipt_json = ?, finished_at = ? WHERE attempt_id = ?",
            [state, reason, calls, _problems_json(problems), None if receipt is None else canonical_json(receipt),
             self._clock.now(), attempt_id])

    async def _update(self, sql: str, params: list) -> None:
        await self._store.atransaction(lambda conn: conn.execute(sql, params))

    def _held(self, slot: AuthorSlot, now: dt.datetime) -> bool:
        return self._hold is not None and self._hold[0] == slot.attempt_id and now < self._hold[1]

    def _hold_slot(self, slot: AuthorSlot, code: str, now: dt.datetime) -> None:
        self._hold = (slot.attempt_id, now + dt.timedelta(seconds=HOLD_SECONDS))
        self._last_wait = (slot.attempt_id, code)
        self._log_once(slot.attempt_id, code, logging.WARNING, "the author slot waits and is tried again while due")

    def _log_once(self, attempt_id: str, code: str, level: int, text: str) -> None:
        if (attempt_id, code) not in self._logged:
            self._logged.add((attempt_id, code))
            logger.log(level, "author %s (%s): %s", attempt_id, code, text)

    # -- restart and status --------------------------------------------------------------------------
    async def recover(self) -> None:
        """Ruling 11: run after the gateway turned STARTED attempts into UNKNOWN. Reads only ai.duckdb: a cut
        submission is checked against the trader by the first resend tick, before anything is sent again."""
        rows = await self._store.aquery("SELECT attempt_id FROM author_attempts WHERE state = 'RUNNING'")
        for (attempt_id,) in rows:
            statuses = {row[0] for row in await self._store.aquery(
                "SELECT status FROM ai_model_attempts WHERE request_key LIKE ?", [f"{attempt_id}/{AUTHOR_ROLE}/%"])}
            if statuses <= {NOT_SENT}:
                logger.warning("author slot %s was cut by a restart before its call was sent; it runs again while "
                               "due", attempt_id)
                await self._update("DELETE FROM author_attempts WHERE attempt_id = ? AND state = 'RUNNING'",
                                   [attempt_id])
                continue
            logger.error("author slot %s was cut by a restart after a call was sent (%s); it is not asked again",
                         attempt_id, ", ".join(sorted(statuses)))
            await self._finish(attempt_id, FAILED, "PROCESS_RESTARTED")
        cut = await self._store.aquery("SELECT attempt_id FROM author_attempts WHERE state = 'SUBMITTING'")
        self._unreconciled = {attempt_id for (attempt_id,) in cut}
        if self._unreconciled:
            logger.warning("author: %d submission(s) cut by a restart; each is checked against the trader's records "
                           "before it is sent again", len(self._unreconciled))

    async def counts(self) -> dict:
        pending = await self._store.aquery("SELECT COUNT(*) FROM author_attempts "
                                           "WHERE state IN ('RUNNING', 'SUBMITTING')", fetch="one")
        last = await self._store.aquery("SELECT attempt_id, state, reason FROM author_attempts "
                                        "ORDER BY session_date DESC LIMIT 1", fetch="one")
        return {"unsubmitted": pending[0],
                "last": None if last is None else {"attempt_id": last[0], "state": last[1], "reason": last[2]}}


async def replay_author_attempt(store: Any, attempt_id: str, *, config: Any,
                                counter: Optional[ExternalAdapterCounter] = None) -> ReplayResult:
    """The night's outcome from recorded evidence with zero model calls; another config or code is INCOMPLETE."""
    passes = (await store.aquery("SELECT COUNT(*) FROM ai_replay_evidence WHERE decision_key = ? "
                                 "AND name = 'given:facts'", [attempt_id], fetch="one"))[0]
    if passes > 1:
        return ReplayResult(INCOMPLETE, missing=("rerun_unit",))
    evidence = await asyncio.to_thread(ReplayEvidence.load, store, attempt_id)
    mismatch = manifest_mismatch(evidence.manifest, config)
    if mismatch:
        return ReplayResult(INCOMPLETE, missing=mismatch)
    session = ReplaySession(evidence, counter or ExternalAdapterCounter())
    settings = AuthorSettings.from_config(config)

    async def work(replay: ReplaySession) -> dict:
        return (await write_strategy(ReplayTools(replay, attempt_id), None, settings)).summary()
    result = await session.arun(work)
    session.assert_no_external_calls()
    return result
