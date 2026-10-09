"""Jev as backtest judge (SP2c spec 4, 5.3, 8; Plan 4 Rulings 11-15, 19-20). Code builds the case and the menu;
any failure is NO_VERDICT, never a default DEPLOY."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

from trader.ai.decision_replay import manifest_mismatch
from trader.ai.gateway import CallFailed, CallRefused
from trader.ai.ids import attempt_ref
from trader.ai.model_client import ModelRequest
from trader.ai.outbox import register_context_in_tx
from trader.ai.replay import INCOMPLETE, ExternalAdapterCounter, ReplayEvidence, ReplayResult, ReplaySession
from trader.ai.research_roles import backtest_messages, parse_backtest_ruling
from trader.ai.research_wire import DIGEST, CaseSummary, WireError, parse_reply
from trader.ai.tools import LiveTools, ReplayTools
from trader.ai.untrusted import OutputRefusal

FULL_MENU, NO_DEPLOY_MENU = ("DEPLOY", "SHADOW", "REJECT"), ("SHADOW", "REJECT")
NO_VERDICT = "NO_VERDICT"
RETRY_REFUSALS = frozenset({"IN_FLIGHT_LIMIT_DEADLINE", "HOURLY_LIMIT_EXCEEDS_DEADLINE", "DEADLINE_EXPIRED"})
NOT_JUDGED_REFUSALS = frozenset({"BUDGET_CAP_UNKNOWN"})     # the cap gate closed: no judgment was taken


_DIGEST = re.compile(DIGEST)


def judgment_id_for(case_digest: str) -> str:
    """One judgment per case (spec 5.2 item 2): the id follows from the case, so a retry reuses it."""
    if not _DIGEST.fullmatch(case_digest):
        raise ValueError("a case digest is sha256: and 64 lower-case hex characters")
    return "jdg-" + hashlib.sha256(f"INITIAL|{case_digest}".encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class BacktestCase:
    case_digest: str
    summary: CaseSummary

    def __post_init__(self) -> None:
        if not isinstance(self.case_digest, str) or not _DIGEST.fullmatch(self.case_digest):
            raise WireError("case: the digest is not sha256: and 64 lower-case hex characters")
        if self.summary.kind != "INITIAL":
            raise WireError(f"case: a {self.summary.kind} case is not judged here (renewals are Plan 5)")

    def to_json(self) -> dict:
        return {"case_digest": self.case_digest, "summary": self.summary.model_dump(mode="json")}

    @classmethod
    def from_json(cls, value: Any) -> "BacktestCase":
        if not isinstance(value, dict) or set(value) != {"case_digest", "summary"}:
            raise WireError("case: wrong keys")
        return cls(value["case_digest"], parse_reply(CaseSummary, "case", value["summary"]))


def jev_menu(case: BacktestCase) -> tuple[str, ...]:
    """Rules first (spec 2, Ruling 13): DEPLOY only when Plan 3's summary says every rule passed."""
    return FULL_MENU if case.summary.rules_passed else NO_DEPLOY_MENU


@dataclass(frozen=True)
class JudgmentDecision:
    verdict: str                                  # DEPLOY | SHADOW | REJECT | NO_VERDICT
    code: str
    menu: tuple[str, ...]
    reason: str = ""
    review: Optional[Mapping[str, str]] = None
    attempt_key: Optional[str] = None

    def summary(self) -> dict:
        return {"verdict": self.verdict, "code": self.code, "menu": list(self.menu), "attempt_key": self.attempt_key,
                "review": None if self.review is None else dict(self.review)}


@dataclass(frozen=True)
class NotJudged:
    """The cap gate closed before Jev was ever asked. Nothing is recorded: the case is judged again later."""
    code: str

    def summary(self) -> dict:
        return {"not_judged": self.code}


@dataclass(frozen=True)
class BacktestJudgeSettings:
    attempts: int
    max_output_tokens: int

    @classmethod
    def from_config(cls, config: Any) -> "BacktestJudgeSettings":
        return cls(config.research.judge_attempts, config.role("jev").max_output_tokens)


async def _call_jev(tools: Any, request: ModelRequest, try_no: int) -> Any:
    """One try. A refusal sends nothing and leaves no journal attempt, so every try records how it ended, and
    replay raises the recorded refusal without a gateway call: live and replay take the same branch."""
    refusal = await tools.recorded_refusal(try_no)
    if refusal is not None:
        raise CallRefused(refusal)
    try:
        result = await tools.gateway.call("jev", request, tools.gateway.new_deadline(tools.unit_key))
    except CallRefused as exc:
        await tools.record_try(try_no, exc.code)
        raise
    except CallFailed:
        await tools.record_try(try_no, None)
        raise
    await tools.record_try(try_no, None)
    return result


async def judge_backtest(tools: Any, case_json: Optional[dict],
                         settings: BacktestJudgeSettings) -> Union[JudgmentDecision, NotJudged]:
    """One replayable judgment (Ruling 11): retry only a call with no answer; any answer is final.
    A closed cap gate before any try reached the gateway is NotJudged, never a NO_VERDICT."""
    case = BacktestCase.from_json(await tools.given("case", case_json))
    menu = jev_menu(case)
    request = ModelRequest(request_key=tools.request_key("jev", 1),
                           messages=backtest_messages(case.summary.model_dump(mode="json"), menu),
                           max_output_tokens=settings.max_output_tokens)
    attempt_key, code = None, "JEV_NOT_ASKED"
    for try_no in range(1, settings.attempts + 1):
        try:
            result = await _call_jev(tools, request, try_no)
        except CallRefused as exc:
            code = f"MODEL_REFUSED_{exc.code}"
            if exc.code in NOT_JUDGED_REFUSALS and attempt_key is None:
                return NotJudged(code)
            if exc.code in RETRY_REFUSALS:
                continue
            break                                                 # budget or config: NO_VERDICT now (spec 8)
        except CallFailed as exc:
            attempt_key, code = exc.attempt_key, f"MODEL_FAILED_{exc.outcome}"
            continue
        attempt_key = result.attempt_key
        ruling = parse_backtest_ruling(result.response.text, menu=menu)
        if isinstance(ruling, OutputRefusal):
            return JudgmentDecision(NO_VERDICT, ruling.code, menu, ruling.detail[:300], None, attempt_key)
        return JudgmentDecision(ruling.verdict, f"JEV_{ruling.verdict}", menu, ruling.reason[:300], ruling.review,
                                attempt_key)
    return JudgmentDecision(NO_VERDICT, code, menu, attempt_key=attempt_key)


def judgment_body(judgment_id: str, case: BacktestCase, decision: JudgmentDecision, *, jev_model: str,
                  decided_at: dt.datetime) -> dict:
    """Plan 1's record_backtest_judgment body. Built once, stored, resent unchanged."""
    deploy = decision.verdict == "DEPLOY"
    if deploy and (decision.review is None or decision.menu != FULL_MENU):
        raise ValueError("a DEPLOY needs the full review and a menu that offered DEPLOY")
    return {"judgment_id": judgment_id, "case_digest": case.case_digest, "kind": "INITIAL",
            "renewal_of_version": None, "verdict": decision.verdict, "menu": list(decision.menu),
            "jev_model": jev_model,
            "jev_attempt_ref": None if decision.attempt_key is None else attempt_ref(decision.attempt_key),
            "decided_at": decided_at.astimezone(dt.timezone.utc).isoformat(),
            "narrative": dict(decision.review) if deploy else None}


class BacktestJudgeRunner:
    def __init__(self, *, config: Any, gateway: Any, store: Any, clock: Any, recorder: Any):
        self._config, self._gateway, self._store, self._clock, self._recorder = config, gateway, store, clock, recorder
        self._settings = BacktestJudgeSettings.from_config(config)

    async def judge(self, judgment_id: str, case: BacktestCase, *,
                    experiment_id: str) -> Union[JudgmentDecision, NotJudged]:
        now = self._clock.now()
        await self._store.atransaction(lambda conn: register_context_in_tx(
            conn, context_key=judgment_id, experiment_id=experiment_id, served_kind="research",
            served_id=judgment_id, now=now))
        tools = LiveTools(unit_key=judgment_id, reads=None, recorder=self._recorder, clock=self._clock,
                          gateway=self._gateway, deadline=None)
        try:
            return await judge_backtest(tools, case.to_json(), self._settings)
        finally:
            await tools.finish(self._config.digest())


async def replay_backtest_judgment(store: Any, judgment_id: str, *, config: Any,
                                   counter: Optional[ExternalAdapterCounter] = None) -> ReplayResult:
    passes = (await store.aquery("SELECT COUNT(*) FROM ai_replay_evidence WHERE decision_key = ? "
                                 "AND name = 'given:case'", [judgment_id], fetch="one"))[0]
    if passes > 1:
        return ReplayResult(INCOMPLETE, missing=("rejudged_unit",))
    evidence = await asyncio.to_thread(ReplayEvidence.load, store, judgment_id)
    mismatch = manifest_mismatch(evidence.manifest, config)
    if mismatch:
        return ReplayResult(INCOMPLETE, missing=mismatch)
    session = ReplaySession(evidence, counter or ExternalAdapterCounter())
    settings = BacktestJudgeSettings.from_config(config)

    async def work(replay: ReplaySession) -> dict:
        return (await judge_backtest(ReplayTools(replay, judgment_id), None, settings)).summary()
    result = await session.arun(work)
    session.assert_no_external_calls()
    return result
