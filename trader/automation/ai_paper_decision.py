"""``submit_ai_paper_decision``: the ai_paper decision model, store and service (spec 5.4, Plan 3 Task 7).

An ``ENTER`` becomes an ``AiPaperEntryOrder`` for the existing protective
saga, so it passes ``session_risk``, the gross reservation and the
``DispatchGuard`` like any automated entry. Every refusal has its own code
and is stored on the decision row, in the same transaction as the ledger
transition, for the scoreboard (Plan 5).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Literal, Mapping, Optional

from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.ai_paper_evidence import AI_ENTRY_POLICY, AI_PAPER_ACTION  # noqa: F401 (re-exported)
from trader.automation.ai_paper_sizing import is_pending_entry
from trader.automation.ai_risk_policy import PolicyRefused
from trader.automation.command_steps import CommandSteps
from trader.automation.controller_epoch import EPOCH_MISSING, EpochRefused
from trader.automation.models import EntryPolicy, StopPolicy, TargetPolicy
from trader.automation.reduction_close import CLOSE_PENDING, start_broker_proven_close
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.trading.approval_context import ApprovalContextError
from trader.trading.command_coordinator import BrokerRejectedError, CommandRequest
from trader.trading.order_correlation import decode_order_ref, liquidation_child_root

logger = logging.getLogger(__name__)

AI_PAPER_DECISION_MIGRATION_VERSION = 56
AI_SUPERVISOR = "ai_supervisor"
COMMAND_PREFIX = "aip-"
MAX_EXPIRY_AHEAD = dt.timedelta(minutes=15)
LATCH_CODES = frozenset({"DAILY_LOSS", "DRAWDOWN", "PORTFOLIO_DAILY_LOSS"})
ACTIONS = ("ENTER", "CLOSE", "PARTIAL_CLOSE")
REDUCTIONS = ("CLOSE", "PARTIAL_CLOSE")
# R13: the ledger states of another ai_paper command that block a new decision on its conid.
# RECEIVED is left out: the check and RECEIVED -> VALIDATED run in one serialized
# journal transaction, so a concurrent decision is VALIDATED before the next one checks.
_BLOCKING_STATES = ("VALIDATED", "SUBMITTING", "OUTCOME_UNKNOWN", "SUBMITTED")
_UNACKNOWLEDGED_SAGA_STATES = frozenset({"VALIDATED", "SUBMITTING", "OUTCOME_UNKNOWN"})

_DECISION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_DECIDER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEYS = ("decision_id", "deployment_digest", "decider", "action", "conid", "side", "stop_price",
         "target_price", "quantity", "policy_revision", "evidence_digest", "expires_at")


def apply_ai_paper_decision_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(AI_PAPER_DECISION_MIGRATION_VERSION, "sp1_ai_paper_decisions", (
        """CREATE TABLE IF NOT EXISTS ai_paper_decisions (
            command_id VARCHAR PRIMARY KEY, decision_id VARCHAR, account_id VARCHAR NOT NULL,
            conid BIGINT, action VARCHAR, decider VARCHAR, evidence_digest VARCHAR,
            deployment_digest VARCHAR, strategy_digest VARCHAR, style VARCHAR,
            policy_revision INTEGER, effective_revision INTEGER, principal VARCHAR,
            controller_epoch BIGINT, body_json VARCHAR NOT NULL, state VARCHAR NOT NULL, error_code VARCHAR,
            close_root_id VARCHAR, received_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_ai_paper_decisions_root ON ai_paper_decisions(close_root_id)",
    ))


class DecisionInvalid(ValueError):
    pass


def command_id_for(decision_id: str) -> str:
    return COMMAND_PREFIX + decision_id


# ---------------------------------------------------------------------------
# Decision model (strict by hand: in-process callers bypass the wire model)
# ---------------------------------------------------------------------------

def _is_price(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _optional_price(name: str, value: Any) -> None:
    if value is not None and not _is_price(value):
        raise DecisionInvalid(f"{name} must be null or a finite number > 0")


def _parse_expiry(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise DecisionInvalid("expires_at must be an ISO-8601 string with an offset")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise DecisionInvalid("expires_at must be an ISO-8601 string with an offset") from None
    if parsed.utcoffset() is None:
        raise DecisionInvalid("expires_at must carry a UTC offset")
    return parsed


@dataclass(frozen=True)
class AiPaperDecision:
    decision_id: str
    deployment_digest: Optional[str]
    decider: str
    action: Literal["ENTER", "CLOSE", "PARTIAL_CLOSE"]
    conid: int
    side: Literal["BUY", "SELL"]
    stop_price: Optional[float]
    target_price: Optional[float]
    quantity: Optional[int]
    policy_revision: Optional[int]
    evidence_digest: str
    expires_at: dt.datetime

    def __post_init__(self):
        self._check_fields()
        self._check_shape()

    def _check_fields(self) -> None:
        if not isinstance(self.decision_id, str) or not _DECISION_ID.match(self.decision_id):
            raise DecisionInvalid("decision_id must match ^[A-Za-z0-9_-]{8,64}$")
        if self.deployment_digest is not None and (
                not isinstance(self.deployment_digest, str) or not _SHA256.match(self.deployment_digest)):
            raise DecisionInvalid("deployment_digest must be null or sha256:<64 hex>")
        if not isinstance(self.decider, str) or not _DECIDER.match(self.decider):
            raise DecisionInvalid("decider must match ^[a-z][a-z0-9_.-]{0,63}$")
        if self.action not in ACTIONS:
            raise DecisionInvalid(f"action must be one of {ACTIONS}")
        if type(self.conid) is not int or self.conid <= 0:
            raise DecisionInvalid("conid must be a JSON integer > 0")
        if self.side not in ("BUY", "SELL"):
            raise DecisionInvalid("side must be BUY or SELL")
        _optional_price("stop_price", self.stop_price)
        _optional_price("target_price", self.target_price)
        if self.quantity is not None and (type(self.quantity) is not int or self.quantity < 1):
            raise DecisionInvalid("quantity must be null or a JSON integer >= 1")
        if self.policy_revision is not None and (type(self.policy_revision) is not int or self.policy_revision < 1):
            raise DecisionInvalid("policy_revision must be null or a JSON integer >= 1")
        if not isinstance(self.evidence_digest, str) or not _SHA256.match(self.evidence_digest):
            raise DecisionInvalid("evidence_digest must be sha256:<64 hex>")
        if not isinstance(self.expires_at, dt.datetime) or self.expires_at.utcoffset() is None:
            raise DecisionInvalid("expires_at must be an aware datetime")

    def _check_shape(self) -> None:
        """R16: an ENTER carries attribution; a reduction carries none."""
        if self.action == "ENTER":
            if self.deployment_digest is None or self.policy_revision is None or self.stop_price is None:
                raise DecisionInvalid("ENTER needs deployment_digest, policy_revision and stop_price")
            if self.target_price is not None and self.target_price <= self.stop_price:
                raise DecisionInvalid("target_price must be above stop_price")
            return
        if self.deployment_digest is not None or self.policy_revision is not None:
            raise DecisionInvalid(f"{self.action} must not carry deployment_digest or policy_revision")
        if self.action == "CLOSE" and (self.stop_price is not None or self.target_price is not None
                                       or self.quantity is not None):
            raise DecisionInvalid("CLOSE takes no quantity, stop_price or target_price")
        if self.action == "PARTIAL_CLOSE" and self.quantity is None:
            raise DecisionInvalid("PARTIAL_CLOSE needs a quantity")

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "AiPaperDecision":
        if not isinstance(body, Mapping) or set(body) != set(_KEYS):
            raise DecisionInvalid(f"decision must have exactly the keys {_KEYS}")
        fields = dict(body)
        fields["expires_at"] = _parse_expiry(body["expires_at"])
        return cls(**fields)

    def to_body(self) -> dict:
        body = {name: getattr(self, name) for name in _KEYS}
        body["expires_at"] = self.expires_at.isoformat()
        return body


# ---------------------------------------------------------------------------
# What the protective saga reads (see entry_views)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AiPaperEntryOrder:
    command_id: str
    conid: int
    side: str
    requested_quantity: Decimal
    risk_fraction: Decimal
    entry_policy: EntryPolicy
    stop_policy: StopPolicy
    target_policy: Optional[TargetPolicy]
    artifact_id: str
    decision_id: str
    account_mode: str = "paper"


@dataclass(frozen=True)
class _AttestedNotional:
    order_notional: float


@dataclass(frozen=True)
class DeploymentBinding:
    artifact_id: str
    allowlist: tuple[str, ...]
    max_gross_allocation: float
    attested_strategy: _AttestedNotional
    expires_at: dt.datetime


def entry_order_for(decision: AiPaperDecision, *, command_id: str, quantity: int, limits: Any,
                    deployment_digest: str) -> AiPaperEntryOrder:
    target = None if decision.target_price is None else TargetPolicy(Decimal(str(decision.target_price)), "LMT")
    return AiPaperEntryOrder(
        command_id=command_id, conid=decision.conid, side="BUY", requested_quantity=Decimal(quantity),
        risk_fraction=Decimal(str(limits.trade_risk_fraction)), entry_policy=AI_ENTRY_POLICY,
        stop_policy=StopPolicy(Decimal(str(decision.stop_price)), "STP"), target_policy=target,
        artifact_id=deployment_digest, decision_id=decision.decision_id)


def deployment_binding(deployment: Any, *, digest: str, limits: Any, expires_at: dt.datetime) -> DeploymentBinding:
    return DeploymentBinding(
        artifact_id=digest, allowlist=tuple(str(conid) for conid in deployment.conids),
        max_gross_allocation=limits.gross_fraction,
        attested_strategy=_AttestedNotional(deployment.evidence_order_notional), expires_at=expires_at)


# ---------------------------------------------------------------------------
# Decision store
# ---------------------------------------------------------------------------

_ROW_COLUMNS = ("command_id", "decision_id", "account_id", "conid", "action", "decider", "evidence_digest",
                "deployment_digest", "strategy_digest", "style", "policy_revision", "effective_revision",
                "principal", "controller_epoch", "body_json", "state", "error_code", "close_root_id", "received_at", "updated_at")


@dataclass(frozen=True)
class DecisionRow:
    command_id: str
    account_id: str
    body_json: str
    state: str
    received_at: dt.datetime
    updated_at: dt.datetime
    decision_id: Optional[str] = None
    conid: Optional[int] = None
    action: Optional[str] = None
    decider: Optional[str] = None
    evidence_digest: Optional[str] = None
    deployment_digest: Optional[str] = None
    strategy_digest: Optional[str] = None
    style: Optional[str] = None
    policy_revision: Optional[int] = None
    effective_revision: Optional[int] = None
    principal: Optional[str] = None
    controller_epoch: Optional[int] = None
    error_code: Optional[str] = None
    close_root_id: Optional[str] = None

    @classmethod
    def received(cls, cmd: CommandRequest, now: dt.datetime) -> "DecisionRow":
        try:
            body_json = json.dumps(cmd.body, sort_keys=True, default=str)
        except Exception:
            body_json = json.dumps({"unserializable": repr(cmd.body)[:2000]})
        return cls(command_id=cmd.command_id, account_id=str(cmd.account_id), body_json=body_json,
                   state="RECEIVED", received_at=now, updated_at=now, principal=cmd.principal,
                   controller_epoch=cmd.controller_epoch)

    def with_decision(self, decision: AiPaperDecision) -> "DecisionRow":
        return replace(self, decision_id=decision.decision_id, conid=decision.conid, action=decision.action,
                       decider=decision.decider, evidence_digest=decision.evidence_digest,
                       deployment_digest=decision.deployment_digest, policy_revision=decision.policy_revision)


@dataclass(frozen=True)
class DecisionLink:
    decision_id: str
    action: str
    decider: str
    strategy_version: Optional[str]
    strategy_digest_provenance: Optional[str]
    policy_revision: Optional[int]
    effective_revision: Optional[int]
    style: Optional[str]
    digest: Optional[str]


class AiPaperDecisionStore:
    """Rows of ``ai_paper_decisions``; every write runs inside a journal transaction."""

    def __init__(self, journal: Any):
        self._journal = journal

    def upsert_in_tx(self, conn, row: DecisionRow) -> None:
        values = [getattr(row, name) for name in _ROW_COLUMNS]
        updates = ", ".join(f"{name} = excluded.{name}" for name in _ROW_COLUMNS if name != "command_id")
        conn.execute(
            f"INSERT INTO ai_paper_decisions ({', '.join(_ROW_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _ROW_COLUMNS)}) "
            f"ON CONFLICT (command_id) DO UPDATE SET {updates}", values)

    def row_by_command(self, command_id: str) -> Optional[DecisionRow]:
        found = self._journal.connect().execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM ai_paper_decisions WHERE command_id = ?",
            [command_id]).fetchone()
        return None if found is None else DecisionRow(**dict(zip(_ROW_COLUMNS, found)))

    def row(self, decision_id: str) -> Optional[DecisionRow]:
        return self.row_by_command(command_id_for(decision_id))

    def blocking_decision_on_conid_in_tx(self, conn, account_id: str, conid: int, *,
                                         exclude_command_id: str, broker: Any,
                                         working_entry_blocks: bool = True) -> Optional[str]:
        """R13: OUTCOME_UNKNOWN_PENDING or ENTRY_ALREADY_WORKING for another ai_paper command on the conid.

        A reduction passes ``working_entry_blocks=False``: the close cancels a working entry itself.
        """
        markers = ", ".join("?" for _ in _BLOCKING_STATES)
        rows = conn.execute(
            f"SELECT command_id, state FROM command_ledger WHERE action = ? AND account_id = ? "
            f"AND target_type = 'conid' AND target_id = ? AND command_id <> ? AND state IN ({markers})",
            [AI_PAPER_ACTION, account_id, str(conid), exclude_command_id, *_BLOCKING_STATES]).fetchall()
        working_entry = False
        for command_id, state in rows:
            if state != "SUBMITTED":
                return "OUTCOME_UNKNOWN_PENDING"
            group = f"og-{command_id}"
            orders = [o for o in broker.working_orders if o.order_group_id == group and not o.deleted]
            if not orders and not self._saga_acknowledged_in_tx(conn, command_id):
                return "OUTCOME_UNKNOWN_PENDING"
            working_entry |= any(o.leg == "entry" and is_pending_entry(o) for o in orders)
        return "ENTRY_ALREADY_WORKING" if working_entry and working_entry_blocks else None

    @staticmethod
    def _saga_acknowledged_in_tx(conn, command_id: str) -> bool:
        """The broker reported on the entry: a working, filled or cancelled entry event reached the saga."""
        found = conn.execute("SELECT payload FROM automated_order_sagas WHERE command_id = ?",
                             [command_id]).fetchone()
        if found is None:
            return False
        payload = json.loads(found[0])
        if payload.get("state") not in _UNACKNOWLEDGED_SAGA_STATES:
            return True
        return bool(payload.get("entry_working") or payload.get("entry_cancelled")
                    or Decimal(str(payload.get("filled_quantity") or "0")) > 0)

    def links_for_order_ref(self, order_ref: str) -> tuple[DecisionLink, ...]:
        """R24: the decisions behind a broker order ref, for the scoreboard (Plan 5)."""
        group = decode_order_ref(order_ref)
        if group is None:
            return ()
        if group.startswith(f"og-{COMMAND_PREFIX}"):
            where, value = "d.command_id = ?", group[len("og-"):]
        else:
            root = liquidation_child_root(group)
            if root is None:
                return ()
            where, value = "d.close_root_id = ?", root
        rows = self._journal.connect().execute(
            "SELECT d.decision_id, d.action, d.decider, d.strategy_digest, p.strategy_digest_provenance, "
            "d.policy_revision, d.effective_revision, d.style, d.deployment_digest "
            "FROM ai_paper_decisions d LEFT JOIN ai_deployments p ON p.digest = d.deployment_digest "
            f"WHERE {where} AND d.decision_id IS NOT NULL ORDER BY d.received_at", [value]).fetchall()
        return tuple(DecisionLink(*row) for row in rows)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class _Refusal(Exception):
    def __init__(self, code: str, *, retryable: bool = False, detail: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail


@dataclass
class _Admission:
    """The command's ledger state and what admission learned, written onto the decision row."""
    row: DecisionRow
    state: str = "RECEIVED"


class AiPaperDecisionService:
    def __init__(self, *, ledger: Any, journal: Any, controls: Any, policy: Any, deployments: Any,
                 evidence: Any, saga: Any, experiments: Any, exit_owners: Any, liquidation: Any, broker: Any,
                 config: Any, account_id: str, now: Callable[[], dt.datetime], epochs: Any,
                 schedule_reconcile: Optional[Callable[[str], None]] = None,
                 decisions: Optional[AiPaperDecisionStore] = None, close_deadline_seconds: float = 300.0):
        self._ledger = ledger
        self._journal = journal
        self._policy = policy
        self._deployments = deployments
        self._evidence = evidence
        self._saga = saga
        self._experiments = experiments
        self._exit_owners = exit_owners
        self._liquidation = liquidation
        self._broker = broker
        self._config = config
        self._account_id = account_id
        self._schedule_reconcile = schedule_reconcile
        self._decisions = decisions or AiPaperDecisionStore(journal)
        self._close_deadline_seconds = close_deadline_seconds
        self._epochs = epochs
        self._steps = CommandSteps(ledger=ledger, journal=journal, controls=controls,
                                   account_id=account_id, now=now)

    @property
    def decisions(self) -> AiPaperDecisionStore:
        return self._decisions

    def execute(self, cmd: CommandRequest) -> CommandReceipt:
        admission = _Admission(DecisionRow.received(cmd, self._steps.now_utc()))
        try:
            decision = self._parse(cmd, admission)
            if decision.action in REDUCTIONS:
                return self._execute_reduction(cmd, decision, admission)
            return self._execute_entry(cmd, decision, admission)
        except _Refusal as refusal:
            return self._reject(cmd, admission, refusal)

    # -- shared admission (steps 1-5) ------------------------------------------

    def _parse(self, cmd: CommandRequest, admission: _Admission) -> AiPaperDecision:
        if cmd.principal != AI_SUPERVISOR:
            raise _Refusal("PRINCIPAL_FORBIDDEN")
        try:
            decision = AiPaperDecision.from_body(cmd.body)
        except DecisionInvalid as ex:
            raise _Refusal("DECISION_INVALID", detail=str(ex)) from None
        admission.row = admission.row.with_decision(decision)
        if (cmd.command_id != command_id_for(decision.decision_id) or cmd.target_type != "conid"
                or cmd.target_id != str(decision.conid)):
            raise _Refusal("DECISION_INVALID", detail="command id or target does not match the decision")
        if cmd.controller_epoch is None:
            raise _Refusal(EPOCH_MISSING)
        if cmd.account_id != self._account_id:
            raise _Refusal("ACCOUNT_MISMATCH")
        return decision

    def _experiment(self, *, allow: tuple[str, ...]) -> Any:
        try:
            view = self._experiments.current(self._account_id)
        except Exception:
            raise _Refusal("EXPERIMENT_STATE_UNAVAILABLE", retryable=True) from None
        if view is None:
            raise _Refusal("NO_EXPERIMENT")
        if view.state == "STOPPED":
            raise _Refusal("EXPERIMENT_STOPPED")
        if view.state not in allow:
            raise _Refusal("EXPERIMENT_NOT_ARMED")
        return view

    def _check_expiry(self, decision: AiPaperDecision) -> None:
        now = self._steps.now_utc()
        if decision.expires_at <= now:
            raise _Refusal("DECISION_EXPIRED")
        if decision.expires_at > now + MAX_EXPIRY_AHEAD:
            raise _Refusal("DECISION_EXPIRY_TOO_FAR")

    def _capture(self) -> Any:
        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception:
            raise _Refusal("BROKER_SNAPSHOT_UNAVAILABLE", retryable=True) from None
        if getattr(snapshot, "account_id", None) != self._account_id:
            raise _Refusal("ACCOUNT_MISMATCH")
        return snapshot

    def _validate(self, cmd: CommandRequest, admission: _Admission, conid: int, snapshot: Any,
                  *, owner_check: Callable[[Any], Optional[str]], working_entry_blocks: bool = True) -> None:
        """Step 6: the blocking check and RECEIVED -> VALIDATED in one serialized journal transaction."""
        from trader.trading.command_coordinator import _command_updated_mutation, _noop_write
        now = self._steps.now_utc()
        outcome: dict[str, Optional[str]] = {}

        def work(conn, append):
            code = self._decisions.blocking_decision_on_conid_in_tx(
                conn, self._account_id, conid, exclude_command_id=cmd.command_id, broker=snapshot,
                working_entry_blocks=working_entry_blocks,
            ) or owner_check(conn)
            to_state = "REJECTED" if code else "VALIDATED"
            self._ledger.transition_in_tx(conn, cmd.command_id, "RECEIVED", to_state, error_code=code, now=now)
            self._decisions.upsert_in_tx(conn, replace(admission.row, state=to_state, error_code=code, updated_at=now))
            append(_command_updated_mutation(cmd, to_state, now, error_code=code), _noop_write,
                   f"command:{cmd.command_id}:{to_state.lower()}")
            outcome["code"] = code

        self._journal.mutate_batch_work(self._journal.connect(), work)
        if outcome["code"]:
            admission.state = "REJECTED"
            raise _Refusal(outcome["code"])
        admission.state = "VALIDATED"

    def _exit_in_progress_in_tx(self, conn, conid: int) -> Optional[str]:
        """R14: a close owned by any producer blocks a new entry on the conid or the account."""
        if (self._exit_owners.owner_for_in_tx(conn, self._account_id, conid) is not None
                or self._exit_owners.account_owner_in_tx(conn, self._account_id) is not None):
            return "EXIT_IN_PROGRESS"
        return None

    # -- ENTER (steps 6-13) ---------------------------------------------------

    def _execute_entry(self, cmd: CommandRequest, decision: AiPaperDecision, admission: _Admission) -> CommandReceipt:
        experiment = self._experiment(allow=("ARMED",))
        if getattr(experiment, "entry_block", None):
            # Plan 4 K19, row 4c: KILL_LINE_UNKNOWN, EXPERIMENT_MONITOR_NOT_READY, BOTH_MODES_ARMED.
            raise _Refusal(experiment.entry_block)
        self._check_expiry(decision)
        snapshot = self._capture()
        self._validate(cmd, admission, decision.conid, snapshot,
                       owner_check=lambda conn: self._exit_in_progress_in_tx(conn, decision.conid))
        session = self._session(snapshot)
        limits = self._limits(decision, session, admission)
        deployment = self._deployment(decision, admission)
        if decision.side != "BUY":
            raise _Refusal("SIDE_NOT_ENABLED")
        try:
            prepared = self._evidence.prepare_entry(
                conid=decision.conid, stop_price=float(decision.stop_price),
                requested_quantity=decision.quantity, limits=limits, session=session,
                notional=deployment.evidence_order_notional, experiment_id=experiment.experiment_id)
        except ApprovalContextError as ex:
            raise _Refusal(ex.code, detail=ex.message) from None
        self._claim(cmd, admission, require_unpaused=True)
        order = entry_order_for(decision, command_id=cmd.command_id, quantity=prepared.quantity,
                                limits=limits, deployment_digest=decision.deployment_digest)
        binding = deployment_binding(deployment, digest=decision.deployment_digest, limits=limits,
                                     expires_at=decision.expires_at)
        return self._start_saga(cmd, admission, decision, order, binding, prepared)

    def _session(self, snapshot: Any) -> Any:
        try:
            session = self._policy.ensure_session(snapshot)  # commits on its own (R7)
        except PolicyRefused as ex:
            raise _Refusal(ex.code, detail=ex.message) from None
        if session is None:
            raise _Refusal("SESSION_CLOSED")
        if session.latch_code is not None:
            raise _Refusal("RISK_LATCHED", detail=session.latch_code)
        return session

    def _limits(self, decision: AiPaperDecision, session: Any, admission: _Admission) -> Any:
        latest = self._policy.latest_published_revision()
        if latest is None:
            raise _Refusal("NO_ACCEPTED_POLICY")
        if decision.policy_revision != latest:
            raise _Refusal("POLICY_REVISION_STALE", detail=f"latest published revision is {latest}")
        if session.effective is None:
            raise _Refusal("NO_EFFECTIVE_LIMITS")
        admission.row = replace(admission.row, effective_revision=session.effective_revision)
        return session.effective

    def _deployment(self, decision: AiPaperDecision, admission: _Admission) -> Any:
        try:
            deployment = self._deployments.get_sealed(decision.deployment_digest)
        except DeploymentRefused as ex:
            raise _Refusal(ex.code, detail=ex.message) from None
        admission.row = replace(admission.row, strategy_digest=deployment.strategy_digest, style=deployment.style)
        if deployment.decider_verdict != "DEPLOY":
            raise _Refusal("DEPLOYMENT_NOT_DEPLOYABLE")
        if decision.conid not in deployment.conids:
            raise _Refusal("CONID_NOT_IN_DEPLOYMENT")
        if deployment.style not in self._config.styles:
            raise _Refusal("STYLE_NOT_ENABLED")
        return deployment

    def _start_saga(self, cmd, admission, decision, order, binding, prepared) -> CommandReceipt:
        try:
            saga_state = self._saga.start(
                intent=order, approval=prepared.approval, request=cmd, artifact=binding,
                session_state=prepared.session_state, allocation=prepared.allocation)
        except BrokerRejectedError:
            raise _Refusal("BROKER_REJECTED") from None
        except Exception:
            return self._outcome_unknown(cmd, admission, "DISPATCH_AMBIGUOUS")
        if saga_state.state == "CLOSED" and saga_state.error_code:
            if saga_state.error_code in LATCH_CODES:
                self._latch(saga_state.error_code, decision)
            raise _Refusal(saga_state.error_code)
        if saga_state.state == "OUTCOME_UNKNOWN":
            return self._outcome_unknown(cmd, admission, "DISPATCH_AMBIGUOUS")
        outcome = {
            "order_ids": list(saga_state.submitted_order_ids or []),
            "order_group_id": f"og-{cmd.command_id}",
            "decision_id": decision.decision_id,
            "deployment_digest": decision.deployment_digest,
            "quantity": prepared.quantity,
        }
        try:
            self._move(cmd, admission, "SUBMITTED", outcome=outcome)
        except Exception:
            return self._outcome_unknown(cmd, admission, "DISPATCH_AMBIGUOUS", outcome=outcome)
        if self._schedule_reconcile is not None:
            # The reconciler resolves the entry once the broker shows its orders (Plan 6 finding).
            self._schedule_reconcile(cmd.command_id)
        return CommandSteps.receipt(cmd.command_id, "SUBMITTED", None, False, outcome=outcome)

    def _latch(self, code: str, decision: AiPaperDecision) -> None:
        """R10. A failed latch does not unsafely admit anything: session_risk refuses each entry again."""
        try:
            self._policy.latch(code, f"refused entry {decision.decision_id}")
        except Exception:
            logger.exception("ai_paper breach latch %s could not be written", code)

    # -- reductions (Task 8) --------------------------------------------------

    def _execute_reduction(self, cmd: CommandRequest, decision: AiPaperDecision,
                           admission: _Admission) -> CommandReceipt:
        """Spec 5.4: no entry window, budget, loss check, policy or deployment; never paused (R32)."""
        experiment = self._experiment(allow=("ARMED", "PAUSED", "KILLED"))
        killed = experiment.state == "KILLED"
        if killed and self._account_owner() is None:
            # R15: join the kill flatten; never claim a new scoped root while it is being set up.
            raise _Refusal("KILL_FLATTEN_PENDING", retryable=True)
        self._check_expiry(decision)
        snapshot = self._capture()
        self._validate(cmd, admission, decision.conid, snapshot, owner_check=lambda conn: None,
                       working_entry_blocks=False)
        self._claim(cmd, admission, require_unpaused=False)
        partial = decision.action == "PARTIAL_CLOSE" and not killed
        close = start_broker_proven_close(
            liquidation=self._liquidation, broker=self._broker, account_id=self._account_id,
            command_id=cmd.command_id, conid=decision.conid, side=decision.side,
            quantity=float(decision.quantity) if partial else None,
            stop_price=decision.stop_price if partial else None,
            target_price=decision.target_price if partial else None,
            deadline=self._steps.now_utc() + dt.timedelta(seconds=self._close_deadline_seconds))
        if close.state == "REJECTED":
            raise _Refusal(close.error_code, detail=json.dumps(close.outcome, default=str))
        if close.error_code != CLOSE_PENDING:
            return self._outcome_unknown(cmd, admission, close.error_code, outcome=close.outcome)
        return self._outcome_unknown(cmd, admission, CLOSE_PENDING, outcome=close.outcome,
                                     close_root_id=close.close_root_id)

    def _account_owner(self) -> Any:
        try:
            return self._exit_owners.account_owner(self._account_id)
        except Exception:
            raise _Refusal("EXIT_OWNER_UNAVAILABLE", retryable=True) from None

    # -- ledger + decision row -------------------------------------------------

    def _row_writer(self, admission: _Admission, state: str, error_code: Optional[str] = None,
                    close_root_id: Optional[str] = None) -> Callable[[Any], None]:
        row = replace(admission.row, state=state, error_code=error_code, updated_at=self._steps.now_utc(),
                      close_root_id=close_root_id or admission.row.close_root_id)
        admission.row = row
        return lambda conn: self._decisions.upsert_in_tx(conn, row)

    def _move(self, cmd, admission, to_state, *, error_code=None, outcome=None, close_root_id=None) -> None:
        self._steps.transition(cmd, admission.state, to_state, outcome=outcome, error_code=error_code,
                               extra=self._row_writer(admission, to_state, error_code, close_root_id))
        admission.state = to_state

    def _claim(self, cmd, admission, *, require_unpaused: bool) -> None:
        """VALIDATED -> SUBMITTING. The epoch is read in the same transaction (spec 5.1, Plan 1 Ruling 1b)."""
        write_row = self._row_writer(admission, "SUBMITTING")

        def fenced_claim_writes(conn) -> None:
            self._epochs.require_current_in_tx(conn, cmd.controller_epoch)
            write_row(conn)
        try:
            self._steps.claim(cmd, require_unpaused=require_unpaused, extra=fenced_claim_writes)
        except EpochRefused as ex:
            raise _Refusal(ex.code, detail=ex.message) from None          # not retryable: a successor holds it
        except Exception as ex:
            raise _Refusal(str(getattr(ex, "code", None) or "TRADING_PAUSED"), retryable=True) from None
        admission.state = "SUBMITTING"

    def _reject(self, cmd: CommandRequest, admission: _Admission, refusal: _Refusal) -> CommandReceipt:
        outcome = None if refusal.detail is None else {"detail": refusal.detail}
        if admission.state != "REJECTED":
            self._move(cmd, admission, "REJECTED", error_code=refusal.code, outcome=outcome)
        return CommandSteps.receipt(cmd.command_id, "REJECTED", refusal.code, refusal.retryable, outcome=outcome)

    def _outcome_unknown(self, cmd, admission, code: str, *, outcome=None, close_root_id=None) -> CommandReceipt:
        self._move(cmd, admission, "OUTCOME_UNKNOWN", error_code=code, outcome=outcome, close_root_id=close_root_id)
        if self._schedule_reconcile is not None:
            self._schedule_reconcile(cmd.command_id)
        return CommandSteps.receipt(cmd.command_id, "OUTCOME_UNKNOWN", code, False, outcome=outcome)

