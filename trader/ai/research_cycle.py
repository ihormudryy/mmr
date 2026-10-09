"""The ai controller's research cycle (SP2c spec 5.3, 8; Plan 4 Rulings 1-21).

run_due_slot(): once per session after the close: ask renewals of expired lines, end withdrawn or ended ones, one
orchestrator call, screening, candidates.
pump(): inside the research window, moves each durable row one step. Every step is stored before its RPC and is
safe to repeat: a lost reply resends the stored body unchanged. A candidate is submitted only inside its own
slot's window; an INITIAL one that window never sent is closed as STALE_NOT_SUBMITTED, never carried into another
night (a waiting renewal is never closed so; it is asked again in the next window).
recover(): at controller start, settles what a dead process left RUNNING or JUDGING; the pump resumes the rest."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from trader.ai.backtest_judge import (NO_VERDICT, BacktestCase, JudgmentDecision, NotJudged, jev_menu,
                                      judgment_body, judgment_id_for)
from trader.ai.gateway import CallFailed, CallRefused
from trader.ai.ids import canonical_json
from trader.ai.model_client import NOT_SENT, ModelRequest
from trader.ai.outbox import register_context_in_tx
from trader.ai.research_menu import Dropped, ResearchMenu, build_menu, screen_picks
from trader.ai.research_roles import parse_research_proposal, research_messages
from trader.ai.research_wire import (AttestReply, Binding, CaseSummary, EvaluationView, JudgmentReceipt,
                                     Registered, RegisterRefused, SubmitReply, VersionReply, WireError,
                                     parse_registration, parse_reply)
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.schedule import ResearchSlot
from trader.ai.store import to_utc
from trader.ai.untrusted import OutputRefusal, fence_untrusted

logger = logging.getLogger(__name__)
AWAY = (RpcNotSent, RpcOutcomeUnknown)        # RESEARCH_UNREACHABLE, TRADER_UNREACHABLE, a lost reply: next tick
RENEWAL = "RENEWAL"
RENEWABLE_STATE = "EXPIRED"                   # Plan 5 ruling 1: a version is renewed only after it expired
LINE_ENDING_STATES = frozenset({"WITHDRAWN", "ENDED"})
THESIS_LABEL = "orchestrator_thesis"
THESIS_MAX_CHARS = 1000                   # the proposal schema's own limit
TERMINAL = frozenset({"DONE", "FAILED"})
RETRY_REGISTRATION = frozenset({"DEPLOY_CAP_REACHED"})
INTERNAL_ERROR = "INTERNAL_ERROR"                # the typed RPC server's catch-all: the handler may have committed
UNSETTLED_ERROR_SECONDS = 900                    # the trader reconciler's critical-alert boundary
ATTEST_WAITS_FOR_TRADER = "TRADER_UNAVAILABLE"   # infrastructure behind the research service: no try is spent
ATTEST_MAX_TRIES = 3                             # a "retryable" bundle error may be deterministic (Plan 3 as built)
STALE_NOT_SUBMITTED = "STALE_NOT_SUBMITTED"
DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
RESEARCH_REPLY_MISMATCH = "RESEARCH_REPLY_MISMATCH"     # a case of another kind or version than the candidate asked
RENEWAL_REGISTRATION_BODY_MISSING = "RENEWAL_REGISTRATION_BODY_MISSING"   # the renewed line's own row is damaged
SLOT_HOLD_SECONDS = 60                           # a slot refused with no retry time waits this long between tries


class _NothingSent(Exception):
    """The orchestrator call was refused or never left the process. The slot may try again."""

    def __init__(self, code: str, retry_at: Optional[dt.datetime] = None):
        super().__init__(code)
        self.code, self.retry_at = code, retry_at


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_id_for(cycle_id: str, strategy_key: str) -> str:
    """The only candidate identity. The table has no UNIQUE(cycle_id, strategy_key): every insert uses this id."""
    return "rc-" + _sha(f"{cycle_id}|{strategy_key}")[:32]


def renewal_candidate_id(prior_version_digest: str) -> str:
    """Ruling 15: one renewal candidate per version, whatever the cycle."""
    return "rr-" + _sha(f"{RENEWAL}|{prior_version_digest}")[:32]


def proposal_request_key(cycle_id: str) -> str:
    return f"{cycle_id}/orchestrator/1"


def registration_body(binding: Binding, *, judgment_id: str, bundle_digest: str) -> dict:
    """Ruling 16: the binding comes from the bundle the research service signed; the evidence is that bundle."""
    deployment = {"strategy_path": binding.strategy_path, "strategy_digest": binding.file_hash,
                  "class_name": binding.class_name, "params": dict(binding.params),
                  "conids": sorted(binding.conids), "bar_size": binding.bar_size, "style": "intraday_long",
                  "decider": "jev", "decider_verdict": "DEPLOY", "evidence_ref": bundle_digest,
                  "evidence_order_notional": binding.order_notional}
    return {"deployment": deployment, "judgment_id": judgment_id, "bundle_digest": bundle_digest}


def stored_thesis(thesis: str) -> str:
    """The orchestrator's thesis is untrusted text. It is stored fenced and never sent to Jev (Ruling 19)."""
    return fence_untrusted(THESIS_LABEL, thesis, max_chars=THESIS_MAX_CHARS)


class ResearchCycle:
    def __init__(self, *, config: Any, store: Any, clock: Any, slots: Any, leadership: Any, watch: Any, lab: Any,
                 registry: Any, gateway: Any, judge: Any, strategies_root: Path):
        self._config, self._cfg = config, config.research
        self._store, self._clock, self._slots = store, clock, slots
        self._leadership, self._watch = leadership, watch
        self._lab, self._registry, self._gateway, self._judge = lab, registry, gateway, judge
        self._root = strategies_root
        self._stuck_reported: set[str] = set()            # JUDGING rows already logged as stuck
        self._registration_waits: set[tuple[str, str]] = set()   # (judgment_id, code) already logged
        self._unsettled_since: dict[str, dt.datetime] = {}      # judgment_id -> first NOT_SETTLED receipt
        self._slot_hold: Optional[tuple[str, dt.datetime]] = None    # (cycle_id, no orchestrator try before)
        self._slot_waits: set[tuple[str, str]] = set()           # (cycle_id, code) already logged
        self._lines_checked_for: Optional[str] = None            # the cycle whose finished lines were ended

    @property
    def poll_seconds(self) -> float:
        return self._cfg.poll_seconds

    def _experiment_id(self) -> Optional[str]:
        view = self._watch.view if self._watch.known else None
        return None if view is None else view.experiment_id

    async def _update(self, sql: str, params: list) -> None:
        await self._store.atransaction(lambda conn: conn.execute(sql, params))

    # -- the slot ------------------------------------------------------------------------------------
    async def run_due_slot(self) -> None:
        if not self._cfg.enabled:
            return
        now = self._clock.now()
        slot = self._slots.research_slot(now)
        if slot is None or await self._store.aquery("SELECT 1 FROM ai_research_cycles WHERE cycle_id = ?",
                                                    [slot.cycle_id], fetch="one") is not None:
            return
        if not self._slots.research_due(slot, now):
            return await self._open_cycle(slot, "MISSED", "LATE_START")
        if self._slot_hold is not None and self._slot_hold[0] == slot.cycle_id and now < self._slot_hold[1]:
            return
        if self._leadership.current_epoch() is None or not self._watch.known:
            return                                                    # decide later, while the slot is due
        experiment_id = self._experiment_id()
        if experiment_id is None:
            return await self._open_cycle(slot, "SKIPPED", "NO_EXPERIMENT")
        if not await self._open_cycle(slot, "RUNNING", None):
            return
        try:
            await self._run_slot(slot, experiment_id)
        except asyncio.CancelledError:
            raise
        except _NothingSent as exc:
            await self._update("DELETE FROM ai_research_cycles WHERE cycle_id = ? AND state = 'RUNNING'",
                               [slot.cycle_id])
            self._hold_slot(slot, exc, now)
        except Exception:
            logger.exception("research slot %s failed", slot.cycle_id)
            await self._store.atransaction(lambda conn: self._finish_cycle_in_tx(
                conn, slot.cycle_id, "FAILED", "ENGINE_ERROR", None, ()))

    def _hold_slot(self, slot: ResearchSlot, refusal: _NothingSent, now: dt.datetime) -> None:
        """A lasting refusal (a spent budget, a closed cap gate) is not asked again every second."""
        retry_at = refusal.retry_at
        until = retry_at if retry_at is not None and retry_at > now else now + dt.timedelta(seconds=SLOT_HOLD_SECONDS)
        self._slot_hold = (slot.cycle_id, until)
        if (slot.cycle_id, refusal.code) not in self._slot_waits:
            self._slot_waits.add((slot.cycle_id, refusal.code))
            logger.warning("research slot %s: no model call was made (%s); tried again from %s while the slot is "
                           "due", slot.cycle_id, refusal.code, until.isoformat())

    async def _open_cycle(self, slot: ResearchSlot, state: str, reason: Optional[str]) -> bool:
        """True when this call wrote the row. A row that already exists means the slot is taken."""
        now = self._clock.now()
        row = await self._store.atransaction(lambda conn: conn.execute(
            "INSERT INTO ai_research_cycles (cycle_id, session_date, slot_start, state, reason, started_at, "
            "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (cycle_id) DO NOTHING RETURNING cycle_id",
            [slot.cycle_id, f"{slot.session_date:%Y-%m-%d}", slot.start, state, reason, now,
             None if state == "RUNNING" else now]).fetchone())
        return row is not None

    def _finish_cycle_in_tx(self, conn: Any, cycle_id: str, state: str, reason: str,
                            menu: Optional[ResearchMenu], dropped: tuple[Dropped, ...]) -> None:
        conn.execute("UPDATE ai_research_cycles SET state = ?, reason = ?, menu_json = ?, dropped_json = ?, "
                     "finished_at = ? WHERE cycle_id = ?",
                     [state, reason, None if menu is None else canonical_json(menu.to_json()),
                      canonical_json([{"pick": d.pick, "code": d.code, "detail": d.detail} for d in dropped]),
                      self._clock.now(), cycle_id])

    async def _run_slot(self, slot: ResearchSlot, experiment_id: str) -> None:
        if self._lines_checked_for != slot.cycle_id:
            await self._end_finished_lines(slot)
            self._lines_checked_for = slot.cycle_id
        rows = await self._store.aquery("SELECT strategy_key FROM ai_research_cooldowns WHERE until_session >= ?",
                                        [f"{slot.session_date:%Y-%m-%d}"])
        menu, menu_drops = build_menu(self._cfg, strategies_root=self._root,
                                      cooling=frozenset(row[0] for row in rows))
        empty_reason = self._empty_menu_reason(menu)
        if empty_reason is not None:
            self._log_empty_menu(slot, empty_reason, menu_drops)
            return await self._store.atransaction(lambda conn: self._finish_cycle_in_tx(
                conn, slot.cycle_id, "DONE", empty_reason, menu, menu_drops))
        picks = await self._propose(slot, menu, experiment_id)
        if isinstance(picks, OutputRefusal):
            self._log_lost_proposal(slot, picks)
            return await self._store.atransaction(lambda conn: self._finish_cycle_in_tx(
                conn, slot.cycle_id, "DONE", f"PROPOSAL_{picks.code}", menu, menu_drops))
        cohorts, dropped = screen_picks(picks, menu)
        now = self._clock.now()

        def work(conn: Any) -> None:
            for cohort in cohorts:
                body = cohort.submit_body()                # no day, no id: the research service sets both (Ruling 9)
                text = canonical_json(body)
                conn.execute(
                    "INSERT INTO ai_research_candidates (candidate_id, cycle_id, kind, strategy_key, thesis, body_json, "
                    "body_sha256, state, next_try_at, created_at, updated_at) "
                    "VALUES (?, ?, 'INITIAL', ?, ?, ?, ?, 'NEW', ?, ?, ?) ON CONFLICT (candidate_id) DO NOTHING",
                    [candidate_id_for(slot.cycle_id, cohort.strategy_key), slot.cycle_id, cohort.strategy_key,
                     stored_thesis(cohort.thesis), text, _sha(text), now, now, now])
            self._finish_cycle_in_tx(conn, slot.cycle_id, "DONE", f"CANDIDATES_{len(cohorts)}", menu,
                                     menu_drops + dropped)
        await self._store.atransaction(work)

    @staticmethod
    def _empty_menu_reason(menu: ResearchMenu) -> Optional[str]:
        if not menu.strategies:
            return "NO_STRATEGY_ON_MENU"
        if not (menu.universes and menu.bar_sizes):
            return "MENU_INCOMPLETE"
        return None

    @staticmethod
    def _log_empty_menu(slot: ResearchSlot, reason: str, drops: tuple[Dropped, ...]) -> None:
        """An empty menu must not be silent. Strategies that only cool down after a REJECT are expected."""
        codes = sorted({drop.code for drop in drops})
        if reason == "NO_STRATEGY_ON_MENU" and codes == ["COOLING_DOWN"]:
            logger.warning("research slot %s: every listed strategy is cooling down", slot.cycle_id)
        else:
            logger.error("research slot %s: %s with research enabled (menu drops: %s). Check research.strategy_keys, "
                         "universes and that the strategies directory is visible to this container",
                         slot.cycle_id, reason, ", ".join(codes) or "none")

    @staticmethod
    def _log_lost_proposal(slot: ResearchSlot, refusal: OutputRefusal) -> None:
        """A slot that ends without candidates because of the model is never silent."""
        if refusal.code.startswith("MODEL_"):
            logger.error("research slot %s lost: the orchestrator call failed after it was sent (%s %s)",
                         slot.cycle_id, refusal.code, refusal.detail)
        else:
            logger.warning("research slot %s: the orchestrator answer was unusable (%s)", slot.cycle_id, refusal.code)

    async def _propose(self, slot: ResearchSlot, menu: ResearchMenu, experiment_id: str) -> Any:
        unit, now = slot.cycle_id, self._clock.now()
        await self._store.atransaction(lambda conn: register_context_in_tx(
            conn, context_key=unit, experiment_id=experiment_id, served_kind="research", served_id=unit, now=now))
        request = ModelRequest(request_key=proposal_request_key(unit),
                               messages=research_messages(menu, session_date=slot.session_date),
                               max_output_tokens=self._config.role("orchestrator").max_output_tokens)
        try:
            result = await self._gateway.call("orchestrator", request, self._gateway.new_deadline(unit))
        except CallRefused as exc:
            raise _NothingSent(exc.code, exc.retry_at) from None
        except CallFailed as exc:
            if exc.outcome == NOT_SENT:
                raise _NothingSent(exc.code) from None
            return OutputRefusal(f"MODEL_FAILED_{exc.outcome}", exc.code)
        return parse_research_proposal(result.response.text)

    async def _end_finished_lines(self, slot: ResearchSlot) -> None:
        """An EXPIRED version asks for a renewal (the line becomes RENEWING); WITHDRAWN or ENDED ends the line."""
        await self._store.atransaction(lambda conn: self._reopen_stranded_renewals_in_tx(conn, slot))
        rows = await self._store.aquery("SELECT version_digest, strategy_key FROM ai_research_registrations "
                                        "WHERE state = 'REGISTERED' AND line_state = 'LIVE'")
        for version, strategy_key in rows:
            try:
                reply = parse_reply(VersionReply, "get_ai_deployment_version", await self._registry.call(
                    "get_ai_deployment_version", {"version_digest": version}))
            except (*AWAY, RpcRefused, WireError) as exc:
                logger.warning("deployment version %s unreadable (%s); its line stays live for now", version, exc)
                continue
            if not reply.found:
                logger.error("deployment version %s is unknown to the trader", version)
            elif reply.version.state == RENEWABLE_STATE:
                await self._store.atransaction(lambda conn, v=version, k=strategy_key:
                                               self._request_renewal_in_tx(conn, slot, v, k))
            elif reply.version.state in LINE_ENDING_STATES:
                await self._store.atransaction(lambda conn, v=version, s=reply.version.state:
                                               self._end_line_in_tx(conn, v, s))

    def _reopen_stranded_renewals_in_tx(self, conn: Any, slot: ResearchSlot) -> None:
        """Ruling 17: a RENEWING line whose candidate was closed without a judgment asks again (never silently
        ends). A renewal still NEW from an earlier window (say, waiting on FORWARD_EVIDENCE_PENDING) moves on.
        Both join this slot's cycle, so the pump does not close them as STALE_NOT_SUBMITTED."""
        now = self._clock.now()
        moved = conn.execute(
            "UPDATE ai_research_candidates SET cycle_id = ?, updated_at = ? WHERE kind = 'RENEWAL' AND state = 'NEW' "
            "AND cycle_id <> ? AND prior_version_digest IN (SELECT version_digest FROM ai_research_registrations "
            "WHERE line_state = 'RENEWING') RETURNING candidate_id", [slot.cycle_id, now, slot.cycle_id]).fetchall()
        for (candidate_id,) in moved:
            logger.info("renewal candidate %s still waits; it moves to %s", candidate_id, slot.cycle_id)
        rows = conn.execute(
            "SELECT c.candidate_id, c.end_code FROM ai_research_registrations r "
            "JOIN ai_research_candidates c ON c.kind = 'RENEWAL' AND c.prior_version_digest = r.version_digest "
            "LEFT JOIN ai_backtest_judgments j ON j.candidate_id = c.candidate_id "
            "WHERE r.line_state = 'RENEWING' AND c.state = 'CLOSED' AND j.judgment_id IS NULL").fetchall()
        for candidate_id, end_code in rows:
            logger.error("renewal candidate %s was closed (%s) without a judgment; asked again", candidate_id, end_code)
            conn.execute("UPDATE ai_research_candidates SET state = 'NEW', cycle_id = ?, end_code = NULL, "
                         "request_id = NULL, submit_reply_lost = FALSE, accepted_at = NULL, next_try_at = ?, "
                         "updated_at = ? WHERE candidate_id = ?", [slot.cycle_id, now, now, candidate_id])

    def _request_renewal_in_tx(self, conn: Any, slot: ResearchSlot, version: str, strategy_key: str) -> None:
        """The candidate shares the cycle with INITIAL ones, but the cap close skips it (kind = 'RENEWAL').
        The body names only the version: the research service builds the case (Plan 5 ruling 2: no claim)."""
        body = canonical_json({"kind": RENEWAL, "prior_version_digest": version})
        now = self._clock.now()
        conn.execute(                                         # only while the line is LIVE: no orphan renewal
            "INSERT INTO ai_research_candidates (candidate_id, cycle_id, kind, strategy_key, prior_version_digest, "
            "body_json, body_sha256, state, next_try_at, created_at, updated_at) "
            "SELECT ?, ?, 'RENEWAL', ?, ?, ?, ?, 'NEW', ?, ?, ? FROM ai_research_registrations "
            "WHERE version_digest = ? AND line_state = 'LIVE' ON CONFLICT (candidate_id) DO NOTHING",
            [renewal_candidate_id(version), slot.cycle_id, strategy_key, version, body, _sha(body), now, now, now,
             version])
        renewing = conn.execute("UPDATE ai_research_registrations SET line_state = 'RENEWING', updated_at = ? "
                                "WHERE version_digest = ? AND line_state = 'LIVE' RETURNING version_digest",
                                [now, version]).fetchone()
        if renewing is not None:
            logger.info("deployment version %s expired; renewal requested", version)

    def _end_line_in_tx(self, conn: Any, version: str, code: str) -> None:
        ended = conn.execute(
            "UPDATE ai_research_registrations SET line_state = 'ENDED', error_code = ?, updated_at = ? "
            "WHERE version_digest = ? AND line_state IN ('LIVE', 'RENEWING') RETURNING version_digest",
            [code, self._clock.now(), version]).fetchone()
        if ended is not None:
            logger.info("deployment line of %s ended: %s", version, code)

    # -- the pump ------------------------------------------------------------------------------------
    async def pump(self) -> None:
        if not self._cfg.enabled or self._leadership.current_epoch() is None:
            return
        now = self._clock.now()
        slot = self._slots.research_slot(now)
        experiment_id = self._experiment_id()
        if slot is None or not self._slots.research_due(slot, now) or experiment_id is None:
            return                                                        # Rulings 2 and 3
        await self._close_unsent_of_closed_windows(slot.cycle_id)
        await self._start_new(slot, now)
        await self._poll_submitted(now)
        await self._judge_evaluated(experiment_id)
        await self._record_decided()
        await self._advance_registrations(now)

    async def _each(self, rows: list, work: Any, what: str) -> None:
        """One bad row never stops the others; it stays where it is and is tried again."""
        for row in rows:
            try:
                await work(*row)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("research %s failed for %s", what, row[0])

    async def _close_unsent_of_closed_windows(self, open_cycle_id: str) -> None:
        """An INITIAL candidate of an earlier slot that was never accepted, because it was written after its
        window closed or every try was lost, is closed: tomorrow's research day would claim a new evaluation for
        it. A waiting renewal is never closed here; the next slot moves it into its own cycle."""
        now = self._clock.now()
        rows = await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_research_candidates SET state = 'CLOSED', end_code = ?, updated_at = ? "
            "WHERE state = 'NEW' AND kind = 'INITIAL' AND cycle_id <> ? "
            "RETURNING candidate_id, cycle_id, submit_reply_lost",
            [STALE_NOT_SUBMITTED, now, open_cycle_id]).fetchall())
        for candidate_id, cycle_id, reply_lost in rows:
            if reply_lost:
                logger.error("research candidate %s of %s: %s after a lost submit reply; the research service may "
                             "hold an accepted evaluation for it. Check get_evaluation by its body.", candidate_id,
                             cycle_id, STALE_NOT_SUBMITTED)
            else:
                logger.warning("research candidate %s of %s was never submitted inside its window: %s",
                               candidate_id, cycle_id, STALE_NOT_SUBMITTED)

    # submit, and resend the unchanged body after a lost reply (Ruling 9) ------------------------------
    async def _start_new(self, slot: ResearchSlot, now: dt.datetime) -> None:
        rows = await self._store.aquery(
            "SELECT candidate_id, cycle_id, strategy_key, body_json, prior_version_digest FROM ai_research_candidates "
            "WHERE state = 'NEW' AND next_try_at <= ? ORDER BY created_at, candidate_id", [now])

        async def start(candidate_id, cycle_id, strategy_key, body_json, prior):
            if not self._slots.research_due(slot, self._clock.now()):
                return                                                    # an earlier submit ran past closes_at
            if await self._store.aquery("SELECT state FROM ai_research_candidates WHERE candidate_id = ?",
                                        [candidate_id], fetch="one") != ("NEW",):
                return                                                    # closed by a sibling's limit refusal
            try:
                reply = parse_reply(SubmitReply, "submit_evaluation",
                                    await self._lab.call("submit_evaluation", json.loads(body_json)))
            except RpcNotSent:
                return                                                    # the next pump resends the same body
            except RpcOutcomeUnknown:                                     # it may have claimed an evaluation
                return await self._mark_submit_reply_lost(candidate_id)
            except RpcRefused as exc:
                return await self._close_loudly(candidate_id, f"RPC_{exc.code}", "submit_evaluation", exc, prior)
            if reply.status in ("ACCEPTED", "DUPLICATE"):
                if reply.request_id is None:
                    raise WireError(f"submit_evaluation: {reply.status} without a request id")
                return await self._mark_submitted(candidate_id, reply.request_id, prior)
            if reply.retryable:
                logger.warning("research candidate %s: submit refused for now (%s); sent again next tick",
                               candidate_id, reply.code)
                return
            logger.warning("research candidate %s: submit refused: %s", candidate_id, reply.code)
            await self._submit_refused(candidate_id, cycle_id, strategy_key, reply.code, prior)
        await self._each(rows, start, "submit")

    async def _mark_submit_reply_lost(self, candidate_id: str) -> None:
        first_loss = await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_research_candidates SET submit_reply_lost = TRUE, updated_at = ? "
            "WHERE candidate_id = ? AND NOT submit_reply_lost RETURNING candidate_id",
            [self._clock.now(), candidate_id]).fetchone())
        if first_loss is not None:
            logger.warning("research candidate %s: a submit reply was lost; the same body is sent again while its "
                           "window is open", candidate_id)

    @staticmethod
    def _view(request_id: str, reply: Any) -> EvaluationView:
        view = parse_reply(EvaluationView, "get_evaluation", reply)
        if view.request_id != request_id:
            raise WireError(f"get_evaluation: request id {view.request_id} is not the local {request_id}")
        return view

    async def _mark_submitted(self, candidate_id: str, request_id: str, prior: Optional[str] = None) -> None:
        """One request has one case and one judgment. A second candidate the service maps to a request another
        candidate holds (a submit retried past New York midnight, then the same cohort the next evening) ends."""
        now = self._clock.now()

        def work(conn: Any) -> Optional[str]:
            holder = conn.execute("SELECT candidate_id FROM ai_research_candidates WHERE request_id = ? "
                                  "AND candidate_id <> ? ORDER BY created_at LIMIT 1",
                                  [request_id, candidate_id]).fetchone()
            if holder is not None:
                self._close_in_tx(conn, candidate_id, DUPLICATE_REQUEST, prior)
                return holder[0]
            conn.execute("UPDATE ai_research_candidates SET state = 'SUBMITTED', request_id = ?, accepted_at = ?, "
                         "next_try_at = ?, updated_at = ? WHERE candidate_id = ?",
                         [request_id, now, now, now, candidate_id])
            return None
        holder = await self._store.atransaction(work)
        if holder is not None:
            logger.warning("research candidate %s ends: %s, request %s already belongs to candidate %s",
                           candidate_id, DUPLICATE_REQUEST, request_id, holder)

    async def _submit_refused(self, candidate_id: str, cycle_id: str, strategy_key: str, code: Optional[str],
                              prior: Optional[str] = None) -> None:
        code = code or "REFUSED_WITHOUT_CODE"
        now = self._clock.now()

        def work(conn: Any) -> None:
            self._close_in_tx(conn, candidate_id, f"REFUSED_{code}", prior)
            if code == "FAMILY_COOLING_DOWN":
                session = dt.datetime.strptime(cycle_id[len("rcy-"):], "%Y%m%d").date()
                self._cool_in_tx(conn, strategy_key, f"{session:%Y-%m-%d}", "CLAIM_REFUSED")
            if code == "EVALUATION_LIMIT_REACHED":                       # the day is full: the rest would be refused
                conn.execute("UPDATE ai_research_candidates SET state = 'CLOSED', end_code = ?, updated_at = ? "
                             "WHERE cycle_id = ? AND state = 'NEW' AND kind = 'INITIAL'",   # Ruling 23
                             [f"NOT_SUBMITTED_{code}", now, cycle_id])
        await self._store.atransaction(work)

    # poll -------------------------------------------------------------------------------------------
    async def _poll_submitted(self, now: dt.datetime) -> None:
        rows = await self._store.aquery("SELECT candidate_id, request_id, accepted_at, kind, prior_version_digest "
                                        "FROM ai_research_candidates WHERE state = 'SUBMITTED' AND next_try_at <= ?",
                                        [now])
        stale_after = dt.timedelta(hours=self._cfg.evaluation_stale_hours)

        async def poll(candidate_id, request_id, accepted_at, kind, prior):
            try:
                view = self._view(request_id, await self._lab.call("get_evaluation", {"request_id": request_id}))
            except AWAY:
                return
            except RpcRefused as exc:
                return await self._close_loudly(candidate_id, f"RPC_{exc.code}", "get_evaluation", exc, prior)
            if view.found and view.state in TERMINAL and view.case_digest and view.summary is not None:
                if (view.summary.kind, view.summary.prior_version_digest) != (kind, prior):
                    return await self._close_loudly(
                        candidate_id, RESEARCH_REPLY_MISMATCH, "get_evaluation",
                        f"{request_id} answers a {view.summary.kind} case of {view.summary.prior_version_digest}, "
                        f"not this {kind} candidate of {prior}", prior)
                return await self._update(
                    "UPDATE ai_research_candidates SET state = 'EVALUATED', case_digest = ?, summary_json = ?, "
                    "updated_at = ? WHERE candidate_id = ?",
                    [view.case_digest, canonical_json(view.summary.model_dump(mode="json")), now, candidate_id])
            if view.found and view.state == "FAILED":                     # parked, or failed before any case
                return await self._close_loudly(candidate_id, "EVALUATION_FAILED_NO_CASE", "get_evaluation",
                                                f"request {request_id} ended without a case to judge", prior)
            if view.found and view.state == "DONE":
                raise WireError(f"get_evaluation: {request_id} is DONE without a case")
            if not view.found:
                logger.warning("the research service does not know accepted request %s", request_id)
            if now - to_utc(accepted_at) > stale_after:
                logger.error("evaluation %s is still %s after %s; closed without a judgment", request_id,
                             view.state or "NOT_FOUND", stale_after)
                return await self._close(candidate_id, "EVALUATION_STALE", prior)
            await self._update("UPDATE ai_research_candidates SET next_try_at = ?, updated_at = ? "
                               "WHERE candidate_id = ?",
                               [now + dt.timedelta(seconds=self.poll_seconds), now, candidate_id])
        await self._each(rows, poll, "poll")

    # judge: the decision and its body are stored before the record call; Jev runs once per id --------
    async def _judge_evaluated(self, experiment_id: str) -> None:
        rows = await self._store.aquery("SELECT candidate_id, case_digest, summary_json FROM ai_research_candidates "
                                        "WHERE state = 'EVALUATED' ORDER BY updated_at, candidate_id")
        if rows and not self._gateway.ready():
            logger.warning("%d evaluated case(s) wait: the budget cap gate is closed, so Jev would be refused and the "
                           "case lost to NO_VERDICT", len(rows))
            return

        async def judge(candidate_id, case_digest, summary_json):
            case = BacktestCase(case_digest, parse_reply(CaseSummary, "case", json.loads(summary_json)))
            judgment_id = judgment_id_for(case_digest, case.summary.kind)
            existing = await self._store.atransaction(lambda conn: self._open_judgment_in_tx(
                conn, judgment_id, candidate_id, case, self._clock.now()))
            if existing is not None:
                return self._report_not_judged(judgment_id, candidate_id, *existing)
            try:
                decision = await self._judge.judge(judgment_id, case, experiment_id=experiment_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("judging %s failed", judgment_id)
                decision = JudgmentDecision(NO_VERDICT, "ENGINE_ERROR", jev_menu(case))
            if isinstance(decision, NotJudged):
                return await self._undo_judgment(judgment_id, decision.code)
            await self._decide(judgment_id, candidate_id, case, decision)
        await self._each(rows, judge, "judgment")

    async def _undo_judgment(self, judgment_id: str, code: str) -> None:
        """Jev was never asked: the JUDGING row and the pass's replay evidence go, so the next judgment of this
        case is the only one recorded and stays replayable. The candidate stays EVALUATED."""
        def work(conn: Any) -> None:
            conn.execute("DELETE FROM ai_backtest_judgments WHERE judgment_id = ? AND state = 'JUDGING'",
                         [judgment_id])
            conn.execute("DELETE FROM ai_replay_evidence WHERE decision_key = ?", [judgment_id])
        await self._store.atransaction(work)
        logger.warning("judgment %s not taken (%s): the cap gate closed before the call; judged on a later pump",
                       judgment_id, code)

    def _report_not_judged(self, judgment_id: str, candidate_id: str, state: str, owner: str) -> None:
        """This case already has a judgment row, so Jev is never asked a second time. A JUDGING row of this
        candidate has no live call (the pump runs one pass at a time): only recover() at the next start decides it."""
        if judgment_id in self._stuck_reported:
            return
        self._stuck_reported.add(judgment_id)
        if state == "JUDGING" and owner == candidate_id:
            logger.error("judgment %s is stuck in JUDGING; it is recorded as NO_VERDICT at the next restart",
                         judgment_id)
        else:
            logger.error("judgment %s already exists (state %s, candidate %s); candidate %s stays EVALUATED and "
                         "is not judged again", judgment_id, state, owner, candidate_id)

    @staticmethod
    def _open_judgment_in_tx(conn: Any, judgment_id: str, candidate_id: str, case: BacktestCase,
                             now: dt.datetime) -> Optional[tuple[str, str]]:
        """None when this call opened the judgment, else the existing row's (state, candidate_id)."""
        existing = conn.execute("SELECT state, candidate_id FROM ai_backtest_judgments WHERE judgment_id = ?",
                                [judgment_id]).fetchone()
        if existing is not None:
            return existing[0], existing[1]
        conn.execute("INSERT INTO ai_backtest_judgments (judgment_id, candidate_id, case_digest, kind, "
                     "prior_version_digest, menu_json, state, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, 'JUDGING', ?, ?)",
                     [judgment_id, candidate_id, case.case_digest, case.summary.kind,
                      case.summary.prior_version_digest, json.dumps(list(jev_menu(case))), now, now])
        return None

    async def _decide(self, judgment_id: str, candidate_id: str, case: BacktestCase,
                      decision: JudgmentDecision) -> None:
        now = self._clock.now()
        body = canonical_json(judgment_body(judgment_id, case, decision, jev_model=self._config.role("jev").model,
                                            decided_at=now))

        def work(conn: Any) -> None:
            changed = conn.execute(
                "UPDATE ai_backtest_judgments SET state = 'DECIDED', verdict = ?, code = ?, body_json = ?, "
                "body_sha256 = ?, decided_at = ?, updated_at = ? WHERE judgment_id = ? AND state = 'JUDGING' "
                "RETURNING judgment_id", [decision.verdict, decision.code, body, _sha(body), now, now,
                                          judgment_id]).fetchone()
            if changed is not None:
                self._close_in_tx(conn, candidate_id, f"JUDGED_{decision.verdict}")
        await self._store.atransaction(work)

    # record: EXISTING is success (Ruling 10) ---------------------------------------------------------
    async def _record_decided(self) -> None:
        rows = await self._store.aquery(
            "SELECT j.judgment_id, j.verdict, j.body_json, c.strategy_key, j.kind, j.prior_version_digest "
            "FROM ai_backtest_judgments j JOIN ai_research_candidates c ON c.candidate_id = j.candidate_id "
            "WHERE j.state = 'DECIDED' ORDER BY j.decided_at")

        async def record(judgment_id, verdict, body_json, strategy_key, kind, prior):
            try:
                receipt = parse_reply(JudgmentReceipt, "record_backtest_judgment", await self._registry.call(
                    "record_backtest_judgment", json.loads(body_json)))
            except AWAY:
                return                                                    # resend the unchanged body
            except RpcRefused as exc:                                     # every trader refusal arrives here
                return await self._refuse_judgment(judgment_id, f"RPC_{exc.code}", exc, prior)
            if receipt.judgment_id != judgment_id:
                raise WireError(f"record_backtest_judgment: receipt for {receipt.judgment_id}, not {judgment_id}")
            if receipt.status == "REFUSED":
                if receipt.retryable:
                    logger.warning("judgment %s refused for now (%s); sent again next tick", judgment_id, receipt.code)
                    return
                return await self._refuse_judgment(judgment_id, receipt.code or "REFUSED_WITHOUT_CODE",
                                                   receipt.detail, prior)
            if receipt.verdict != verdict:
                raise WireError(f"record_backtest_judgment: the trader holds {receipt.verdict} for {judgment_id}, "
                                f"this controller decided {verdict}")
            now = self._clock.now()

            def work(conn: Any) -> None:
                conn.execute("UPDATE ai_backtest_judgments SET state = 'RECORDED', receipt_json = ?, updated_at = ? "
                             "WHERE judgment_id = ?", [canonical_json(receipt.model_dump()), now, judgment_id])
                if verdict == "REJECT" and receipt.cooldown_until_session is not None:
                    self._cool_in_tx(conn, strategy_key, receipt.cooldown_until_session, "REJECT")
                if kind == RENEWAL and verdict == "DEPLOY":
                    self._renewal_registration_in_tx(conn, judgment_id, prior, strategy_key, now)
                elif kind == RENEWAL:
                    self._end_line_in_tx(conn, prior, f"RENEWAL_{verdict}")
                elif verdict == "DEPLOY":
                    conn.execute("INSERT INTO ai_research_registrations (judgment_id, kind, strategy_key, state, "
                                 "next_try_at, created_at, updated_at) VALUES (?, 'INITIAL', ?, 'ATTESTING', ?, ?, ?) "
                                 "ON CONFLICT (judgment_id) DO NOTHING", [judgment_id, strategy_key, now, now, now])
            await self._store.atransaction(work)
        await self._each(rows, record, "record")

    async def _refuse_judgment(self, judgment_id: str, code: str, detail: Any, prior: Optional[str]) -> None:
        logger.error("the trader refused judgment %s: %s (%s); its line ends here", judgment_id, code, detail)

        def work(conn: Any) -> None:
            conn.execute("UPDATE ai_backtest_judgments SET state = 'REFUSED', error_code = ?, updated_at = ? "
                         "WHERE judgment_id = ?", [code, self._clock.now(), judgment_id])
            if prior is not None:
                self._end_line_in_tx(conn, prior, f"RENEWAL_JUDGMENT_{code}")
        await self._store.atransaction(work)

    def _renewal_registration_in_tx(self, conn: Any, judgment_id: str, prior: str, strategy_key: str,
                                    now: dt.datetime) -> None:
        """Ruling 11: no attestation; the line's registration body with the renewal judgment id."""
        found = conn.execute("SELECT bundle_digest, body_json FROM ai_research_registrations WHERE version_digest = ?",
                             [prior]).fetchone()
        if found is None or found[0] is None or found[1] is None:
            logger.error("renewal judgment %s: no registration body of %s to renew; its line ends (%s)", judgment_id,
                         prior, RENEWAL_REGISTRATION_BODY_MISSING)
            return self._end_line_in_tx(conn, prior, RENEWAL_REGISTRATION_BODY_MISSING)
        body = canonical_json({**json.loads(found[1]), "judgment_id": judgment_id})
        conn.execute(
            "INSERT INTO ai_research_registrations (judgment_id, kind, prior_version_digest, strategy_key, "
            "bundle_digest, body_json, body_sha256, state, next_try_at, created_at, updated_at) "
            "VALUES (?, 'RENEWAL', ?, ?, ?, ?, ?, 'REGISTERING', ?, ?, ?) ON CONFLICT (judgment_id) DO NOTHING",
            [judgment_id, prior, strategy_key, found[0], body, _sha(body), now, now, now])

    # attest and register ----------------------------------------------------------------------------
    async def _advance_registrations(self, now: dt.datetime) -> None:
        rows = await self._store.aquery(
            "SELECT judgment_id, state, body_json, attest_tries, prior_version_digest FROM ai_research_registrations "
            "WHERE state IN ('ATTESTING', 'REGISTERING', 'WAITING_CAP') AND next_try_at <= ? ORDER BY created_at",
            [now])

        async def advance(judgment_id, state, body_json, attest_tries, prior):
            if state == "ATTESTING":                                      # an INITIAL row; a renewal is never attested
                body_json = await self._attest(judgment_id, attest_tries)
                if body_json is None:
                    return
            await self._register(judgment_id, body_json, prior)
        await self._each(rows, advance, "registration")

    async def _attest(self, judgment_id: str, tries: int) -> Optional[str]:
        """DUPLICATE is ATTESTED: a repeat returns the same digest and binding."""
        try:
            reply = parse_reply(AttestReply, "attest_from_judgment",
                                await self._lab.call("attest_from_judgment", {"judgment_id": judgment_id}))
        except AWAY:
            return None
        except RpcRefused as exc:
            await self._refuse_registration(judgment_id, f"RPC_{exc.code}", "attest_from_judgment", exc)
            return None
        if reply.status == "REFUSED":
            await self._attest_refused(judgment_id, reply, tries)
            return None
        if reply.bundle_digest is None or reply.binding is None:
            raise WireError(f"attest_from_judgment: {reply.status} without bundle digest and binding")
        body = canonical_json(registration_body(reply.binding, judgment_id=judgment_id,
                                                bundle_digest=reply.bundle_digest))
        await self._update("UPDATE ai_research_registrations SET state = 'REGISTERING', bundle_digest = ?, "
                           "body_json = ?, body_sha256 = ?, updated_at = ? WHERE judgment_id = ?",
                           [reply.bundle_digest, body, _sha(body), self._clock.now(), judgment_id])
        return body

    async def _attest_refused(self, judgment_id: str, reply: AttestReply, tries: int) -> None:
        code = reply.code or "REFUSED_WITHOUT_CODE"
        if not reply.retryable:
            return await self._refuse_registration(judgment_id, f"ATTEST_{code}", "attest_from_judgment",
                                                   reply.detail)
        if code == ATTEST_WAITS_FOR_TRADER:
            logger.warning("attestation of %s waits: %s", judgment_id, code)
            return
        tries += 1
        if tries >= ATTEST_MAX_TRIES:
            return await self._refuse_registration(judgment_id, f"ATTEST_{code}_RETRIES_EXHAUSTED",
                                                   "attest_from_judgment", f"{tries} tries: {reply.detail}",
                                                   attest_tries=tries)
        logger.warning("attestation of %s failed (%s), try %d of %d", judgment_id, code, tries, ATTEST_MAX_TRIES)
        await self._update("UPDATE ai_research_registrations SET attest_tries = ?, updated_at = ? "
                           "WHERE judgment_id = ?", [tries, self._clock.now(), judgment_id])

    async def _register(self, judgment_id: str, body_json: str, prior: Optional[str]) -> None:
        try:
            outcome = parse_registration(await self._registry.call("register_ai_deployment", json.loads(body_json)))
        except AWAY:
            return                                                        # an exact retry returns the same version
        except RpcRefused as exc:
            if exc.code == INTERNAL_ERROR:                                # the handler may have committed first
                return self._wait_for_settlement(judgment_id, INTERNAL_ERROR, "the trader failed while handling it "
                                                 "and the outcome is unknown; the same body is sent again")
            return await self._refuse_registration(judgment_id, f"RPC_{exc.code}", "register_ai_deployment", exc,
                                                   prior=prior)
        if outcome is None:
            return self._wait_for_settlement(judgment_id, "NOT_SETTLED", "the trader's ledger has not settled it; "
                                                                         "asked again each pump")
        now = self._clock.now()
        if isinstance(outcome, RegisterRefused) and outcome.retryable:
            return self._warn_registration_once(judgment_id, outcome.code, "the trader refused it for now and kept "
                                                                          "no record; the same body is sent again")
        if isinstance(outcome, RegisterRefused) and outcome.code in RETRY_REGISTRATION:
            # The trader keys the command by New York date: a retry on the same date replays this refusal.
            retry_at = self._slots.research_start_after_next_midnight(now)
            logger.warning("registration of %s waits until %s: %s", judgment_id, retry_at.isoformat(), outcome.code)
            await self._update("UPDATE ai_research_registrations SET state = 'WAITING_CAP', error_code = ?, "
                               "next_try_at = ?, updated_at = ? WHERE judgment_id = ?",
                               [outcome.code, retry_at, now, judgment_id])
        elif isinstance(outcome, RegisterRefused):
            await self._refuse_registration(judgment_id, outcome.code, "register_ai_deployment", "REJECTED",
                                            prior=prior)
        else:
            await self._store.atransaction(lambda conn: self._registered_in_tx(conn, judgment_id, outcome, prior, now))

    def _registered_in_tx(self, conn: Any, judgment_id: str, outcome: Registered, prior: Optional[str],
                          now: dt.datetime) -> None:
        """The new version's line is LIVE; a renewal ends the line it renewed in the same transaction."""
        conn.execute("UPDATE ai_research_registrations SET state = 'REGISTERED', base_digest = ?, version_digest = ?, "
                     "expiry_session = ?, line_state = 'LIVE', error_code = NULL, updated_at = ? WHERE judgment_id = ?",
                     [outcome.base_digest, outcome.version_digest, outcome.expiry_session, now, judgment_id])
        if prior is not None:
            self._end_line_in_tx(conn, prior, "RENEWED")

    def _wait_for_settlement(self, judgment_id: str, code: str, why: str) -> None:
        """One WARNING, then one ERROR once the trader's reconciler would raise its own critical alert."""
        self._warn_registration_once(judgment_id, code, why)
        since = self._unsettled_since.setdefault(judgment_id, self._clock.now())
        waited = (self._clock.now() - since).total_seconds()
        reported = (judgment_id, "NOT_SETTLED_ERROR")
        if waited >= UNSETTLED_ERROR_SECONDS and reported not in self._registration_waits:
            self._registration_waits.add(reported)
            logger.error("registration of %s is still NOT_SETTLED after %d s (since %s): the trader's reconciler "
                         "has not resolved it; an operator must check its command ledger", judgment_id, waited,
                         since.isoformat())

    def _warn_registration_once(self, judgment_id: str, code: str, why: str) -> None:
        if (judgment_id, code) not in self._registration_waits:
            self._registration_waits.add((judgment_id, code))
            logger.warning("registration of %s waits (%s): %s", judgment_id, code, why)

    async def _refuse_registration(self, judgment_id: str, code: str, method: str, detail: Any, *,
                                   attest_tries: Optional[int] = None, prior: Optional[str] = None) -> None:
        logger.error("%s refused judgment %s: %s (%s); its line ends here", method, judgment_id, code, detail)

        def work(conn: Any) -> None:
            conn.execute("UPDATE ai_research_registrations SET state = 'REFUSED', error_code = ?, "
                         "attest_tries = COALESCE(?, attest_tries), updated_at = ? WHERE judgment_id = ?",
                         [code, attest_tries, self._clock.now(), judgment_id])
            if prior is not None:
                self._end_line_in_tx(conn, prior, f"RENEWAL_REGISTER_{code}")
        await self._store.atransaction(work)

    # row helpers ------------------------------------------------------------------------------------
    def _close_in_tx(self, conn: Any, candidate_id: str, code: str, prior: Optional[str] = None) -> None:
        """Close a candidate; a renewal candidate (``prior`` set) ends its line with the same reason."""
        conn.execute("UPDATE ai_research_candidates SET state = 'CLOSED', end_code = ?, updated_at = ? "
                     "WHERE candidate_id = ?", [code, self._clock.now(), candidate_id])
        if prior is not None:
            self._end_line_in_tx(conn, prior, f"RENEWAL_{code}")

    async def _close(self, candidate_id: str, code: str, prior: Optional[str] = None) -> None:
        await self._store.atransaction(lambda conn: self._close_in_tx(conn, candidate_id, code, prior))

    async def _close_loudly(self, candidate_id: str, code: str, method: str, detail: Any,
                            prior: Optional[str] = None) -> None:
        logger.error("%s: research candidate %s ends: %s (%s)", method, candidate_id, code, detail)
        await self._close(candidate_id, code, prior)

    def _cool_in_tx(self, conn: Any, strategy_key: str, until: str, source: str) -> None:
        conn.execute("INSERT INTO ai_research_cooldowns VALUES (?, ?, ?, ?) ON CONFLICT (strategy_key) DO UPDATE "
                     "SET until_session = greatest(until_session, excluded.until_session), "
                     "source = excluded.source, recorded_at = excluded.recorded_at",
                     [strategy_key, until, source, self._clock.now()])

    # restart and status -----------------------------------------------------------------------------
    async def recover(self) -> None:
        """At controller start, after the gateway turned STARTED attempts into UNKNOWN. Candidates and
        registrations need nothing here: each is stored at its step and the pump resumes it."""
        await self._recover_running_slots()
        await self._settle_cut_judgments()

    async def _recover_running_slots(self) -> None:
        """A slot cut before its call left the process may run again while due; one cut after is never re-asked."""
        rows = await self._store.aquery("SELECT cycle_id FROM ai_research_cycles WHERE state = 'RUNNING'")
        for (cycle_id,) in rows:
            statuses = sorted({row[0] for row in await self._store.aquery(
                "SELECT status FROM ai_model_attempts WHERE request_key = ?", [proposal_request_key(cycle_id)])})
            if set(statuses) <= {NOT_SENT}:
                logger.warning("research slot %s was cut by a restart before its call was sent; it runs again "
                               "while due", cycle_id)
                await self._update("DELETE FROM ai_research_cycles WHERE cycle_id = ? AND state = 'RUNNING'",
                                   [cycle_id])
                continue
            logger.error("research slot %s was cut by a restart after its orchestrator call was sent (attempts: %s); "
                         "the slot is lost and not asked again", cycle_id, ", ".join(statuses))
            await self._update("UPDATE ai_research_cycles SET state = 'FAILED', reason = 'PROCESS_RESTARTED', "
                               "finished_at = ? WHERE cycle_id = ? AND state = 'RUNNING'",
                               [self._clock.now(), cycle_id])

    async def _settle_cut_judgments(self) -> None:
        """Ruling 12: a judgment cut mid-call is NO_VERDICT, never re-asked."""
        rows = await self._store.aquery(
            "SELECT j.judgment_id, j.candidate_id, j.menu_json, c.case_digest, c.summary_json "
            "FROM ai_backtest_judgments j JOIN ai_research_candidates c ON c.candidate_id = j.candidate_id "
            "WHERE j.state = 'JUDGING'")
        for judgment_id, candidate_id, menu_json, case_digest, summary_json in rows:
            last = await self._store.aquery("SELECT attempt_key FROM ai_model_attempts WHERE request_key = ? "
                                            "ORDER BY attempt_no DESC LIMIT 1", [f"{judgment_id}/jev/1"], fetch="one")
            case = BacktestCase(case_digest, parse_reply(CaseSummary, "case", json.loads(summary_json)))
            decision = JudgmentDecision(NO_VERDICT, "PROCESS_RESTARTED", tuple(json.loads(menu_json)),
                                        attempt_key=None if last is None else last[0])
            logger.warning("judgment %s was cut by a restart; recorded as NO_VERDICT, Jev is not asked again",
                           judgment_id)
            await self._decide(judgment_id, candidate_id, case, decision)

    async def counts(self) -> dict:
        row = await self._store.aquery(
            "SELECT (SELECT COUNT(*) FROM ai_research_candidates WHERE state <> 'CLOSED'), "
            "(SELECT COUNT(*) FROM ai_backtest_judgments WHERE state IN ('JUDGING', 'DECIDED')), "
            "(SELECT COUNT(*) FROM ai_research_registrations WHERE state IN ('ATTESTING', 'REGISTERING', "
            "'WAITING_CAP'))", fetch="one")
        return {"open_candidates": row[0], "unrecorded_judgments": row[1], "pending_registrations": row[2]}
