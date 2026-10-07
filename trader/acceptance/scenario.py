"""The SP1 acceptance scenario (Plan 6 Task 2, spec 6 "Acceptance harness").

A fixed sequence, not an autonomous loop. ``run()`` does steps 1-7 plus the S
steps of the live OCA shrink proof (ruling 13) and ends with B open and
protected for the session flatten. ``finish()`` runs after 15:55 ET and checks
the end state on broker evidence and the scoreboard.

Every write is journaled before it is sent (``intent`` with the exact body
text) and after (``receipt``). A resume replays the stored text unchanged.
Waiting never sends anything: a timeout fails the step.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Optional

from trader.acceptance.journal import RunJournal, canonical_text
from trader.acceptance.order_status import may_still_fill
from trader.acceptance.ports import OperatorChannelUnavailable, RemoteRefusal
from trader.acceptance.preflight import READINGS_APART_SECONDS, evaluate_preflight

RUN_STEPS = ("preflight", "register", "publish", "enter_a", "enter_b", "partial_close_a", "close_a",
             "enter_s", "shrink_proof", "settle_s")
END_CHECKS = ("session_flat", "equity_row_flat", "no_positions_or_orders", "round_trips", "oca_shrink",
              "no_incidents")
WORKING = ("Submitted", "PreSubmitted")
DECISION_TTL = dt.timedelta(minutes=10)
FINISH_TIMEOUT_SECONDS = 1200.0
DECIDER = "acceptance_harness"
AAPL, MSFT = 265598, 272093


class SimulatedCrash(BaseException):
    """Test hook: the host process dies at a named point."""


class StepFailure(Exception):
    def __init__(self, code: str, evidence: Optional[dict] = None):
        self.code = code
        self.evidence = evidence or {}
        super().__init__(code)


@dataclass(frozen=True)
class StepResult:
    name: str
    passed: bool
    code: Optional[str]
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    passed: bool
    oca_shrink: str


@dataclass(frozen=True)
class AcceptanceSettings:
    run_id: str
    account_id: str
    conid_a: int = AAPL
    conid_b: int = MSFT
    quantity_a: int = 3
    quantity_b: int = 1
    partial_quantity: int = 1
    quantity_s: int = 3
    notional: float = 2000.0
    strategy_path: str = "strategies/opening_range_breakout.py"
    strategy_class: str = "OpeningRangeBreakout"
    strategy_bytes: bytes = b""
    step_timeout: float = 120.0
    poll_seconds: float = 2.0

    @property
    def conid_s(self) -> int:
        return self.conid_a

    @property
    def strategy_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.strategy_bytes).hexdigest()

    def to_json(self) -> dict:
        payload = asdict(self)
        payload.pop("strategy_bytes")
        payload["strategy_digest"] = self.strategy_digest
        return payload


def new_run_id(now: dt.datetime) -> str:
    return f"acc-{now.strftime('%Y%m%d')}-{secrets.token_hex(3)}"


def decision_id(run_id: str, step: str) -> str:
    return f"{run_id}-{step}"


def command_id_for(decision: str) -> str:
    return f"aip-{decision}"


def evidence_digest(run_id: str, step: str) -> str:
    return "sha256:" + hashlib.sha256(f"acceptance|{run_id}|{step}".encode()).hexdigest()


def held_quantity(port: Any, conid: int) -> float:
    rows = port.supervisor("get_positions", {}).get("positions") or []
    return float(sum(float(r.get("position") or 0.0) for r in rows if int(r.get("instrument_id") or 0) == conid))


def working_orders(evidence: dict, conid: int) -> list[dict]:
    return [o for o in evidence.get("orders") or [] if o.get("conid") == conid and o.get("status") in WORKING]


def build_entry(settings: AcceptanceSettings, ctx: dict, step: str, conid: int, quantity: int, ask: float,
                now: dt.datetime, *, target: bool = False) -> dict:
    return {"decision_id": decision_id(settings.run_id, step), "deployment_digest": ctx["deployment_digest"],
            "decider": DECIDER, "action": "ENTER", "conid": conid, "side": "BUY",
            "stop_price": round(ask * 0.98, 2), "target_price": round(ask * 1.02, 2) if target else None,
            "quantity": quantity, "policy_revision": ctx["policy_revision"],
            "evidence_digest": evidence_digest(settings.run_id, step),
            "expires_at": (now + DECISION_TTL).isoformat()}


def build_entry_s(settings: AcceptanceSettings, ask: float, *, ctx: Optional[dict] = None,
                  now: Optional[dt.datetime] = None) -> dict:
    """S's own ENTER (Task 7): stop below and target above the ask, the same notional rule."""
    ctx = ctx or {"deployment_digest": None, "policy_revision": None}
    return build_entry(settings, ctx, "e-s", settings.conid_s, settings.quantity_s, ask,
                       now or dt.datetime.now(dt.timezone.utc), target=True)


def build_reduction(settings: AcceptanceSettings, step: str, action: str, conid: int, now: dt.datetime, *,
                    quantity: Optional[int] = None, stop_price: Optional[float] = None,
                    target_price: Optional[float] = None) -> dict:
    """Plan 3 R16: a reduction names no deployment and no policy revision."""
    return {"decision_id": decision_id(settings.run_id, step), "deployment_digest": None, "decider": DECIDER,
            "action": action, "conid": conid, "side": "SELL", "stop_price": stop_price,
            "target_price": target_price, "quantity": quantity, "policy_revision": None,
            "evidence_digest": evidence_digest(settings.run_id, step),
            "expires_at": (now + DECISION_TTL).isoformat()}


def deployment_record(settings: AcceptanceSettings) -> dict:
    """Ruling 6: a catalogue strategy, honestly labelled as a harness fixture."""
    return {"strategy_path": settings.strategy_path, "strategy_digest": settings.strategy_digest,
            "class_name": settings.strategy_class, "params": {}, "conids": sorted([settings.conid_a, settings.conid_b]),
            "bar_size": "1 min", "style": "intraday_long", "decider": DECIDER, "decider_verdict": "DEPLOY",
            "evidence_ref": f"acceptance:{settings.run_id}", "evidence_order_notional": float(settings.notional)}


class AcceptanceScenario:
    def __init__(self, port: Any, settings: AcceptanceSettings, journal: RunJournal, *,
                 now: Optional[Callable[[], dt.datetime]] = None):
        self.port = port
        self.settings = settings
        self.journal = journal
        self._now = now or port.now
        self.ctx: dict = {}
        self._crash: Optional[tuple[str, str]] = None
        self._stop_after: Optional[str] = None

    # -- test hooks --------------------------------------------------------------------------
    def crash_after(self, step: str) -> None:
        """Raise ``SimulatedCrash`` once ``step`` has its receipt and its result journaled."""
        self._crash = ("after", step)

    def crash_before_receipt(self, step: str) -> None:
        """Raise ``SimulatedCrash`` after ``step``'s send, before its receipt is journaled."""
        self._crash = ("before_receipt", step)

    def run_until(self, step: str, *, crash_before_receipt: bool = False) -> list[StepResult]:
        if crash_before_receipt:
            self.crash_before_receipt(step)
        else:
            self._stop_after = step
        try:
            return self.run()
        except SimulatedCrash:
            return []

    # -- the two phases ----------------------------------------------------------------------
    def run(self) -> list[StepResult]:
        """Steps 1-7 and S. A resume (a journal with a settings record) skips the flat-account preflight."""
        if self.journal.settings() is not None:
            return self.resume()
        if not getattr(self.port, "has_operator", True):
            # Without --place-orders: the preflight reads only, then stop before any write.
            preflight = self._guarded("preflight", self._step_preflight)
            return [preflight, StepResult("operator_channel", False, "OPERATOR_CHANNEL_UNAVAILABLE",
                                          {"planned_calls": self.planned_calls()})]
        self.journal.append("settings", {"settings": self.settings.to_json()})
        return self._run_steps(RUN_STEPS)

    def resume(self) -> list[StepResult]:
        """Ruling 15: validate the account against the journal, then continue with stored bodies."""
        from trader.acceptance.resume import validate_resume
        self._load_context()
        check = validate_resume(self.journal, self.port, self.settings)
        if not check.passed:
            result = StepResult("resume", False, check.failures[0], {"failures": list(check.failures)})
            self.journal.append("step", self._record(result))
            return [result]
        return self._run_steps(RUN_STEPS, resuming=True)

    def run_from(self, name: str) -> list[StepResult]:
        """Run the steps from ``name`` on, with the context the journal (or the settings) gives."""
        self._load_context()
        return self._run_steps(RUN_STEPS[RUN_STEPS.index(name):], resuming=True)

    def finish(self) -> list[StepResult]:
        self._load_context()
        results = []
        for name in END_CHECKS:
            result = self._guarded(name, getattr(self, f"_check_{name}"))
            self.journal.append("step", self._record(result, kind="end_check"))
            results.append(result)
        return results

    @staticmethod
    def outcome(results: list[StepResult]) -> Outcome:
        proof = next((r for r in results if r.name == "shrink_proof"), None)
        oca = "NOT_RUN" if proof is None else proof.evidence.get("oca_shrink", "UNPROVEN")
        return Outcome(passed=bool(results) and all(r.passed for r in results), oca_shrink=oca)

    def planned_calls(self) -> list[tuple[str, str]]:
        """What ``run`` would send, for the dry run (reads used for waiting are left out)."""
        return [("supervisor", "get_acceptance_preflight"), ("supervisor", "get_acceptance_preflight"),
                ("supervisor", "get_experiment"), ("research", "register_ai_deployment"),
                ("supervisor", "publish_ai_risk_policy"),
                ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),
                ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),
                ("supervisor", "submit_ai_paper_decision"), ("supervisor", "submit_ai_paper_decision"),
                ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),
                ("operator", "acceptance_mark_start"), ("operator", "acceptance_shrink_probe"),
                ("supervisor", "submit_ai_paper_decision")]

    # -- the runner ----------------------------------------------------------------------------
    def _run_steps(self, names, *, resuming: bool = False) -> list[StepResult]:
        results: list[StepResult] = []
        for name in names:
            done = self.journal.step_result(name) if resuming else None
            if done is not None and done["passed"]:
                result = StepResult(name, True, done.get("code"), done.get("evidence") or {})
                self._absorb(result)
                results.append(result)
                continue
            if resuming and name == "preflight":
                continue                                    # ruling 15: a resume never re-runs the flat gate
            result = self._guarded(name, getattr(self, f"_step_{name}"))
            self._absorb(result)
            self.journal.append("step", self._record(result))
            results.append(result)
            if self._crash == ("after", name):
                raise SimulatedCrash(name)
            if self._stop_after == name:
                break
            if not result.passed:
                if name == "shrink_proof" and result.code == "OCA_SHRINK_UNPROVEN":
                    settle = self._guarded("settle_s", self._step_settle_s)
                    self.journal.append("step", self._record(settle))
                    results.append(settle)
                break
        return results

    def _guarded(self, name: str, fn: Callable[[], StepResult]) -> StepResult:
        try:
            return fn()
        except StepFailure as failure:
            return StepResult(name, False, failure.code, failure.evidence)
        except RemoteRefusal as refusal:
            return StepResult(name, False, refusal.code, {"detail": str(refusal)})
        except OperatorChannelUnavailable as missing:
            return StepResult(name, False, "OPERATOR_CHANNEL_UNAVAILABLE", {"detail": str(missing)})

    def _record(self, result: StepResult, kind: str = "run_step") -> dict:
        return {"name": result.name, "passed": result.passed, "code": result.code, "evidence": result.evidence,
                "phase": kind}

    def _absorb(self, result: StepResult) -> None:
        for key in ("experiment_id", "deployment_digest", "policy_revision", "ask", "stop_price", "legs"):
            if key in result.evidence:
                self.ctx[f"{result.name}.{key}"] = result.evidence[key]
                if key in ("experiment_id", "deployment_digest", "policy_revision"):
                    self.ctx[key] = result.evidence[key]

    def _load_context(self) -> None:
        for entry in self.journal.of_kind("step"):
            if entry.get("passed"):
                self._absorb(StepResult(entry["name"], True, entry.get("code"), entry.get("evidence") or {}))

    # -- sending ---------------------------------------------------------------------------------
    def _send(self, step: str, principal: str, method: str, build: Callable[[], dict],
              command_id: Optional[str]) -> dict:
        prior = self.journal.intent_for(step, method)
        if prior is None:
            body_json = canonical_text(build())
            self.journal.record_intent(step, method, principal, body_json, command_id)
        else:
            receipt = self.journal.receipt_for(step, method)
            if receipt is not None:
                return receipt["reply"]
            found = self._lookup(prior.get("command_id"))
            if found is not None:
                self.journal.record_receipt(step, found, method)
                return found
            body_json = prior["body_json"]
            if self._expired(json.loads(body_json)):
                raise StepFailure("RESUME_EXPIRED_NO_COMMAND", {"command_id": prior.get("command_id")})
        reply = getattr(self.port, principal)(method, json.loads(body_json))
        if self._crash == ("before_receipt", step):
            raise SimulatedCrash(step)
        self.journal.record_receipt(step, reply, method)
        return reply

    def _lookup(self, command_id: Optional[str]) -> Optional[dict]:
        if not command_id:
            return None
        try:
            return self.port.supervisor("get_command", {"command_id": command_id})
        except RemoteRefusal as refusal:
            if refusal.code == "COMMAND_NOT_FOUND":
                return None
            raise

    def _expired(self, body: dict) -> bool:
        expires = body.get("expires_at")
        return expires is not None and dt.datetime.fromisoformat(expires) <= self._now()

    @staticmethod
    def _accepted(reply: dict, *, allowed=("SUBMITTED", "RESOLVED", "OUTCOME_UNKNOWN")) -> dict:
        if reply.get("state") not in allowed:
            raise StepFailure(reply.get("error_code") or f"STATE_{reply.get('state')}", {"receipt": reply})
        return reply

    # -- waiting ---------------------------------------------------------------------------------
    def _wait(self, check: Callable[[], Optional[dict]], timeout: float, code: Any,
              last: Optional[Callable[[], dict]] = None) -> dict:
        """Poll ``check`` until it returns evidence; at the deadline fail with ``code`` (or ``code()``)."""
        deadline = self._now() + dt.timedelta(seconds=timeout)
        while True:
            found = check()
            if found is not None:
                return found
            if self._now() >= deadline:
                raise StepFailure(code() if callable(code) else code, last() if last else {})
            self.port.sleep(self.settings.poll_seconds)

    def _snapshot_ask(self, conid: int) -> float:
        snapshot = self.port.supervisor("get_snapshot", {"instrument_id": conid}).get("snapshot") or {}
        ask = snapshot.get("ask")
        if type(ask) not in (int, float) or not ask > 0:
            raise StepFailure("QUOTE_UNAVAILABLE", {"snapshot": snapshot})
        return float(ask)

    # -- run steps -------------------------------------------------------------------------------
    def _step_preflight(self) -> StepResult:
        first = self.port.supervisor("get_acceptance_preflight", {})
        self.port.sleep(READINGS_APART_SECONDS)
        second = self.port.supervisor("get_acceptance_preflight", {})
        verdict = evaluate_preflight(first, second, now=self._now())
        evidence = {"first": first, "second": second, "failures": list(verdict.failures)}
        if not verdict.passed:
            raise StepFailure(verdict.failures[0], evidence)
        if self.settings.account_id and second.get("account_id") != self.settings.account_id:
            raise StepFailure("ACCOUNT_MISMATCH", evidence)          # a dry run names no account
        if getattr(self.port, "has_operator", True) and second.get("acceptance_probe") is False:
            raise StepFailure("PROBE_NOT_ENABLED", evidence)        # the S proof would be refused after S entered
        view = self.port.supervisor("get_experiment", {})
        experiment = view.get("experiment") or {}
        if experiment.get("state") != "ARMED":
            raise StepFailure("EXPERIMENT_NOT_ARMED", {**evidence, "experiment": experiment})
        return StepResult("preflight", True, None, {**evidence, "experiment_id": experiment["experiment_id"]})

    def _step_register(self) -> StepResult:
        reply = self._send("register", "research", "register_ai_deployment",
                           lambda: {"deployment": deployment_record(self.settings)}, None)
        self._accepted(reply, allowed=("RESOLVED",))
        return StepResult("register", True, None, {"deployment_digest": reply["outcome"]["digest"],
                                                   "deployment_record": "harness_fixture"})

    def _step_publish(self) -> StepResult:
        from trader.automation.risk_limits import PAPER_LIMITS
        command_id = f"{self.settings.run_id}-pol"
        reply = self._send("publish", "supervisor", "publish_ai_risk_policy",
                           lambda: {"command_id": command_id, "limits": PAPER_LIMITS.to_json(),
                                    "reason": f"acceptance {self.settings.run_id}"}, command_id)
        self._accepted(reply, allowed=("RESOLVED",))
        return StepResult("publish", True, None, {"policy_revision": reply["outcome"]["revision"]})

    def _enter(self, step: str, short: str, conid: int, quantity: int, *, target: bool = False) -> dict:
        asked: dict = {}

        def build() -> dict:
            ask = self._snapshot_ask(conid)
            asked["ask"] = ask
            if quantity * ask > self.settings.notional:
                raise StepFailure("HARNESS_NOTIONAL_TOO_SMALL",
                                  {"quantity": quantity, "ask": ask, "notional": self.settings.notional})
            return build_entry(self.settings, self.ctx, short, conid, quantity, ask, self._now(), target=target)
        decision = decision_id(self.settings.run_id, short)
        reply = self._accepted(self._send(step, "supervisor", "submit_ai_paper_decision", build,
                                          command_id_for(decision)))
        stored = json.loads(self.journal.intent_for(step, "submit_ai_paper_decision")["body_json"])
        return {"receipt": reply, "decision_id": decision, "stop_price": stored["stop_price"],
                "target_price": stored["target_price"], "ask": asked.get("ask")}

    def _wait_protected(self, conid: int, quantity: float, code: str) -> dict:
        def check():
            evidence = self.port.evidence(conid)
            if evidence.get("capture_error"):
                return None
            stops = [o for o in working_orders(evidence, conid) if o.get("leg") == "stop"
                     and o.get("remaining_quantity") == quantity]
            if stops and held_quantity(self.port, conid) == quantity:
                return {"stop": stops[0], "generation_id": evidence.get("generation_id")}
            return None
        return self._wait(check, self.settings.step_timeout, code)

    def _step_enter_a(self) -> StepResult:
        sent = self._enter("enter_a", "e-a", self.settings.conid_a, self.settings.quantity_a)
        proof = self._wait_protected(self.settings.conid_a, float(self.settings.quantity_a), "ENTRY_NOT_PROTECTED")
        return StepResult("enter_a", True, None, {**sent, **proof})

    def _step_enter_b(self) -> StepResult:
        sent = self._enter("enter_b", "e-b", self.settings.conid_b, self.settings.quantity_b)
        proof = self._wait_protected(self.settings.conid_b, float(self.settings.quantity_b), "ENTRY_NOT_PROTECTED")
        return StepResult("enter_b", True, None, {**sent, **proof})

    def _command_outcome(self, command_id: str) -> dict:
        reply = self._lookup(command_id) or {}
        if reply.get("state") == "REJECTED":
            raise StepFailure(reply.get("error_code") or "REJECTED", {"receipt": reply})
        return reply

    _CLOSE_FAILURES = {"FAILED_SAFE": "CLOSE_FAILED_SAFE"}

    def _step_partial_close_a(self) -> StepResult:
        conid, run = self.settings.conid_a, self.settings.run_id
        remainder = float(self.settings.quantity_a - self.settings.partial_quantity)
        ask = self.ctx.get("enter_a.ask") or self.ctx.get("enter_a.stop_price", 0.0) / 0.98
        decision = decision_id(run, "pc-a")
        command_id = command_id_for(decision)
        self._accepted(self._send(
            "partial_close_a", "supervisor", "submit_ai_paper_decision",
            lambda: build_reduction(self.settings, "pc-a", "PARTIAL_CLOSE", conid, self._now(),
                                    quantity=self.settings.partial_quantity,
                                    stop_price=self.ctx["enter_a.stop_price"], target_price=round(ask * 1.02, 2)),
            command_id))
        seen: dict = {}

        def check():
            outcome = (self._command_outcome(command_id).get("outcome") or {})
            state = outcome.get("liquidation_state")
            seen["state"] = state
            if state == "CLOSED":
                raise StepFailure("PARTIAL_ENDED_CLOSED", {"liquidation_state": state})
            if state in self._CLOSE_FAILURES or state in ("REDUCE_FAILED", "SUPERSEDED", "FLAT"):
                raise StepFailure(self._CLOSE_FAILURES.get(state, f"CLOSE_{state}"), {"liquidation_state": state})
            if state != "DONE":
                return None
            evidence = self.port.evidence(conid)
            legs = {o["leg"]: o for o in working_orders(evidence, conid) if o.get("leg") in ("stop", "take_profit")}
            seen["legs"] = list(legs.values())
            stop, target = legs.get("stop"), legs.get("take_profit")
            if (stop and target and stop.get("oca_group") and stop.get("oca_group") == target.get("oca_group")
                    and stop.get("oca_type") == 2 and target.get("oca_type") == 2
                    and stop.get("remaining_quantity") == remainder == target.get("remaining_quantity")):
                return {"liquidation_state": state, "legs": [stop, target],
                        "generation_id": evidence.get("generation_id")}
            return None
        proof = self._wait(check, self.settings.step_timeout,
                           lambda: "REPROTECT_NOT_LINKED" if seen.get("state") == "DONE" else "PARTIAL_NOT_DONE",
                           last=lambda: dict(seen))
        return StepResult("partial_close_a", True, None, {"decision_id": decision, **proof})

    def _close(self, step: str, short: str, conid: int, *, timeout_code: str) -> dict:
        decision = decision_id(self.settings.run_id, short)
        command_id = command_id_for(decision)
        self._accepted(self._send(step, "supervisor", "submit_ai_paper_decision",
                                  lambda: build_reduction(self.settings, short, "CLOSE", conid, self._now()),
                                  command_id))
        seen: dict = {}

        def check():
            state = (self._command_outcome(command_id).get("outcome") or {}).get("liquidation_state")
            seen["state"] = state
            if state in self._CLOSE_FAILURES or state in ("REDUCE_FAILED", "SUPERSEDED"):
                raise StepFailure(self._CLOSE_FAILURES.get(state, f"CLOSE_{state}"), {"liquidation_state": state})
            if state not in ("CLOSED", "FLAT"):
                return None
            evidence = self.port.evidence(conid)
            if evidence.get("capture_error") or working_orders(evidence, conid) or held_quantity(self.port, conid):
                return None
            return {"liquidation_state": state, "generation_id": evidence.get("generation_id")}
        proof = self._wait(check, self.settings.step_timeout, timeout_code, last=lambda: dict(seen))
        return {"decision_id": decision, **proof}

    def _step_close_a(self) -> StepResult:
        return StepResult("close_a", True, None,
                          self._close("close_a", "c-a", self.settings.conid_a, timeout_code="CLOSE_NOT_DONE"))

    # -- the S steps (Task 7, ruling 13) -----------------------------------------------------------
    def _step_enter_s(self) -> StepResult:
        sent = self._enter("enter_s", "e-s", self.settings.conid_s, self.settings.quantity_s, target=True)
        legs = self._await_s_protected()
        return StepResult("enter_s", True, None, {**sent, **legs})

    def _await_s_protected(self) -> dict:
        conid, quantity = self.settings.conid_s, float(self.settings.quantity_s)
        group = f"og-{command_id_for(decision_id(self.settings.run_id, 'e-s'))}"

        def check():
            evidence = self.port.evidence(conid)
            if evidence.get("capture_error"):
                return None
            mine = [o for o in working_orders(evidence, conid) if o.get("order_group_id") == group]
            stops = [o for o in mine if o.get("leg") == "stop"]
            targets = [o for o in mine if o.get("leg") == "take_profit"]
            if len(stops) != 1 or len(targets) != 1:
                return None
            stop, target = stops[0], targets[0]
            linked = (stop.get("oca_group") and stop.get("oca_group") == target.get("oca_group")
                      and stop.get("oca_type") == 2 == target.get("oca_type")
                      and stop.get("perm_id") and target.get("perm_id")
                      and stop.get("remaining_quantity") == quantity == target.get("remaining_quantity"))
            if not linked or held_quantity(self.port, conid) != quantity:
                return None
            return {"legs": {"stop": stop["order_entity_id"], "take_profit": target["order_entity_id"]},
                    "s_legs": [stop, target], "generation_id": evidence.get("generation_id")}
        return self._wait(check, self.settings.step_timeout, "S_NOT_PROTECTED_WITH_TARGET")

    def _step_shrink_proof(self) -> StepResult:
        from trader.acceptance.shrink_proof import run_shrink_proof
        run, conid = self.settings.run_id, self.settings.conid_s
        if self.journal.intent_for("shrink_proof", "acceptance_mark_start") is not None:
            # A resume never sends the mark or the probe again (ruling 23: one probe per mark).
            raise StepFailure("OCA_SHRINK_UNPROVEN", {"oca_shrink": "UNPROVEN", "why": "resumed after the mark"})
        common = {"experiment_id": self.ctx["experiment_id"], "run_id": run, "conid": conid,
                  "decision_id": decision_id(run, "e-s")}
        mark = self._send("shrink_proof", "operator", "acceptance_mark_start",
                          lambda: {"command_id": f"{run}-mark", **common}, f"{run}-mark")
        if mark.get("state") != "RESOLVED":
            raise StepFailure(mark.get("error_code") or "PROBE_MARK_FAILED", {"oca_shrink": "UNPROVEN"})
        probe = self._send("shrink_proof", "operator", "acceptance_shrink_probe",
                           lambda: {"command_id": f"{run}-probe", **common, "display_size": 1}, f"{run}-probe")
        if probe.get("state") != "RESOLVED":
            code = probe.get("error_code") or "PROBE_FAILED"
            raise StepFailure(code, {"oca_shrink": "FAILED" if code == "PROBE_OCA_LOST" else "UNPROVEN",
                                     "receipt": probe})
        legs = self.ctx.get("enter_s.legs")
        return run_shrink_proof(self.port, self.settings, self.journal, legs=legs, conid=conid)

    def _step_settle_s(self) -> StepResult:
        conid = self.settings.conid_s
        if self.journal.intent_for("settle_s", "submit_ai_paper_decision") is None:
            evidence = self.port.evidence(conid)
            if (not evidence.get("capture_error") and not working_orders(evidence, conid)
                    and held_quantity(self.port, conid) == 0):
                return StepResult("settle_s", True, None, {"settled_by": "already_flat",
                                                           "generation_id": evidence.get("generation_id")})
        try:
            closed = self._close("settle_s", "c-s", conid, timeout_code="S_NOT_FLAT")
        except StepFailure as failure:
            raise StepFailure("S_NOT_FLAT", {"cause": failure.code, **failure.evidence}) from None
        return StepResult("settle_s", True, None, {"settled_by": "close", **closed})

    # -- end checks (finish) -------------------------------------------------------------------------
    def _today(self) -> str:
        from zoneinfo import ZoneInfo
        return self._now().astimezone(ZoneInfo("America/New_York")).date().isoformat()

    def _experiment_id(self) -> str:
        if "experiment_id" not in self.ctx:
            experiment = self.port.supervisor("get_experiment", {}).get("experiment") or {}
            if not experiment.get("experiment_id"):
                raise StepFailure("EXPERIMENT_UNKNOWN", {})
            self.ctx["experiment_id"] = experiment["experiment_id"]
        return self.ctx["experiment_id"]

    def _today_row(self) -> dict:
        if "_row" in self.ctx:
            return self.ctx["_row"]

        def check():
            report = self.port.supervisor("get_scoreboard", {"experiment_id": self._experiment_id()})
            row = next((s for s in report.get("sessions") or [] if str(s.get("date")) == self._today()), None)
            if row is not None:
                self.ctx["_report"] = report
            return row
        row = self._wait(check, FINISH_TIMEOUT_SECONDS, "EQUITY_ROW_MISSING")
        self.ctx["_row"] = row
        return row

    def _check_session_flat(self) -> StepResult:
        row = self._today_row()
        if row.get("end_state") != "FLAT":
            raise StepFailure(row.get("end_state") or "SESSION_NOT_FLAT", {"row": row})
        return StepResult("session_flat", True, None, {"row": row})

    def _check_equity_row_flat(self) -> StepResult:
        row = self._today_row()
        if row.get("end_state") != "FLAT" or row.get("open_positions") != 0:
            raise StepFailure("EQUITY_ROW_NOT_FLAT", {"row": row})
        return StepResult("equity_row_flat", True, None, {"row": row})

    def _check_no_positions_or_orders(self) -> StepResult:
        evidence = self.port.evidence(None)
        positions = [r for r in self.port.supervisor("get_positions", {}).get("positions") or []
                     if float(r.get("position") or 0.0)]
        working = [o for o in evidence.get("orders") or [] if may_still_fill(o)]
        if evidence.get("capture_error") or positions or working:
            raise StepFailure("NOT_FLAT", {"positions": positions, "working_orders": working,
                                           "capture_error": evidence.get("capture_error")})
        return StepResult("no_positions_or_orders", True, None, {"generation_id": evidence.get("generation_id")})

    def _check_round_trips(self) -> StepResult:
        reply = self.port.trips(self._experiment_id())
        trips = reply.get("trips") or []
        s = self.settings
        a_trips = [t for t in trips if t.get("conid") == s.conid_a]
        b_trips = [t for t in trips if t.get("conid") == s.conid_b]
        expected_a = [float(s.quantity_a), float(s.quantity_s)]
        ok = ([float(t.get("closed_quantity") or 0) for t in a_trips] == expected_a
              and [float(t.get("closed_quantity") or 0) for t in b_trips] == [float(s.quantity_b)]
              and all(t.get("state") == "CLOSED" for t in a_trips + b_trips))
        if not ok:
            raise StepFailure("ROUND_TRIPS_MISMATCH", {"trips": trips})
        return StepResult("round_trips", True, None, {"trips": trips})

    def _check_oca_shrink(self) -> StepResult:
        proof = self.journal.step_result("shrink_proof")
        result = "NOT_RUN" if proof is None else (proof.get("evidence") or {}).get("oca_shrink", "UNPROVEN")
        if result != "PROVEN":
            raise StepFailure("OCA_SHRINK_" + result, {"oca_shrink": result})
        return StepResult("oca_shrink", True, None, {"oca_shrink": result})

    def _check_no_incidents(self) -> StepResult:
        self._today_row()
        incidents = (self.ctx.get("_report") or {}).get("incidents") or []
        if incidents:
            raise StepFailure("INCIDENTS_RECORDED", {"incidents": incidents})
        return StepResult("no_incidents", True, None, {})


def with_run_id(settings: AcceptanceSettings, run_id: str) -> AcceptanceSettings:
    return replace(settings, run_id=run_id)
